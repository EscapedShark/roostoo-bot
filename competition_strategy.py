#!/usr/bin/env python3
"""Frozen Roostoo competition signals: 20% BTC core + 80% slow crypto rotation.

The engine is pure: advance() accepts completed-bar signals and a persisted
state, then returns order *intents* and the proposed next state. It never calls
an exchange or assumes an intent has filled. Use `replay` to audit it against
the research implementation, or `decide` to integrate with a separate broker.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from download_v2_data import SYMBOLS

UTC = timezone.utc
BTC = "BTCUSDT"
STATE_VERSION = 1


def utc_time(value: str | datetime) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class HourSignal:
    """All values are computed using the 1h candle closed at `when`."""

    ret7d: float
    ret72: float
    liq24: float
    chan: int
    zone: int
    wyckoff: int

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "HourSignal":
        return cls(float(row["ret7d"]), float(row["ret72"]),
                   float(row["liq24"]), int(row["chan"]),
                   int(row["zone"]), int(row["wyckoff"]))


@dataclass
class StrategyState:
    version: int = STATE_VERSION
    last_bar: str | None = None
    btc_held: bool = False
    btc_peak_close: float | None = None
    btc_cooldown_until: str | None = None
    alt_held: list[str] = field(default_factory=list)
    alt_peak_close: dict[str, float] = field(default_factory=dict)
    alt_entry_time: dict[str, str] = field(default_factory=dict)
    alt_cooldown_until: dict[str, str] = field(default_factory=dict)
    alt_soft_fail_days: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrategyState":
        state = cls(**dict(raw))
        if state.version != STATE_VERSION:
            raise ValueError(f"unsupported state version: {state.version}")
        if state.btc_held and state.btc_peak_close is None:
            raise ValueError("held BTC requires a saved peak close")
        if len(set(state.alt_held)) != len(state.alt_held) or len(state.alt_held) > 2:
            raise ValueError("alt_held must contain at most two distinct symbols")
        for symbol in state.alt_held:
            if symbol not in SYMBOLS or symbol not in state.alt_peak_close or symbol not in state.alt_entry_time:
                raise ValueError(f"incomplete saved state for {symbol}")
        if any(symbol not in state.alt_held or not isinstance(days, int) or days < 0
               for symbol, days in state.alt_soft_fail_days.items()):
            raise ValueError("soft failure counts require held symbols and nonnegative integers")
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    sleeve: str
    reason: str
    entry_fraction_of_sleeve: float | None = None
    initial_sleeve_fraction_of_account: float | None = None


@dataclass(frozen=True)
class Decision:
    decision_at: str
    execute_not_before: str
    intents: tuple[OrderIntent, ...]
    next_state: StrategyState

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_at": self.decision_at,
            "execute_not_before": self.execute_not_before,
            "intents": [asdict(intent) for intent in self.intents],
            "next_state": self.next_state.to_dict(),
        }


@dataclass(frozen=True)
class StrategyConfig:
    # One preserves the frozen baseline. Two is the research hysteresis arm.
    rank_exit_confirmation_days: int = 1
    structure_exit_confirmation_days: int = 1

    def __post_init__(self) -> None:
        if self.rank_exit_confirmation_days < 1 or self.structure_exit_confirmation_days < 1:
            raise ValueError("exit confirmation days must be positive")


def _close(closes: Mapping[str, float], symbol: str) -> float:
    if symbol not in closes:
        raise ValueError(f"missing fully closed 15m close for {symbol}")
    value = float(closes[symbol])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid fully closed 15m close for {symbol}")
    return value


def _hour(signals: Mapping[str, HourSignal], symbol: str) -> HourSignal:
    if symbol not in signals:
        raise ValueError(f"missing fully closed 1h signal for {symbol}")
    return signals[symbol]


def advance(state: StrategyState, when: str | datetime,
            hourly: Mapping[str, HourSignal], closes15: Mapping[str, float],
            config: StrategyConfig = StrategyConfig()) -> Decision:
    """Advance one completed 15m bar; assume emitted intents fill before next bar.

    The caller must reconcile actual fills before committing next_state. Existing
    quantities are retained. New buys use the current value of their separately
    funded sleeve, not a fixed fraction of combined account equity.
    """
    when = utc_time(when)
    if when.second or when.microsecond or when.minute % 15:
        raise ValueError("decision time must be a completed UTC 15m boundary")
    if state.last_bar is not None and when <= utc_time(state.last_bar):
        raise ValueError("bar already processed or out of order")
    next_state = StrategyState.from_mapping(state.to_dict())
    prior_btc = state.btc_held
    prior_alts = set(state.alt_held)
    reasons: dict[tuple[str, str], str] = {}
    btc_close = _close(closes15, BTC)

    # BTC core: a 15m close can trigger the 5% trailing stop; otherwise the
    # seven-day Chan/Wyckoff trend gate runs at 00, 04, ..., 20 UTC.
    btc_stopped = False
    if next_state.btc_held:
        next_state.btc_peak_close = max(btc_close, float(next_state.btc_peak_close))
        if btc_close <= next_state.btc_peak_close * 0.95:
            next_state.btc_held = False
            next_state.btc_peak_close = None
            next_state.btc_cooldown_until = iso(when + timedelta(hours=4))
            reasons[("core", BTC)] = "btc_trailing_stop_5pct"
            btc_stopped = True
    else:
        next_state.btc_peak_close = None
    if not btc_stopped and when.minute == 0 and when.hour % 4 == 0:
        btc = _hour(hourly, BTC)
        if next_state.btc_held:
            bearish = ((btc.ret7d < -0.02 and btc.chan == -1) or
                       (btc.ret7d < -0.04 and btc.wyckoff == -1))
            if bearish:
                next_state.btc_held = False
                next_state.btc_peak_close = None
                reasons[("core", BTC)] = "btc_bearish_structure"
        elif (next_state.btc_cooldown_until is None or
              when >= utc_time(next_state.btc_cooldown_until)):
            bullish = btc.ret7d > 0 and btc.chan >= 0 and btc.wyckoff >= 0
            if bullish:
                next_state.btc_held = True
                next_state.btc_peak_close = btc_close
                reasons[("core", BTC)] = "btc_trend_entry"

    # Alt sleeve: keep the actual chosen symbols, not periodically rebalanced
    # weights. The trailing stop is checked on every completed 15m bar.
    stopped: set[str] = set()
    for symbol in list(next_state.alt_held):
        close = _close(closes15, symbol)
        peak = max(close, next_state.alt_peak_close[symbol])
        next_state.alt_peak_close[symbol] = peak
        if close < peak * 0.88:
            stopped.add(symbol)
            next_state.alt_cooldown_until[symbol] = iso(when + timedelta(hours=24))
            next_state.alt_peak_close.pop(symbol, None)
            next_state.alt_entry_time.pop(symbol, None)
            next_state.alt_soft_fail_days.pop(symbol, None)
            reasons[("alt", symbol)] = "alt_trailing_stop_12pct"
    active = set(next_state.alt_held) - stopped
    if when.hour == 0 and when.minute == 0:
        if set(hourly) != set(SYMBOLS):
            missing = sorted(set(SYMBOLS) - set(hourly))
            raise ValueError(f"daily ranking requires all 21 closed 1h signals; missing {missing}")
        btc_ret7 = _hour(hourly, BTC).ret7d
        candidates: list[tuple[float, str]] = []
        for symbol in SYMBOLS:
            row = hourly[symbol]
            _close(closes15, symbol)
            if not all(math.isfinite(v) for v in (row.liq24, row.ret7d, row.ret72)):
                continue
            if row.liq24 < 20_000_000 or row.chan < 0 or row.zone < 0 or row.wyckoff < 0:
                continue
            candidates.append((row.ret7d + 0.3 * row.ret72, symbol))
        ranked = [symbol for _, symbol in sorted(candidates, reverse=True)]
        selected: list[str] = []
        for symbol in sorted(active):
            row = hourly[symbol]
            entry_time = utc_time(next_state.alt_entry_time[symbol])
            in_top_five = symbol in ranked[:5]
            within_initial_hold = when - entry_time < timedelta(hours=48)
            structure_ok = row.chan >= 0 and row.zone >= 0 and row.wyckoff >= 0
            valid = in_top_five or (within_initial_hold and structure_ok)
            valid = valid and row.ret7d > -0.02
            if math.isfinite(btc_ret7):
                valid = valid and row.ret7d - btc_ret7 > -0.02
            # Experimental confirmation applies only to a pure rank miss or
            # one negative Chan/Wyckoff signal. Price trend, zone failure,
            # multiple negative signals and 15m stops remain immediate.
            rank_only_failure = (not in_top_five and not within_initial_hold and
                                 structure_ok and row.liq24 >= 20_000_000 and
                                 row.ret7d > -0.02 and
                                 (not math.isfinite(btc_ret7) or
                                  row.ret7d - btc_ret7 > -0.02))
            single_structure_failure = (
                not in_top_five and row.zone >= 0 and row.liq24 >= 20_000_000 and
                (row.chan < 0) != (row.wyckoff < 0) and
                row.ret7d > -0.02 and
                (not math.isfinite(btc_ret7) or row.ret7d - btc_ret7 > -0.02)
            )
            soft_limit = (config.rank_exit_confirmation_days
                          if rank_only_failure else
                          config.structure_exit_confirmation_days
                          if single_structure_failure else 1)
            if soft_limit > 1:
                # Rank and single-structure warnings share one consecutive
                # count, so alternating warnings cannot postpone exit forever.
                failures = next_state.alt_soft_fail_days.get(symbol, 0) + 1
                next_state.alt_soft_fail_days[symbol] = failures
                valid = failures < soft_limit
            else:
                next_state.alt_soft_fail_days.pop(symbol, None)
            if valid:
                selected.append(symbol)
            else:
                next_state.alt_peak_close.pop(symbol, None)
                next_state.alt_entry_time.pop(symbol, None)
                next_state.alt_soft_fail_days.pop(symbol, None)
                reasons[("alt", symbol)] = "alt_rank_or_trend_exit"
        for symbol in ranked:
            if len(selected) == 2:
                break
            if symbol in selected or symbol in stopped:
                continue
            cooldown = next_state.alt_cooldown_until.get(symbol)
            if cooldown is not None and when < utc_time(cooldown):
                continue
            row = hourly[symbol]
            if (row.ret7d >= 0.05 and row.ret72 >= 0.01 and
                (row.chan == 1 or row.wyckoff == 1) and
                math.isfinite(btc_ret7) and row.ret7d - btc_ret7 >= 0.05):
                selected.append(symbol)
                next_state.alt_entry_time[symbol] = iso(when)
                next_state.alt_peak_close[symbol] = _close(closes15, symbol)
                next_state.alt_soft_fail_days.pop(symbol, None)
                reasons[("alt", symbol)] = "alt_relative_strength_entry"
        next_state.alt_held = selected
    else:
        next_state.alt_held = [symbol for symbol in next_state.alt_held if symbol in active]

    next_state.last_bar = iso(when)
    current_alts = set(next_state.alt_held)
    intents: list[OrderIntent] = []
    if prior_btc and not next_state.btc_held:
        intents.append(OrderIntent(BTC, "SELL", "core", reasons.get(("core", BTC), "exit")))
    for symbol in sorted(prior_alts - current_alts):
        intents.append(OrderIntent(symbol, "SELL", "alt", reasons.get(("alt", symbol), "exit")))
    if not prior_btc and next_state.btc_held:
        intents.append(OrderIntent(BTC, "BUY", "core", "btc_trend_entry", 1.0, 0.20))
    for symbol in sorted(current_alts - prior_alts):
        intents.append(OrderIntent(symbol, "BUY", "alt", "alt_relative_strength_entry", 0.5, 0.80))
    return Decision(iso(when), iso(when + timedelta(seconds=1)), tuple(intents), next_state)


def _replay(start: str, end: str, months: set[str], verify_legacy: bool,
            config: StrategyConfig = StrategyConfig()) -> list[dict[str, Any]]:
    import pandas as pd
    from rolling_screen import prepare

    minutes, h, q = prepare(SYMBOLS, months)
    for symbol in SYMBOLS:
        h[symbol]["ret7d"] = h[symbol].close / h[symbol].close.shift(168) - 1
    old_core = old_alt = None
    old_core_held: set[str] = set()
    old_alt_held: set[str] = set()
    if verify_legacy:
        if config != StrategyConfig():
            raise ValueError("legacy parity applies only to the frozen default configuration")
        from slow_rolling_screen import make_chooser
        from trail_core import make_trailing_chooser
        old_core = make_trailing_chooser(0.05)
        old_alt = make_chooser("crypto", h, q, h[BTC], True)
    state = StrategyState()
    events: list[dict[str, Any]] = []
    grid = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                         freq="15min", inclusive="left")
    for when in grid:
        daily = when.hour == 0 and when.minute == 0
        core_check = when.minute == 0 and when.hour % 4 == 0
        signal_symbols = SYMBOLS if daily else ((BTC,) if core_check else ())
        hourly = {}
        for symbol in signal_symbols:
            row = h[symbol].loc[when]
            hourly[symbol] = HourSignal.from_mapping(row)
        close_symbols = set(SYMBOLS) if daily else {BTC, *state.alt_held}
        closes = {symbol: float(q[symbol].at[when, "close"]) for symbol in close_symbols}
        decision = advance(state, when.to_pydatetime(), hourly, closes, config)
        state = decision.next_state
        if verify_legacy:
            legacy_core = old_core("guarded", when, h, q,
                                   {s: 1.0 for s in old_core_held})
            legacy_alt = old_alt("relative_crypto", when, h, q,
                                 {s: 1.0 for s in old_alt_held})
            old_core_held = set(legacy_core)
            old_alt_held = set(legacy_alt)
            if old_core_held != ({BTC} if state.btc_held else set()) or old_alt_held != set(state.alt_held):
                raise AssertionError(f"strategy parity mismatch at {when}")
        if decision.intents:
            events.append(decision.to_dict())
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="emit signal intents from local Binance archives")
    replay.add_argument("--start", default="2026-09-15")
    replay.add_argument("--end", default="2026-09-29", help="exclusive UTC date")
    replay.add_argument("--months", default="2026-05,2026-06,2026-07,2026-08,2026-09")
    replay.add_argument("--verify-legacy", action="store_true")
    replay.add_argument("--rank-exit-confirmation-days", type=int, default=1)
    replay.add_argument("--structure-exit-confirmation-days", type=int, default=1)
    replay.add_argument("--output", type=Path, help="write JSONL; otherwise print to stdout")
    decide = sub.add_parser("decide", help="turn a closed-bar JSON snapshot into intents")
    decide.add_argument("--snapshot", type=Path, required=True)
    decide.add_argument("--state", type=Path, help="confirmed prior state; defaults to flat")
    decide.add_argument("--output", type=Path, help="write result JSON; otherwise print")
    decide.add_argument("--rank-exit-confirmation-days", type=int, default=1)
    decide.add_argument("--structure-exit-confirmation-days", type=int, default=1)
    args = parser.parse_args()
    config = StrategyConfig(args.rank_exit_confirmation_days,
                            args.structure_exit_confirmation_days)
    if args.command == "replay":
        events = _replay(args.start, args.end, set(args.months.split(",")),
                         args.verify_legacy, config)
        payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
        if args.output:
            args.output.write_text(payload)
        else:
            print(payload, end="")
        print(f"{len(events)} decision bars with order intents", file=__import__("sys").stderr)
    else:
        raw = json.loads(args.snapshot.read_text())
        state = StrategyState.from_mapping(json.loads(args.state.read_text())) if args.state else StrategyState()
        hourly = {symbol: HourSignal.from_mapping(row)
                  for symbol, row in raw.get("hourly", {}).items()}
        decision = advance(state, raw["when"], hourly, raw["close_15m"], config)
        payload = json.dumps(decision.to_dict(), indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.write_text(payload)
        else:
            print(payload, end="")


if __name__ == "__main__":
    main()
