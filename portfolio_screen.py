#!/usr/bin/env python3
"""Compare predeclared allocations of the original seven-symbol CW strategies."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import backtest as bt
from research_v2 import WINDOWS

MIXES = {
    "cw_30safe_70break": {"conservative": 0.30, "balanced": 0.00, "aggressive": 0.70, "btc": 0.00},
    "cw_25safe_15rotate_60break": {"conservative": 0.25, "balanced": 0.15, "aggressive": 0.60, "btc": 0.00},
    "cw_40safe_20rotate_40break": {"conservative": 0.40, "balanced": 0.20, "aggressive": 0.40, "btc": 0.00},
    "btc_20safe_20break_60": {"conservative": 0.20, "balanced": 0.00, "aggressive": 0.60, "btc": 0.20},
    "btc_30break_70": {"conservative": 0.00, "balanced": 0.00, "aggressive": 0.70, "btc": 0.30},
}


def btc_curve(start: str, end: str, minutes: dict, seconds: bt.SecondPrices | None,
              extra_bps: float = 2.0) -> np.ndarray:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    if seconds is None:
        raw = float(minutes["BTCUSDT"].at[start_ts, "open"])
    else:
        raw = seconds.get("BTCUSDT", start_ts + pd.Timedelta(seconds=1))
    entry = raw * (1 + extra_bps / 10000)
    units = 100_000 / (entry * (1 + bt.FEE))
    grid = pd.date_range(start_ts, end_ts, freq="15min", inclusive="left")
    # A 15-minute candle labelled t closes at t; at the beginning of the
    # window, the minute bar ending at t is part of the prior day.
    close15 = bt.candles(minutes["BTCUSDT"], "15min").close
    values = np.array([units * float(close15.at[t]) for t in grid])
    terminal_mid = float(minutes["BTCUSDT"].at[end_ts - pd.Timedelta(minutes=1), "close"])
    terminal = units * terminal_mid * (1 - extra_bps / 10000) * (1 - bt.FEE)
    return np.append(values, terminal)


def combine(legs: dict, btc: np.ndarray, weights: dict) -> dict:
    arrays = {s: np.array([row["equity"] for row in run.equity]) for s, run in legs.items()}
    arrays["btc"] = btc
    values = sum(weights[name] * arrays[name] for name in weights)
    peak = np.maximum.accumulate(values)
    return {"return_pct": float(100 * (values[-1] / 100_000 - 1)),
            "max_drawdown_pct": float(100 * np.min(values / peak - 1)),
            "estimated_fees_usd": float(sum(weights.get(name, 0) * legs[name].metrics["fees_usd"] for name in legs))}


def main() -> None:
    minutes = {s: bt.load_minutes(s) for s in bt.SYMBOLS}
    feats = {s: bt.build_features(minutes[s]) for s in bt.SYMBOLS}
    h = {s: feats[s][0] for s in bt.SYMBOLS}
    q = {s: feats[s][1] for s in bt.SYMBOLS}
    report = {"mixes": MIXES, "windows": []}
    for start, end in WINDOWS:
        exact = (start, end) == WINDOWS[-1]
        print(start, "1s" if exact else "1m proxy", flush=True)
        seconds = bt.SecondPrices() if exact else None
        legs = {name: bt.simulate(name, start, end, h, q, minutes, seconds)
                for name in ("conservative", "balanced", "aggressive")}
        btc = btc_curve(start, end, minutes, seconds)
        mixes = {name: combine(legs, btc, weights) for name, weights in MIXES.items()}
        report["windows"].append({"start": start, "end_exclusive": end,
                                  "execution": "1s" if exact else "1m proxy",
                                  "legs": {name: leg.metrics for name, leg in legs.items()},
                                  "btc_return_pct": float(100 * (btc[-1] / 100_000 - 1)),
                                  "mixes": mixes})
        print({name: round(row["return_pct"], 3) for name, row in mixes.items()}, flush=True)
    start, end = WINDOWS[-1]
    seconds = bt.SecondPrices()
    legs = {name: bt.simulate(name, start, end, h, q, minutes, seconds, extra_bps=7)
            for name in ("conservative", "balanced", "aggressive")}
    btc = btc_curve(start, end, minutes, seconds, extra_bps=7)
    report["validation_cost_stress"] = {name: combine(legs, btc, weights) for name, weights in MIXES.items()}
    (bt.ROOT / "portfolio_screen.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Wrote portfolio_screen.json", flush=True)


if __name__ == "__main__":
    main()
