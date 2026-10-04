from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import replace
from logging.handlers import RotatingFileHandler

from competition_strategy import advance, iso, utc_time
from protected_strategy import (GuardConfig, ProtectedDecision, ProtectedState,
                                _valid_bar_snapshot, on_bar, on_tick)

from .api import APIError
from .execution import Blocked, Executor
from .feed import boundary
from .store import dec, reconcile_wallet


def logger(config):
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"roostoo.{config.mode}")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            config.log_dir / f"{config.mode}.jsonl", maxBytes=10_000_000, backupCount=5)):
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)

    def emit(kind, **fields):
        from datetime import datetime, timezone
        log.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "kind": kind,
                             "mode": config.mode, **fields}, ensure_ascii=False, default=str))
    return emit


def initialize(config, api, store, rules):
    existing = store.account()
    if existing:
        store.initialize(config.fingerprint, 1)  # Validate; never redivide capital.
        return
    cash = config.initial_cash
    if config.live:
        wallet = api.balance()
        if api.pending_count() or api.short_positions().get("Positions"):
            raise Blocked("initial account has pending orders or short positions")
        if api.orders(limit="1"):
            raise Blocked("account has historical orders but no local ledger; provide a fresh key or restore its state")
        cash = float(dec(wallet.get("USD", {}).get("Free", 0)))
        if cash <= 0:
            raise Blocked("initial USD cash is missing")
        # Every nonzero coin or locked amount must be accounted for, never ignored.
        from .store import initial_ledger
        reconcile_wallet(initial_ledger(cash), wallet, rules)
    store.initialize(config.fingerprint, cash)


def decide(config, state, when, snapshot, quotes, equity, evaluated):
    if config.variant == "protected":
        return on_bar(state, when, snapshot.hourly, snapshot.closes, snapshot.hour_at, snapshot.close_at,
                      quotes, equity, evaluated, snapshot.previous, config=config.guard,
                      strategy_config=config.strategy)
    problems = _valid_bar_snapshot(when, state.strategy, snapshot.hourly, snapshot.closes,
                                   snapshot.hour_at, snapshot.close_at)
    if problems or equity is None or (evaluated - when).total_seconds() > 30:
        # Keep closed-bar stop handling on broken inputs without adding macro,
        # intrabar or portfolio stops to the baseline research variant.
        guard = replace(config.guard, cpi_pre_minutes=0, cpi_post_minutes=0, max_portfolio_drawdown=1)
        return on_bar(state, when, snapshot.hourly, snapshot.closes, snapshot.hour_at, snapshot.close_at,
                      quotes, equity, evaluated, snapshot.previous, config=guard,
                      strategy_config=config.strategy)
    result = advance(state.strategy, when, snapshot.hourly, snapshot.closes, config.strategy)
    candidate = ProtectedState.from_mapping(state.to_dict())
    candidate.strategy = result.next_state
    return ProtectedDecision(result.decision_at, result.execute_not_before, result.intents, candidate)


def restore_missed_peaks(state, feed, when):
    """Retain intervening completed-bar highs after downtime; never replay trades."""
    from datetime import timedelta
    import pandas as pd
    from competition_strategy import BTC
    out = ProtectedState.from_mapping(state.to_dict())
    base = out.strategy
    if not base.last_bar or (when - utc_time(base.last_bar)).total_seconds() <= 900:
        return out
    expected = pd.date_range(utc_time(base.last_bar) + timedelta(minutes=15), when, freq="15min")
    held = ([BTC] if base.btc_held else []) + base.alt_held
    for symbol in held:
        bars = feed.frame(symbol, "15m", int(when.timestamp() * 1000))
        window = bars.loc[bars.index > utc_time(base.last_bar)]
        if not window.index.equals(expected):
            raise Blocked(f"incomplete missed-bar history for held {symbol}; fresh ticks still handle exits")
        peak = float(window.close.max())
        if symbol == BTC:
            base.btc_peak_close = max(float(base.btc_peak_close), peak)
        else:
            base.alt_peak_close[symbol] = max(base.alt_peak_close[symbol], peak)
    return out


class Runner:
    def __init__(self, config, api, store, feed, log):
        self.cfg, self.api, self.store, self.feed, self.log = config, api, store, feed, log
        self.rules = api.exchange_info()
        initialize(config, api, store, self.rules)
        self.executor = Executor(config, api, store, self.rules, log)
        self.last_recovery = 0.0
        self.last_rules = time.monotonic()
        self.last_heartbeat = 0.0

    def submit_decision(self, key, decision, references=None):
        if key.startswith("tick:") and not decision.intents:
            self.store.save_idle_tick(decision, iso(self.api.now()))
            return
        if not self.store.prepare(key, decision, iso(self.api.now())):
            return
        for row in self.store.orders(key):
            if references and row["payload"]["intent"]["symbol"] in references:
                self.store.update_order(row, "planned", reference_close=references[row["payload"]["intent"]["symbol"]])
        self.log("decision", decision=decision.to_dict())
        self.executor.run_pending()

    def tick(self):
        if self.store.pending():
            if time.monotonic() - self.last_recovery >= 8:
                self.last_recovery = time.monotonic()
                self.executor.run_pending()
            return
        ex = self.executor
        if ex.balance_at is None or (self.api.now() - ex.balance_at).total_seconds() >= 30:
            ex.validate_balance()
        if self.cfg.variant == "protected":
            state = ProtectedState.from_mapping(self.store.account()["state"])
            now = self.api.now()
            result = on_tick(state, now, ex.quotes, ex.equity(), self.cfg.guard)
            self.submit_decision("tick:" + iso(now), result)

    def run(self, once=False):
        ex = self.executor
        future, target, last_refresh = None, None, None
        failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            while True:
                started = time.monotonic()
                try:
                    if self.cfg.end_at and self.api.now() >= utc_time(self.cfg.end_at):
                        self.log("competition_end", end_at=self.cfg.end_at,
                                 pending_batch=self.store.pending()["id"] if self.store.pending() else None)
                        return
                    if self.api.clock() - self.api.last_sync > 300:
                        self.api.sync_time()
                    if started - self.last_rules > 3600:
                        self.rules = self.api.exchange_info()
                        ex.rules = self.rules
                        self.last_rules = started
                    ex.refresh_quotes()
                    if not self.store.pending() and ex.balance_at is None and self.cfg.live:
                        recent = self.api.orders(limit="100")
                        foreign = {str(row["OrderID"]) for row in recent} - self.store.known_ids()
                        if foreign:
                            raise Blocked("untracked account orders found; restore the original ledger")
                        if self.api.pending_count() or self.api.short_positions().get("Positions"):
                            raise Blocked("untracked pending orders or short positions found")
                    self.tick()
                    now, bar = self.api.now(), boundary(self.api.now())
                    if future and future.done():
                        errors = future.result()
                        self.log("feed_refresh", boundary=iso(target), errors=errors)
                        future, last_refresh = None, target
                    if future is None and (last_refresh is None or bar > last_refresh):
                        target = bar
                        future = pool.submit(self.feed.refresh, now)
                    state = ProtectedState.from_mapping(self.store.account()["state"])
                    due = state.strategy.last_bar is None or bar > utc_time(state.strategy.last_bar)
                    ready = last_refresh is not None and last_refresh >= bar
                    expired = (now - bar).total_seconds() >= 25
                    if due and not self.store.pending() and (ready or expired) and (not once or ready):
                        snapshot = self.feed.snapshot(bar)
                        if state.strategy.last_bar and (bar - utc_time(state.strategy.last_bar)).total_seconds() > 900:
                            self.log("missed_bars", previous=state.strategy.last_bar, resumed_at=iso(bar))
                            state = restore_missed_peaks(state, self.feed, bar)
                            idle = ProtectedDecision(iso(now), iso(now), (), state)
                            self.store.save_idle_tick(idle, iso(now))
                        result = decide(self.cfg, state, bar, snapshot, ex.quotes, ex.equity(), self.api.now())
                        self.submit_decision("bar:" + iso(bar), result, snapshot.closes)
                        if once:
                            self.log("once_complete", simulated=not self.cfg.live)
                            return
                    failures = 0
                    if started - self.last_heartbeat >= 60:
                        self.log("heartbeat", equity=ex.equity(), last_bar=self.store.account()["state"]["strategy"]["last_bar"],
                                 pending=self.store.pending()["id"] if self.store.pending() else None,
                                 feed_ready_symbols=len(self.feed.frames), variant=self.cfg.variant)
                        self.last_heartbeat = started
                except (APIError, Blocked, ValueError, OSError) as exc:
                    failures += 1
                    self.log("execution_paused", error=str(exc), failures=failures,
                             pending=self.store.pending()["id"] if self.store.pending() else None)
                    if once:
                        raise
                elapsed = time.monotonic() - started
                time.sleep(max(0.1, min(20, self.cfg.poll_seconds * (2 ** min(failures, 2))) - elapsed))
