#!/usr/bin/env python3
"""Opt-in extreme-market guard around the frozen competition strategy.

This module produces order intents only. The broker must reconcile actual
fills and balances before persisting next_state. Call on_bar() for every fully
closed 15m candle and on_tick() with fresh bids between candles.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from competition_strategy import (
    BTC, UTC, Decision, HourSignal, OrderIntent, StrategyConfig, StrategyState,
    advance, iso, utc_time,
)
from download_v2_data import SYMBOLS

BLS_CPI_CALENDAR = "https://www.bls.gov/schedule/news_release/cpi.htm"
# Dates frozen from the BLS calendar on 2026-10-02. Recheck before deployment.
CPI_RELEASE_DATES_ET = (
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14",
)


def cpi_releases_utc() -> tuple[datetime, ...]:
    ny = ZoneInfo("America/New_York")
    return tuple(datetime.fromisoformat(f"{day}T08:30:00").replace(tzinfo=ny).astimezone(UTC)
                 for day in CPI_RELEASE_DATES_ET)


@dataclass(frozen=True)
class GuardConfig:
    # Operational starting values, not optimized on the historical windows.
    cpi_pre_minutes: int = 60
    cpi_post_minutes: int = 120
    max_portfolio_drawdown: float = 0.12
    drawdown_pause_hours: int = 24
    max_new_buy_spread_bps: float = 30.0
    max_new_buy_15m_move: float = 0.08
    max_new_buy_quote_deviation: float = 0.03
    max_quote_age_seconds: float = 5.0
    max_bar_processing_delay_seconds: float = 30.0


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    observed_at: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Quote":
        return cls(float(raw["bid"]), float(raw["ask"]), str(raw["observed_at"]))

    def is_fresh(self, when: datetime, config: GuardConfig) -> bool:
        try:
            stamp = utc_time(self.observed_at)
        except (AttributeError, TypeError, ValueError):
            return False
        age = (when - stamp).total_seconds()
        return (math.isfinite(self.bid) and math.isfinite(self.ask) and
                self.bid > 0 and self.ask >= self.bid and
                -1 <= age <= config.max_quote_age_seconds)

    @property
    def spread_bps(self) -> float:
        return 10_000 * (self.ask - self.bid) / ((self.ask + self.bid) / 2)


@dataclass
class ProtectedState:
    strategy: StrategyState = field(default_factory=StrategyState)
    peak_equity: float | None = None
    paused_until: str | None = None
    pause_reason: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProtectedState":
        return cls(StrategyState.from_mapping(raw["strategy"]),
                   raw.get("peak_equity"), raw.get("paused_until"), raw.get("pause_reason"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProtectedDecision:
    decision_at: str
    execute_not_before: str
    intents: tuple[OrderIntent, ...]
    next_state: ProtectedState
    alerts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"decision_at": self.decision_at,
                "execute_not_before": self.execute_not_before,
                "intents": [asdict(order) for order in self.intents],
                "next_state": self.next_state.to_dict(), "alerts": list(self.alerts)}


def _clone(state: ProtectedState) -> ProtectedState:
    return ProtectedState.from_mapping(state.to_dict())


def _decision(when: datetime, state: ProtectedState, orders: list[OrderIntent],
              alerts: list[str], ready_at: datetime | None = None) -> ProtectedDecision:
    execution = max(when + timedelta(seconds=1), ready_at or when)
    return ProtectedDecision(iso(when), iso(execution),
                             tuple(orders), state, tuple(alerts))


def _flatten(state: ProtectedState, reason: str) -> list[OrderIntent]:
    base = state.strategy
    orders = []
    if base.btc_held:
        orders.append(OrderIntent(BTC, "SELL", "core", reason))
    for symbol in sorted(base.alt_held):
        orders.append(OrderIntent(symbol, "SELL", "alt", reason))
    base.btc_held = False
    base.btc_peak_close = None
    base.alt_held = []
    base.alt_peak_close.clear()
    base.alt_entry_time.clear()
    base.alt_soft_fail_days.clear()
    return orders


def _cpi_window(when: datetime, config: GuardConfig) -> tuple[datetime, datetime] | None:
    for release in cpi_releases_utc():
        begin = release - timedelta(minutes=config.cpi_pre_minutes)
        end = release + timedelta(minutes=config.cpi_post_minutes)
        if begin <= when < end:
            return begin, end
    return None


def _valid_equity(equity: float | None) -> bool:
    try:
        return equity is not None and math.isfinite(float(equity)) and float(equity) > 0
    except (TypeError, ValueError):
        return False


def _update_equity_guard(state: ProtectedState, when: datetime, equity: float | None,
                         config: GuardConfig) -> bool:
    if not _valid_equity(equity):
        return False
    equity = float(equity)
    if state.paused_until is not None and when >= utc_time(state.paused_until):
        state.paused_until = None
        state.pause_reason = None
        state.peak_equity = equity
    state.peak_equity = max(equity, state.peak_equity or equity)
    if state.paused_until is None and equity <= state.peak_equity * (1 - config.max_portfolio_drawdown):
        state.paused_until = iso(when + timedelta(hours=config.drawdown_pause_hours))
        state.pause_reason = "portfolio_drawdown"
    return state.paused_until is not None and when < utc_time(state.paused_until)


def _valid_bar_snapshot(when: datetime, state: StrategyState,
                        hourly: Mapping[str, HourSignal], closes15: Mapping[str, float],
                        hour_closed_at: Mapping[str, str], close15_at: Mapping[str, str]) -> list[str]:
    daily = when.hour == 0 and when.minute == 0
    core_check = when.minute == 0 and when.hour % 4 == 0
    required_closes = set(SYMBOLS) if daily else {BTC, *state.alt_held}
    required_hours = set(SYMBOLS) if daily else ({BTC} if core_check else set())
    problems = []
    for symbol in sorted(required_closes):
        try:
            price = float(closes15[symbol])
            if not math.isfinite(price) or price <= 0 or utc_time(close15_at[symbol]) != when:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            problems.append(f"stale_or_missing_15m:{symbol}")
    for symbol in sorted(required_hours):
        try:
            row = hourly[symbol]
            if (utc_time(hour_closed_at[symbol]) != when or
                not all(math.isfinite(x) for x in (row.ret7d, row.ret72, row.liq24)) or
                any(x not in (-1, 0, 1) for x in (row.chan, row.zone, row.wyckoff))):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            problems.append(f"stale_or_missing_1h:{symbol}")
    return problems


def _stop_only(when: datetime, state: ProtectedState, closes15: Mapping[str, float],
               close15_at: Mapping[str, str]) -> list[OrderIntent]:
    """Evaluate each valid held price independently when the full bar is unavailable."""
    base = state.strategy
    orders = []
    held = [BTC] if base.btc_held else []
    held += list(base.alt_held)
    for symbol in held:
        try:
            close = float(closes15[symbol])
            if close <= 0 or not math.isfinite(close) or utc_time(close15_at[symbol]) != when:
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if symbol == BTC and base.btc_held:
            peak = max(close, float(base.btc_peak_close))
            base.btc_peak_close = peak
            if close <= peak * 0.95:
                orders.append(OrderIntent(BTC, "SELL", "core", "btc_trailing_stop_5pct"))
                base.btc_held = False
                base.btc_peak_close = None
                base.btc_cooldown_until = iso(when + timedelta(hours=4))
        elif symbol in base.alt_held:
            peak = max(close, base.alt_peak_close[symbol])
            base.alt_peak_close[symbol] = peak
            if close < peak * 0.88:
                orders.append(OrderIntent(symbol, "SELL", "alt", "alt_trailing_stop_12pct"))
                base.alt_held.remove(symbol)
                base.alt_peak_close.pop(symbol, None)
                base.alt_entry_time.pop(symbol, None)
                base.alt_soft_fail_days.pop(symbol, None)
                base.alt_cooldown_until[symbol] = iso(when + timedelta(hours=24))
    return orders


def _veto_unsafe_buys(decision: Decision,
                      quotes: Mapping[str, Quote], when: datetime,
                      closes15: Mapping[str, float], previous_closes15: Mapping[str, float],
                      config: GuardConfig) -> tuple[tuple[OrderIntent, ...], StrategyState, list[str]]:
    next_base = StrategyState.from_mapping(decision.next_state.to_dict())
    orders = []
    alerts = []
    for order in decision.intents:
        if order.side != "BUY":
            orders.append(order)
            continue
        quote = quotes.get(order.symbol)
        quote_ok = (quote is not None and quote.is_fresh(when, config) and
                    quote.spread_bps <= config.max_new_buy_spread_bps)
        current = math.nan
        try:
            current = float(closes15[order.symbol])
            previous = float(previous_closes15[order.symbol])
            move_ok = (math.isfinite(current) and math.isfinite(previous) and previous > 0 and
                       abs(current / previous - 1) <= config.max_new_buy_15m_move)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            move_ok = False
        price_ok = (quote_ok and math.isfinite(current) and current > 0 and
                    abs(quote.ask / current - 1) <= config.max_new_buy_quote_deviation)
        if quote_ok and move_ok and price_ok:
            orders.append(order)
            continue
        if not quote_ok:
            alerts.append(f"new_buy_vetoed_untrusted_quote:{order.symbol}")
        if not move_ok:
            alerts.append(f"new_buy_vetoed_abrupt_15m_move:{order.symbol}")
        if quote_ok and not price_ok:
            alerts.append(f"new_buy_vetoed_quote_price_gap:{order.symbol}")
        if order.sleeve == "core":
            next_base.btc_held = False
            next_base.btc_peak_close = None
        else:
            next_base.alt_held.remove(order.symbol)
            next_base.alt_peak_close.pop(order.symbol, None)
            next_base.alt_entry_time.pop(order.symbol, None)
            next_base.alt_soft_fail_days.pop(order.symbol, None)
    return tuple(orders), next_base, alerts


def on_bar(state: ProtectedState, when: str | datetime,
           hourly: Mapping[str, HourSignal], closes15: Mapping[str, float],
           hour_closed_at: Mapping[str, str], close15_at: Mapping[str, str],
           quotes: Mapping[str, Quote], account_equity: float | None,
           evaluated_at: str | datetime,
           previous_closes15: Mapping[str, float] | None = None,
           config: GuardConfig = GuardConfig(),
           strategy_config: StrategyConfig = StrategyConfig()) -> ProtectedDecision:
    """Process one completed UTC 15m bar; no new buys on bad data or quotes."""
    when = utc_time(when)
    evaluated_at = utc_time(evaluated_at)
    if when.second or when.microsecond or when.minute % 15:
        raise ValueError("bar time must be a completed UTC 15m boundary")
    if evaluated_at < when:
        raise ValueError("evaluated_at cannot precede the completed bar")
    if state.strategy.last_bar and when <= utc_time(state.strategy.last_bar):
        raise ValueError("bar already processed or out of order")
    out = _clone(state)
    alerts = []
    if not _valid_equity(account_equity):
        alerts.append("account_equity_unavailable")
    paused = _update_equity_guard(out, evaluated_at, account_equity, config)
    macro = _cpi_window(evaluated_at, config)
    if paused or macro is not None:
        reason = "portfolio_drawdown" if paused else "cpi_event_window"
        orders = _flatten(out, reason)
        out.strategy.last_bar = iso(when)
        alerts.append(reason)
        return _decision(when, out, orders, alerts, evaluated_at)
    problems = _valid_bar_snapshot(when, out.strategy, hourly, closes15,
                                   hour_closed_at, close15_at)
    if (evaluated_at - when).total_seconds() > config.max_bar_processing_delay_seconds:
        problems.append("bar_processing_delay")
    if problems or "account_equity_unavailable" in alerts:
        orders = _stop_only(when, out, closes15, close15_at)
        out.strategy.last_bar = iso(when)
        return _decision(when, out, orders, alerts + problems + ["new_entries_paused"], evaluated_at)
    baseline = advance(out.strategy, when, hourly, closes15, strategy_config)
    orders, next_base, quote_alerts = _veto_unsafe_buys(
        baseline, quotes, evaluated_at, closes15, previous_closes15 or {}, config)
    out.strategy = next_base
    return _decision(when, out, list(orders), alerts + quote_alerts, evaluated_at)


def on_tick(state: ProtectedState, when: str | datetime,
            quotes: Mapping[str, Quote], account_equity: float | None,
            config: GuardConfig = GuardConfig()) -> ProtectedDecision:
    """Poll fresh Roostoo bids between candles for early exits and account halt."""
    when = utc_time(when)
    out = _clone(state)
    alerts = []
    if not _valid_equity(account_equity):
        alerts.append("account_equity_unavailable")
    paused = _update_equity_guard(out, when, account_equity, config)
    if paused or _cpi_window(when, config) is not None:
        reason = "portfolio_drawdown" if paused else "cpi_event_window"
        orders = _flatten(out, reason)
        return _decision(when, out, orders, alerts + [reason])
    orders = []
    base = out.strategy
    for symbol in ([BTC] if base.btc_held else []) + list(base.alt_held):
        quote = quotes.get(symbol)
        if quote is None or not quote.is_fresh(when, config):
            alerts.append(f"stale_or_missing_quote:{symbol}")
            continue
        peak = float(base.btc_peak_close if symbol == BTC else base.alt_peak_close[symbol])
        threshold = 0.95 if symbol == BTC else 0.88
        triggered = quote.bid <= peak * threshold if symbol == BTC else quote.bid < peak * threshold
        if not triggered:
            continue
        if symbol == BTC:
            base.btc_held = False
            base.btc_peak_close = None
            base.btc_cooldown_until = iso(when + timedelta(hours=4))
            orders.append(OrderIntent(BTC, "SELL", "core", "intrabar_btc_trailing_stop_5pct"))
        else:
            base.alt_held.remove(symbol)
            base.alt_peak_close.pop(symbol, None)
            base.alt_entry_time.pop(symbol, None)
            base.alt_soft_fail_days.pop(symbol, None)
            base.alt_cooldown_until[symbol] = iso(when + timedelta(hours=24))
            orders.append(OrderIntent(symbol, "SELL", "alt", "intrabar_alt_trailing_stop_12pct"))
    return _decision(when, out, orders, alerts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="JSON with mode=bar/tick, when, quotes and account_equity")
    parser.add_argument("--state", type=Path, help="last fill-confirmed protected state")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rank-exit-confirmation-days", type=int, default=1)
    parser.add_argument("--structure-exit-confirmation-days", type=int, default=1)
    args = parser.parse_args()
    strategy_config = StrategyConfig(args.rank_exit_confirmation_days,
                                     args.structure_exit_confirmation_days)
    raw = json.loads(args.snapshot.read_text())
    state = ProtectedState.from_mapping(json.loads(args.state.read_text())) if args.state else ProtectedState()
    quotes = {symbol: Quote.from_mapping(row) for symbol, row in raw.get("quotes", {}).items()}
    equity = float(raw["account_equity"]) if raw.get("account_equity") is not None else None
    if raw["mode"] == "tick":
        decision = on_tick(state, raw["when"], quotes, equity)
    elif raw["mode"] == "bar":
        hourly = {symbol: HourSignal.from_mapping(row)
                  for symbol, row in raw.get("hourly", {}).items()}
        decision = on_bar(state, raw["when"], hourly, raw.get("close_15m", {}),
                          raw.get("hour_closed_at", {}), raw.get("close_15m_at", {}),
                          quotes, equity, raw["evaluated_at"], raw.get("previous_close_15m", {}),
                          strategy_config=strategy_config)
    else:
        raise ValueError("mode must be bar or tick")
    payload = json.dumps(decision.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
