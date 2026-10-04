#!/usr/bin/env python3
"""Predeclared altcoin and tokenized-stock rolling screen for Roostoo."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import backtest as bt
from download_stock_data import symbols as stock_symbols
from download_v2_data import SYMBOLS as CRYPTO_SYMBOLS
from portfolio_screen import combine
from trail_core import make_trailing_chooser

WINDOWS = [
    ("2026-07-01", "2026-07-15"), ("2026-07-15", "2026-07-29"),
    ("2026-08-01", "2026-08-15"), ("2026-08-15", "2026-08-29"),
    ("2026-09-01", "2026-09-15"), ("2026-09-15", "2026-09-29"),
]
HOLDOUT_START = "2026-09-15"


def prepare(symbols: tuple[str, ...], months: set[str] | None = None) -> tuple[dict, dict, dict]:
    minutes = {s: bt.load_minutes(s, months=months) for s in symbols}
    h, q = {}, {}
    for s in symbols:
        h[s], q[s] = bt.build_features(minutes[s])
        h[s]["ret72"] = h[s].close / h[s].close.shift(72) - 1
        h[s]["liq24"] = h[s].quote_volume.rolling(24, min_periods=24).sum()
    return minutes, h, q


def make_roller(kind: str, h: dict, q: dict):
    assert kind in ("crypto", "stock")
    min_liq = 20_000_000 if kind == "crypto" else 2_000_000
    min_ret24 = 0.02 if kind == "crypto" else 0.01
    min_ret72 = 0.03 if kind == "crypto" else 0.02
    peaks: dict[str, float] = {}
    cooldown: dict[str, pd.Timestamp] = {}

    def decide(_strategy: str, when: pd.Timestamp, _h: dict, _q: dict,
               current: dict[str, float]) -> dict[str, float]:
        stopped = set()
        for symbol in current:
            if when not in q[symbol].index:
                continue
            close = float(q[symbol].at[when, "close"])
            peaks[symbol] = max(close, peaks.get(symbol, close))
            if close < peaks[symbol] * 0.92:
                stopped.add(symbol)
                cooldown[symbol] = when + pd.Timedelta(hours=4)
                peaks.pop(symbol, None)
        active = set(current) - stopped
        if when.minute != 0 or when.hour % 4 != 0:
            return {s: 0.5 for s in active}
        candidates = []
        for symbol, frame in h.items():
            if when not in frame.index or when not in q[symbol].index:
                continue
            r = frame.loc[when]
            if not np.isfinite(r.liq24) or r.liq24 < min_liq:
                continue
            if not np.isfinite(r.ret24) or not np.isfinite(r.ret72):
                continue
            if r.chan < 0 or r.zone < 0 or r.wyckoff < 0:
                continue
            score = float(r.ret24 + 0.4 * r.ret72)
            candidates.append((score, symbol))
        ranked = [s for _, s in sorted(candidates, reverse=True)]
        keep = []
        for s in active:
            if s not in ranked[:4]:
                peaks.pop(s, None)
                continue
            r = h[s].loc[when]
            if r.ret24 <= -0.02:
                peaks.pop(s, None)
                continue
            keep.append(s)
        selected = keep[:2]
        for symbol in ranked:
            if len(selected) == 2:
                break
            if symbol in selected or symbol in stopped or when < cooldown.get(symbol, when):
                continue
            r = h[symbol].loc[when]
            if (r.ret24 >= min_ret24 and r.ret72 >= min_ret72 and
                    (r.chan == 1 or r.wyckoff == 1)):
                selected.append(symbol)
                peaks[symbol] = float(q[symbol].at[when, "close"])
        return {s: 0.5 for s in selected}

    return decide


def main() -> None:
    print("Preparing 21 cryptocurrencies", flush=True)
    crypto_minutes, crypto_h, crypto_q = prepare(CRYPTO_SYMBOLS,
                                                {f"2026-{m:02d}" for m in range(5, 10)})
    crypto_h["BTCUSDT"]["ret7d"] = crypto_h["BTCUSDT"].close / crypto_h["BTCUSDT"].close.shift(168) - 1
    print("Preparing 21 tokenized stocks", flush=True)
    stocks = stock_symbols()
    stock_minutes, stock_h, stock_q = prepare(stocks,
                                             {f"2026-{m:02d}" for m in range(6, 10)})
    output = {"windows": [], "crypto_universe": CRYPTO_SYMBOLS, "stock_universe": stocks,
              "rule": {"max_positions_per_sleeve": 2, "each_position_weight": 0.5,
                       "evaluation": "4h; 15m close 8% trailing stop",
                       "crypto_min_24h_quote_usdt": 20_000_000,
                       "stock_min_24h_quote_usdt": 2_000_000,
                       "fee_per_side": 0.001, "extra_cost_bps_per_side": 2,
                       "execution": "t+1s in latest window, next-minute open proxy elsewhere"}}
    for start, end in WINDOWS:
        exact = start == HOLDOUT_START
        bt.SYMBOLS = CRYPTO_SYMBOLS
        crypto_sec = bt.SecondPrices() if exact else None
        crypto = bt.simulate("rolling_crypto", start, end, crypto_h, crypto_q, crypto_minutes,
                             crypto_sec, chooser=make_roller("crypto", crypto_h, crypto_q))
        core = bt.simulate("core", start, end, crypto_h, crypto_q, crypto_minutes,
                           crypto_sec, chooser=make_trailing_chooser(0.05))
        bt.SYMBOLS = stocks
        stock_sec = bt.SecondPrices() if exact else None
        stock = bt.simulate("rolling_stock", start, end, stock_h, stock_q, stock_minutes,
                            stock_sec, chooser=make_roller("stock", stock_h, stock_q))
        btc = np.array([row["equity"] for row in core.equity])
        # combine() requires a BTC benchmark curve only for its array index;
        # each named sleeve is already normalized to a 100,000 USD account.
        legs = {"core": core, "crypto": crypto, "stock": stock}
        mixes = {
            "half_crypto_half_stock": combine(legs, btc, {"crypto": 0.5, "stock": 0.5}),
            "core40_crypto30_stock30": combine(legs, btc, {"core": 0.4, "crypto": 0.3, "stock": 0.3}),
            "core20_crypto40_stock40": combine(legs, btc, {"core": 0.2, "crypto": 0.4, "stock": 0.4}),
        }
        row = {"start": start, "end_exclusive": end, "execution": "1s" if exact else "1m proxy",
               "legs": {"core": core.metrics, "crypto": crypto.metrics, "stock": stock.metrics},
               "mixes": mixes}
        if exact:
            row["exact_second_fraction"] = {
                "crypto": float(np.mean(np.array(crypto_sec.waits) == 0)) if crypto_sec.waits else None,
                "stock": float(np.mean(np.array(stock_sec.waits) == 0)) if stock_sec.waits else None}
            row["fill_wait_count"] = {"crypto": len(crypto_sec.waits), "stock": len(stock_sec.waits)}
            # A more pessimistic spread/slippage scenario, with the same signals.
            stress = {}
            for name, sym, hh, qq, mm, kind in (
                ("crypto", CRYPTO_SYMBOLS, crypto_h, crypto_q, crypto_minutes, "crypto"),
                ("stock", stocks, stock_h, stock_q, stock_minutes, "stock")):
                bt.SYMBOLS = sym
                stress[name] = bt.simulate(name, start, end, hh, qq, mm, bt.SecondPrices(),
                                           extra_bps=12, chooser=make_roller(kind, hh, qq)).metrics
            row["cost_stress_12bps"] = stress
        output["windows"].append(row)
        print(start, {k: round(v.metrics["return_pct"], 2) for k, v in legs.items()},
              {k: round(v["return_pct"], 2) for k, v in mixes.items()}, flush=True)
    (bt.ROOT / "rolling_screen.json").write_text(json.dumps(output, indent=2) + "\n")
    print("Wrote rolling_screen.json", flush=True)


if __name__ == "__main__":
    main()
