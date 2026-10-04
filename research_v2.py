#!/usr/bin/env python3
"""Fixed-allocation Chan/Wyckoff portfolio research on 21 eligible coins."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
from download_v2_data import SYMBOLS

ROOT = Path(__file__).resolve().parent
WINDOWS = [
    ("2026-06-01", "2026-06-15"), ("2026-06-15", "2026-06-29"),
    ("2026-07-01", "2026-07-15"), ("2026-07-15", "2026-07-29"),
    ("2026-08-01", "2026-08-15"), ("2026-08-15", "2026-08-29"),
    ("2026-09-01", "2026-09-15"), ("2026-09-15", "2026-09-29"),
]
LEGS = ("core", "rotation", "breakout")
MIXES = {
    "mix_40_30_30": {"core": 0.40, "rotation": 0.30, "breakout": 0.30},
    "mix_25_25_50": {"core": 0.25, "rotation": 0.25, "breakout": 0.50},
    "mix_50_00_50": {"core": 0.50, "rotation": 0.00, "breakout": 0.50},
    "mix_50_50_00": {"core": 0.50, "rotation": 0.50, "breakout": 0.00},
    "mix_30_00_70": {"core": 0.30, "rotation": 0.00, "breakout": 0.70},
}
MIN_DAILY_QUOTE = 20_000_000


def decide(strategy: str, when: pd.Timestamp, h: dict, q: dict,
           current: dict[str, float]) -> dict[str, float]:
    if strategy in ("core", "rotation"):
        if when.minute != 0 or when.hour % 4 != 0:
            return current
        if strategy == "core":
            r = h["BTCUSDT"].loc[when]
            active = "BTCUSDT" in current
            # The position continues through neutral observations. A bearish
            # structure plus trend/volume weakness is needed to exit.
            if active:
                exit_now = (r.chan == -1 and r.ema20 < r.ema60) or (r.wyckoff == -1 and r.ret24 < 0)
                return {} if exit_now else {"BTCUSDT": 1.0}
            enter = r.ema20 > r.ema60 and r.ret24 > 0 and r.chan >= 0 and r.zone >= 0 and r.wyckoff >= 0
            return {"BTCUSDT": 1.0} if enter else {}
        # Sticky rotation: retain qualifying positions instead of exchanging
        # them merely because another asset has a slightly higher score.
        ranked = []
        eligible = set()
        for symbol, frame in h.items():
            if when not in frame.index:
                continue
            r = frame.loc[when]
            valid = (r.liq24 >= MIN_DAILY_QUOTE and r.chan == 1 and r.zone >= 0 and
                     r.wyckoff >= 0 and r.ret6 > 0 and r.ret24 > 0 and np.isfinite(r.vol24))
            if valid:
                eligible.add(symbol)
                ranked.append((float((r.ret6 + 0.5 * r.ret24) / r.vol24), symbol))
        keep = [s for s in current if s in eligible]
        chosen = keep[:4]
        for _, symbol in sorted(ranked, reverse=True):
            if len(chosen) == 4:
                break
            if symbol not in chosen:
                chosen.append(symbol)
        return {s: 0.25 for s in chosen}
    # Breakout leg; its signal frequency is 15 minutes but its structural
    # filter uses only the last completed hourly bar.
    ranked = []
    for symbol, frame in q.items():
        if when not in frame.index:
            continue
        r = frame.loc[when]
        if not np.isfinite(r.liq24) or r.liq24 < MIN_DAILY_QUOTE:
            continue
        keep = symbol in current and r.close >= r.prior_lo8 and r.chan == 1 and r.wyckoff >= 0
        enter = r.chan == 1 and r.wyckoff == 1 and r.close > r.prior_hi32 and r.vol_ratio > 1.5
        if keep or enter:
            score = float((r.close / r.prior_hi32 - 1) * min(r.vol_ratio, 5)) if pd.notna(r.prior_hi32) else 0.0
            ranked.append((score + (0.01 if keep else 0), symbol))
    return {symbol: 0.35 for _, symbol in sorted(ranked, reverse=True)[:2]}


def combine(legs: dict[str, bt.Result], weights: dict[str, float]) -> dict:
    assert abs(sum(weights.values()) - 1) < 1e-10
    curves = {name: np.array([row["equity"] for row in result.equity]) for name, result in legs.items()}
    combined = sum(weights.get(name, 0) * curve for name, curve in curves.items())
    peak = np.maximum.accumulate(combined)
    return {
        "return_pct": float(100 * (combined[-1] / 100_000 - 1)),
        "max_drawdown_pct": float(100 * np.min(combined / peak - 1)),
        "component_weights": weights,
        "estimated_order_count_before_netting": sum(legs[name].metrics["trades"] for name in legs if weights.get(name, 0) > 0),
        "fees_usd": float(sum(weights.get(name, 0) * leg.metrics["fees_usd"] for name, leg in legs.items())),
        "turnover_x": float(sum(weights.get(name, 0) * leg.metrics["turnover_x"] for name, leg in legs.items())),
    }


def main() -> None:
    bt.SYMBOLS = SYMBOLS
    print("Loading 21 symbols", flush=True)
    months = {f"2026-{m:02d}" for m in range(5, 10)}
    minutes = {s: bt.load_minutes(s, months=months) for s in SYMBOLS}
    feats = {s: bt.build_features(minutes[s]) for s in SYMBOLS}
    h = {s: feats[s][0] for s in SYMBOLS}
    q = {s: feats[s][1] for s in SYMBOLS}
    for s in SYMBOLS:
        h[s]["liq24"] = h[s].quote_volume.rolling(24, min_periods=24).sum()
        q[s]["liq24"] = h[s].liq24.reindex(q[s].index, method="ffill")
    results = {"universe": SYMBOLS, "quote_volume_filter_usd_24h": MIN_DAILY_QUOTE,
               "mixes": MIXES, "windows": [],
               "selection_protocol": "Select on first seven 14-day windows; latest window is validation, not a guarantee of future performance."}
    for start, end in WINDOWS:
        exact = (start, end) == WINDOWS[-1]
        print(f"Window {start}..{end}, 1-second execution={exact}", flush=True)
        seconds = bt.SecondPrices() if exact else None
        legs = {name: bt.simulate(name, start, end, h, q, minutes, seconds,
                                  chooser=decide) for name in LEGS}
        mixes = {name: combine(legs, weights) for name, weights in MIXES.items()}
        row = {"start": start, "end_exclusive": end, "execution": "1s" if exact else "1m proxy",
               "legs": {name: run.metrics for name, run in legs.items()},
               "mixes": mixes,
               "btc_buy_hold_return_pct": bt.btc_buy_hold(start, end, minutes, seconds)}
        print({name: round(leg.metrics["return_pct"], 3) for name, leg in legs.items()}, flush=True)
        print({name: round(result["return_pct"], 3) for name, result in mixes.items()}, flush=True)
        results["windows"].append(row)
        if exact:
            results["validation_exact_second_fraction"] = float(np.mean(np.array(seconds.waits) == 0))
            for name, run in legs.items():
                pd.DataFrame(run.trades).to_csv(ROOT / f"v2_trades_{name}.csv", index=False)
                pd.DataFrame(run.equity).to_csv(ROOT / f"v2_equity_{name}.csv", index=False)
    # Cost stress on the most recent exact-second window.
    start, end = WINDOWS[-1]
    seconds = bt.SecondPrices()
    stressed = {name: bt.simulate(name, start, end, h, q, minutes, seconds,
                                  extra_bps=7, chooser=decide) for name in LEGS}
    results["validation_cost_stress"] = {
        "legs": {name: leg.metrics for name, leg in stressed.items()},
        "mixes": {name: combine(stressed, weights) for name, weights in MIXES.items()},
    }
    (ROOT / "results_v2.json").write_text(json.dumps(results, indent=2) + "\n")
    print("Wrote results_v2.json", flush=True)


if __name__ == "__main__":
    main()
