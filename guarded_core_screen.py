#!/usr/bin/env python3
"""Evaluate a slow BTC core with Chan/Wyckoff bearish veto plus CW breakout."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import backtest as bt
from portfolio_screen import btc_curve, combine
from research_v2 import WINDOWS

MIXES = {
    "guarded100": {"guarded": 1.00, "aggressive": 0.00},
    "guarded80_break20": {"guarded": 0.80, "aggressive": 0.20},
    "guarded70_break30": {"guarded": 0.70, "aggressive": 0.30},
    "guarded30_break70": {"guarded": 0.30, "aggressive": 0.70},
    "guarded50_break50": {"guarded": 0.50, "aggressive": 0.50},
    "guarded30_safe20_break50": {"guarded": 0.30, "conservative": 0.20, "aggressive": 0.50},
}


def guarded_chooser(strategy: str, when: pd.Timestamp, h: dict, q: dict,
                    current: dict[str, float]) -> dict[str, float]:
    if when.minute != 0 or when.hour % 4 != 0:
        return current
    r = h["BTCUSDT"].loc[when]
    if "BTCUSDT" in current:
        bearish = ((r.ret7d < -0.02 and r.chan == -1) or
                   (r.ret7d < -0.04 and r.wyckoff == -1))
        return {} if bearish else {"BTCUSDT": 1.0}
    bullish = r.ret7d > 0 and r.chan >= 0 and r.wyckoff >= 0
    return {"BTCUSDT": 1.0} if bullish else {}


def main() -> None:
    minutes = {s: bt.load_minutes(s) for s in bt.SYMBOLS}
    feats = {s: bt.build_features(minutes[s]) for s in bt.SYMBOLS}
    h = {s: feats[s][0] for s in bt.SYMBOLS}
    q = {s: feats[s][1] for s in bt.SYMBOLS}
    h["BTCUSDT"]["ret7d"] = h["BTCUSDT"].close / h["BTCUSDT"].close.shift(7 * 24) - 1
    report = {"mixes": MIXES, "guarded_core_rule": "4h decision; 7d return and confirmed 1h Chan/Wyckoff exit/reentry", "windows": []}
    for start, end in WINDOWS:
        exact = (start, end) == WINDOWS[-1]
        secs = bt.SecondPrices() if exact else None
        guarded = bt.simulate("guarded", start, end, h, q, minutes, secs, chooser=guarded_chooser)
        aggressive = bt.simulate("aggressive", start, end, h, q, minutes, secs)
        conservative = bt.simulate("conservative", start, end, h, q, minutes, secs)
        btc = btc_curve(start, end, minutes, secs)
        legs = {"guarded": guarded, "aggressive": aggressive, "conservative": conservative}
        mixes = {name: combine(legs, btc, weights) for name, weights in MIXES.items()}
        report["windows"].append({"start": start, "end_exclusive": end,
                                  "execution": "1s" if exact else "1m proxy",
                                  "legs": {name: run.metrics for name, run in legs.items()},
                                  "mixes": mixes, "btc_return_pct": float(100 * (btc[-1] / 100_000 - 1))})
        print(start, {name: round(row["return_pct"], 3) for name, row in mixes.items()}, flush=True)
    start, end = WINDOWS[-1]
    secs = bt.SecondPrices()
    legs = {"guarded": bt.simulate("guarded", start, end, h, q, minutes, secs, extra_bps=7, chooser=guarded_chooser),
            "aggressive": bt.simulate("aggressive", start, end, h, q, minutes, secs, extra_bps=7),
            "conservative": bt.simulate("conservative", start, end, h, q, minutes, secs, extra_bps=7)}
    btc = btc_curve(start, end, minutes, secs, extra_bps=7)
    report["validation_cost_stress"] = {name: combine(legs, btc, weights) for name, weights in MIXES.items()}
    (bt.ROOT / "guarded_core_screen.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Wrote guarded_core_screen.json", flush=True)


if __name__ == "__main__":
    main()
