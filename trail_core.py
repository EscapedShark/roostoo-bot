#!/usr/bin/env python3
"""Evaluate a causal trailing stop on the guarded BTC core."""
from __future__ import annotations

import json

import pandas as pd

import backtest as bt
from guarded_core_screen import guarded_chooser
from research_v2 import WINDOWS as LATER_WINDOWS
from guarded_validation import WINDOWS as EARLIER_WINDOWS

WINDOWS = EARLIER_WINDOWS + LATER_WINDOWS
THRESHOLDS = (0.03, 0.04, 0.05)


def make_trailing_chooser(threshold: float):
    peak: float | None = None
    cooldown_until: pd.Timestamp | None = None

    def choose(strategy: str, when: pd.Timestamp, h: dict, q: dict,
               current: dict[str, float]) -> dict[str, float]:
        nonlocal peak, cooldown_until
        close = float(q["BTCUSDT"].at[when, "close"])
        if "BTCUSDT" in current:
            peak = max(close, peak if peak is not None else close)
            if close <= peak * (1 - threshold):
                peak = None
                cooldown_until = when + pd.Timedelta(hours=4)
                return {}
        else:
            peak = None
            if cooldown_until is not None and when < cooldown_until:
                return current
        target = guarded_chooser(strategy, when, h, q, current)
        if target is not current and "BTCUSDT" in target and "BTCUSDT" not in current:
            peak = close
        if target is not current and "BTCUSDT" not in target:
            peak = None
        return target

    return choose


def main() -> None:
    minutes = {s: bt.load_minutes(s) for s in bt.SYMBOLS}
    feats = {s: bt.build_features(minutes[s]) for s in bt.SYMBOLS}
    h = {s: feats[s][0] for s in bt.SYMBOLS}
    q = {s: feats[s][1] for s in bt.SYMBOLS}
    h["BTCUSDT"]["ret7d"] = h["BTCUSDT"].close / h["BTCUSDT"].close.shift(7 * 24) - 1
    rows = []
    for start, end in WINDOWS:
        exact = (start, end) in (EARLIER_WINDOWS[1], EARLIER_WINDOWS[-1], LATER_WINDOWS[-1])
        secs = bt.SecondPrices() if exact else None
        candidates = {"base": bt.simulate("guarded", start, end, h, q, minutes, secs, chooser=guarded_chooser)}
        for threshold in THRESHOLDS:
            candidates[f"trail_{int(threshold*100)}pct"] = bt.simulate(
                "guarded", start, end, h, q, minutes, secs, chooser=make_trailing_chooser(threshold))
        rows.append({"start": start, "end_exclusive": end,
                     "execution": "1s" if exact else "1m proxy",
                     "candidates": {name: run.metrics for name, run in candidates.items()},
                     "btc_buy_hold_return_pct": bt.btc_buy_hold(start, end, minutes, secs)})
        print(start, {name: round(run.metrics["return_pct"], 2) for name, run in candidates.items()}, flush=True)
    (bt.ROOT / "trail_core.json").write_text(json.dumps({"thresholds": THRESHOLDS, "windows": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
