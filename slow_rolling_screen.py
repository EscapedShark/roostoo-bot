#!/usr/bin/env python3
"""Lower-turnover Chan/Wyckoff rolling, checked on early and late windows."""
from __future__ import annotations

import gc
import json

import numpy as np
import pandas as pd

import backtest as bt
from download_stock_data import symbols as stock_symbols
from download_v2_data import SYMBOLS as CRYPTO_SYMBOLS
from portfolio_screen import combine
from rolling_screen import prepare
from trail_core import make_trailing_chooser

EARLY = [
    ("2026-01-15", "2026-01-29"), ("2026-02-01", "2026-02-15"),
    ("2026-02-15", "2026-03-01"), ("2026-03-01", "2026-03-15"),
    ("2026-03-15", "2026-03-29"), ("2026-04-01", "2026-04-15"),
    ("2026-04-15", "2026-04-29"),
]
LATE = [
    ("2026-06-01", "2026-06-15"), ("2026-06-15", "2026-06-29"),
    ("2026-07-01", "2026-07-15"), ("2026-07-15", "2026-07-29"),
    ("2026-08-01", "2026-08-15"), ("2026-08-15", "2026-08-29"),
    ("2026-09-01", "2026-09-15"), ("2026-09-15", "2026-09-29"),
]


def make_chooser(kind: str, h: dict, q: dict, btc_h: pd.DataFrame,
                 relative: bool = False, use_chan: bool = True,
                 use_wyckoff: bool = True):
    assert kind in ("crypto", "stock")
    min_liq = 20_000_000 if kind == "crypto" else 2_000_000
    min_ret7 = 0.05 if kind == "crypto" else 0.03
    stop = 0.12 if kind == "crypto" else 0.08
    peaks: dict[str, float] = {}
    entry_time: dict[str, pd.Timestamp] = {}
    cooldown: dict[str, pd.Timestamp] = {}

    def decide(_strategy: str, when: pd.Timestamp, _h: dict, _q: dict,
               current: dict[str, float]) -> dict[str, float]:
        stopped = set()
        for symbol in current:
            if when not in q[symbol].index:
                continue
            close = float(q[symbol].at[when, "close"])
            peaks[symbol] = max(close, peaks.get(symbol, close))
            if close < peaks[symbol] * (1 - stop):
                stopped.add(symbol)
                cooldown[symbol] = when + pd.Timedelta(hours=24)
                peaks.pop(symbol, None)
                entry_time.pop(symbol, None)
        active = set(current) - stopped
        if when.hour != 0 or when.minute != 0:
            return {s: 0.5 for s in active}
        btc_ret7 = float(btc_h.at[when, "ret7d"]) if when in btc_h.index else np.nan
        candidates = []
        for symbol, frame in h.items():
            if when not in frame.index or when not in q[symbol].index:
                continue
            r = frame.loc[when]
            if not np.isfinite(r.liq24) or r.liq24 < min_liq:
                continue
            if not np.isfinite(r.ret7d) or not np.isfinite(r.ret72):
                continue
            if (use_chan and (r.chan < 0 or r.zone < 0)) or (use_wyckoff and r.wyckoff < 0):
                continue
            candidates.append((float(r.ret7d + 0.3 * r.ret72), symbol))
        ranked = [s for _, s in sorted(candidates, reverse=True)]
        selected = []
        for symbol in active:
            valid = symbol in ranked[:5]
            if symbol in h and when in h[symbol].index:
                r = h[symbol].loc[when]
                valid = valid or (when - entry_time.get(symbol, when) < pd.Timedelta(hours=48)
                                  and (not use_chan or (r.chan >= 0 and r.zone >= 0))
                                  and (not use_wyckoff or r.wyckoff >= 0))
                valid = valid and r.ret7d > -0.02
                if relative and np.isfinite(btc_ret7):
                    valid = valid and r.ret7d - btc_ret7 > -0.02
            if valid:
                selected.append(symbol)
            else:
                peaks.pop(symbol, None)
                entry_time.pop(symbol, None)
        selected = selected[:2]
        for symbol in ranked:
            if len(selected) == 2:
                break
            if symbol in selected or symbol in stopped or when < cooldown.get(symbol, when):
                continue
            r = h[symbol].loc[when]
            positive_structure = ((use_chan and r.chan == 1) or
                                  (use_wyckoff and r.wyckoff == 1) or
                                  (not use_chan and not use_wyckoff))
            if (r.ret7d >= min_ret7 and r.ret72 >= 0.01 and
                    positive_structure and
                    (not relative or (np.isfinite(btc_ret7) and r.ret7d - btc_ret7 >= 0.05))):
                selected.append(symbol)
                entry_time[symbol] = when
                peaks[symbol] = float(q[symbol].at[when, "close"])
        return {s: 0.5 for s in selected}

    return decide


def segment(months: set[str], windows: list[tuple[str, str]], with_stocks: bool) -> list[dict]:
    print("Preparing crypto", sorted(months), flush=True)
    crypto_m, crypto_h, crypto_q = prepare(CRYPTO_SYMBOLS, months)
    for s in CRYPTO_SYMBOLS:
        crypto_h[s]["ret7d"] = crypto_h[s].close / crypto_h[s].close.shift(168) - 1
    stock_m = stock_h = stock_q = None
    stocks = stock_symbols()
    if with_stocks:
        print("Preparing stocks", flush=True)
        stock_m, stock_h, stock_q = prepare(stocks, months)
        for s in stocks:
            stock_h[s]["ret7d"] = stock_h[s].close / stock_h[s].close.shift(168) - 1
    rows = []
    for start, end in windows:
        exact = start == "2026-09-15"
        bt.SYMBOLS = CRYPTO_SYMBOLS
        secs = bt.SecondPrices() if exact else None
        core = bt.simulate("core", start, end, crypto_h, crypto_q, crypto_m, secs,
                           chooser=make_trailing_chooser(0.05))
        absolute = bt.simulate("slow_crypto", start, end, crypto_h, crypto_q, crypto_m, secs,
                               chooser=make_chooser("crypto", crypto_h, crypto_q, crypto_h["BTCUSDT"]))
        relative = bt.simulate("relative_crypto", start, end, crypto_h, crypto_q, crypto_m, secs,
                               chooser=make_chooser("crypto", crypto_h, crypto_q, crypto_h["BTCUSDT"], True))
        relative_cost12 = bt.simulate(
            "relative_crypto_cost12", start, end, crypto_h, crypto_q, crypto_m,
            bt.SecondPrices() if exact else None, extra_bps=12,
            chooser=make_chooser("crypto", crypto_h, crypto_q, crypto_h["BTCUSDT"], True))
        legs = {"core": core, "slow_crypto": absolute, "relative_crypto": relative}
        if with_stocks:
            bt.SYMBOLS = stocks
            stock_sec = bt.SecondPrices() if exact else None
            stock = bt.simulate("slow_stock", start, end, stock_h, stock_q, stock_m,
                                stock_sec, chooser=make_chooser("stock", stock_h, stock_q,
                                                               crypto_h["BTCUSDT"]))
            legs["slow_stock"] = stock
        curve = np.array([v["equity"] for v in core.equity])
        mixes = {
            "core80_slow20": combine(legs, curve, {"core": 0.8, "slow_crypto": 0.2}),
        }
        for core_weight in (0.0, 0.2, 0.4, 0.6, 0.8):
            label = f"core{round(100 * core_weight)}_relative{round(100 * (1 - core_weight))}"
            mixes[label] = combine(legs, curve, {"core": core_weight,
                                                  "relative_crypto": 1 - core_weight})
        if with_stocks:
            mixes["core60_crypto20_stock20"] = combine(
                legs, curve, {"core": 0.6, "relative_crypto": 0.2, "slow_stock": 0.2})
        row = {"start": start, "end_exclusive": end, "execution": "1s" if exact else "1m proxy",
               "legs": {k: v.metrics for k, v in legs.items()}, "mixes": mixes,
               "relative_crypto_cost12": relative_cost12.metrics}
        if exact:
            row["exact_second_fraction"] = {
                "crypto": float(np.mean(np.array(secs.waits) == 0)) if secs.waits else None,
                "stock": float(np.mean(np.array(stock_sec.waits) == 0)) if stock_sec.waits else None}
        rows.append(row)
        print(start, {k: round(v.metrics["return_pct"], 2) for k, v in legs.items()}, flush=True)
    del crypto_m, crypto_h, crypto_q, stock_m, stock_h, stock_q
    gc.collect()
    return rows


def main() -> None:
    output = {"rules": {"evaluation": "daily UTC midnight; closed 15m stops",
                         "max_positions": 2, "weight_per_position": 0.5,
                         "min_24h_quote_crypto": 20_000_000,
                         "min_24h_quote_stock": 2_000_000,
                         "crypto_trailing_stop": 0.12, "stock_trailing_stop": 0.08,
                         "fee_per_side": 0.001, "extra_bps_per_side": 2},
              "windows": []}
    output["windows"] += segment({f"2026-{m:02d}" for m in range(1, 5)}, EARLY, False)
    output["windows"] += segment({f"2026-{m:02d}" for m in range(5, 10)}, LATE, True)
    (bt.ROOT / "slow_rolling_screen.json").write_text(json.dumps(output, indent=2) + "\n")
    print("Wrote slow_rolling_screen.json", flush=True)


if __name__ == "__main__":
    main()
