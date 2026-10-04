#!/usr/bin/env python3
"""Frozen guarded-core allocations on previously unused March-April history."""
from __future__ import annotations

import json

import pandas as pd

import backtest as bt
from guarded_core_screen import MIXES, guarded_chooser
from portfolio_screen import btc_curve, combine

WINDOWS = [
    ("2026-03-01", "2026-03-15"), ("2026-03-15", "2026-03-29"),
    ("2026-04-01", "2026-04-15"), ("2026-04-15", "2026-04-29"),
]


def main() -> None:
    minutes = {s: bt.load_minutes(s) for s in bt.SYMBOLS}
    feats = {s: bt.build_features(minutes[s]) for s in bt.SYMBOLS}
    h = {s: feats[s][0] for s in bt.SYMBOLS}
    q = {s: feats[s][1] for s in bt.SYMBOLS}
    h["BTCUSDT"]["ret7d"] = h["BTCUSDT"].close / h["BTCUSDT"].close.shift(7 * 24) - 1
    report = {"frozen_mixes": MIXES, "windows": []}
    for start, end in WINDOWS:
        exact = (start, end) in (WINDOWS[1], WINDOWS[-1])
        secs = bt.SecondPrices() if exact else None
        legs = {
            "guarded": bt.simulate("guarded", start, end, h, q, minutes, secs, chooser=guarded_chooser),
            "aggressive": bt.simulate("aggressive", start, end, h, q, minutes, secs),
            "conservative": bt.simulate("conservative", start, end, h, q, minutes, secs),
        }
        btc = btc_curve(start, end, minutes, secs)
        mixes = {name: combine(legs, btc, weights) for name, weights in MIXES.items()}
        report["windows"].append({"start": start, "end_exclusive": end,
                                  "execution": "1s" if exact else "1m proxy",
                                  "legs": {name: run.metrics for name, run in legs.items()},
                                  "btc_return_pct": float(100 * (btc[-1] / 100_000 - 1)),
                                  "mixes": mixes})
        print(start, {name: round(row["return_pct"], 3) for name, row in mixes.items()}, flush=True)
        if exact:
            report.setdefault("exact_second_fraction", {})[start] = sum(wait == 0 for wait in secs.waits) / len(secs.waits)
            stressed = {
                "guarded": bt.simulate("guarded", start, end, h, q, minutes, secs, extra_bps=7, chooser=guarded_chooser),
                "aggressive": bt.simulate("aggressive", start, end, h, q, minutes, secs, extra_bps=7),
                "conservative": bt.simulate("conservative", start, end, h, q, minutes, secs, extra_bps=7),
            }
            btc_stressed = btc_curve(start, end, minutes, secs, extra_bps=7)
            report.setdefault("exact_cost_stress", {})[start] = {
                name: combine(stressed, btc_stressed, weights) for name, weights in MIXES.items()
            }
    (bt.ROOT / "guarded_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Wrote guarded_validation.json", flush=True)


if __name__ == "__main__":
    main()
