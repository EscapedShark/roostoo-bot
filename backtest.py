#!/usr/bin/env python3
"""Reproducible, directional Chan/Wyckoff proxy backtest for Roostoo.

This is a deliberately explicit approximation: confirmed three-bar fractals and
three-stroke overlap for Chan; spring/upthrust/SOS/SOW price-volume events for
Wyckoff. All signals use completed bars. The holdout uses archived 1-second
Binance spot klines to execute one second after the decision time.
"""
from __future__ import annotations

import bisect
import io
import json
import math
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT")
TRAIN = [
    ("2026-07-01", "2026-07-15"),
    ("2026-07-15", "2026-07-29"),
    ("2026-08-01", "2026-08-15"),
    ("2026-08-15", "2026-08-29"),
    ("2026-09-01", "2026-09-15"),
]
HOLDOUT = ("2026-09-15", "2026-09-29")
FEE = 0.001  # taker per side, per supplied competition rules


def read_zip(path: Path, usecols: list[int]) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        with zf.open(zf.namelist()[0]) as fp:
            return pd.read_csv(fp, header=None, usecols=usecols)


def load_minutes(symbol: str, months: set[str] | None = None) -> pd.DataFrame:
    paths = sorted((ROOT / "data" / "1m" / symbol).glob("*.zip"))
    if months is not None:
        paths = [path for path in paths if path.stem.split("-1m-")[1][:7] in months]
    parts = [read_zip(path, [0, 1, 2, 3, 4, 5, 7]) for path in paths]
    df = pd.concat(parts, ignore_index=True)
    df.columns = ["time", "open", "high", "low", "close", "volume", "quote_volume"]
    df.index = pd.to_datetime(df.pop("time"), unit="us", utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def candles(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="right", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "quote_volume": "sum",
    }).dropna()
    return out


def chan_structure(bars: pd.DataFrame) -> pd.DataFrame:
    """Confirmed 3-bar fractals, alternating strokes at least 3 bars apart.

    Zone = intersection of price ranges of the last three completed strokes.
    A stroke endpoint is recorded only after its right-neighbour bar closes.
    """
    highs = bars.high.to_numpy()
    lows = bars.low.to_numpy()
    close = bars.close.to_numpy()
    direction = np.zeros(len(bars), dtype=np.int8)
    zone_state = np.zeros(len(bars), dtype=np.int8)
    strokes: list[tuple[int, int, float]] = []  # (bar, +1 top/-1 bottom, price)
    for t in range(2, len(bars)):
        i = t - 1
        cand = []
        if highs[i] > highs[i-1] and highs[i] >= highs[t]:
            cand.append((i, 1, highs[i]))
        if lows[i] < lows[i-1] and lows[i] <= lows[t]:
            cand.append((i, -1, lows[i]))
        for point in cand:
            if not strokes:
                strokes.append(point)
            elif strokes[-1][1] == point[1]:
                if (point[1] == 1 and point[2] > strokes[-1][2]) or (point[1] == -1 and point[2] < strokes[-1][2]):
                    strokes[-1] = point
            elif point[0] - strokes[-1][0] >= 3:
                strokes.append(point)
        tops = [p[2] for p in strokes if p[1] == 1]
        bottoms = [p[2] for p in strokes if p[1] == -1]
        if len(tops) >= 2 and len(bottoms) >= 2:
            if tops[-1] > tops[-2] and bottoms[-1] > bottoms[-2]:
                direction[t] = 1
            elif tops[-1] < tops[-2] and bottoms[-1] < bottoms[-2]:
                direction[t] = -1
        if len(strokes) >= 4:
            ranges = [(min(strokes[j-1][2], strokes[j][2]), max(strokes[j-1][2], strokes[j][2])) for j in range(len(strokes)-3, len(strokes))]
            lo = max(x[0] for x in ranges)
            hi = min(x[1] for x in ranges)
            if lo < hi:
                zone_state[t] = 1 if close[t] > hi else (-1 if close[t] < lo else 0)
    return pd.DataFrame({"chan": direction, "zone": zone_state}, index=bars.index)


def wyckoff_events(bars: pd.DataFrame) -> pd.DataFrame:
    """Causal price-volume event proxies, not subjective chart labels."""
    prior_hi = bars.high.shift(1).rolling(24, min_periods=24).max()
    prior_lo = bars.low.shift(1).rolling(24, min_periods=24).min()
    med_vol = bars.quote_volume.shift(1).rolling(24, min_periods=24).median()
    vol_ok = bars.quote_volume > 1.35 * med_vol
    bar_range = (bars.high - bars.low).replace(0, np.nan)
    strong_close = (bars.close - bars.low) / bar_range > 0.7
    weak_close = (bars.high - bars.close) / bar_range > 0.7
    spring = (bars.low < prior_lo) & (bars.close > prior_lo) & vol_ok & strong_close
    upthrust = (bars.high > prior_hi) & (bars.close < prior_hi) & vol_ok & weak_close
    sos = (bars.close > prior_hi) & vol_ok & strong_close
    sow = (bars.close < prior_lo) & vol_ok & weak_close
    event = np.select([spring | sos, upthrust | sow], [1, -1], default=0).astype(np.int8)
    state = np.zeros(len(bars), dtype=np.int8)
    last = 0
    age = 99
    for i, value in enumerate(event):
        if value:
            last, age = int(value), 0
        else:
            age += 1
        state[i] = last if age <= 8 else 0
    return pd.DataFrame({"wyckoff_event": event, "wyckoff": state}, index=bars.index)


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    h = candles(df, "1h")
    h = h.join(chan_structure(h)).join(wyckoff_events(h))
    h["ret6"] = h.close / h.close.shift(6) - 1
    h["ret24"] = h.close / h.close.shift(24) - 1
    h["vol24"] = np.log(h.close).diff().rolling(24).std().clip(lower=0.001)
    h["ema20"] = h.close.ewm(span=20, adjust=False).mean()
    h["ema60"] = h.close.ewm(span=60, adjust=False).mean()
    q = candles(df, "15min")
    q["prior_hi32"] = q.high.shift(1).rolling(32).max()
    q["prior_lo8"] = q.low.shift(1).rolling(8).min()
    q["prior_vol32"] = q.quote_volume.shift(1).rolling(32).median()
    for col in ("chan", "zone", "wyckoff", "wyckoff_event", "ret6", "ret24", "vol24"):
        q[col] = h[col].reindex(q.index, method="ffill")
    q["vol_ratio"] = q.quote_volume / q.prior_vol32.replace(0, np.nan)
    return h, q


class SecondPrices:
    def __init__(self):
        self.cache: OrderedDict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self.waits: list[float] = []

    def get(self, symbol: str, target: pd.Timestamp) -> float | None:
        day = target.strftime("%Y-%m-%d")
        key = (symbol, day)
        if key not in self.cache:
            path = ROOT / "data" / "1s" / symbol / f"{symbol}-1s-{day}.zip"
            if not path.exists():
                return None
            df = read_zip(path, [0, 1])
            self.cache[key] = (df.iloc[:, 0].to_numpy(dtype="int64"), df.iloc[:, 1].to_numpy(dtype="float64"))
            if len(self.cache) > 112:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        times, prices = self.cache[key]
        target_us = target.value // 1000
        i = np.searchsorted(times, target_us, side="left")
        if i >= len(times) or times[i] - target_us > 10_000_000:
            return None
        self.waits.append(float((times[i] - target_us) / 1_000_000))
        return float(prices[i])


def choose(strategy: str, when: pd.Timestamp, h: dict, q: dict, current: dict[str, float], use_cw: bool = True) -> dict[str, float]:
    if strategy in ("conservative", "balanced"):
        if when.minute != 0 or when.hour % 4 != 0:
            return current
        rows = {s: frame.loc[when] for s, frame in h.items() if when in frame.index}
        if strategy == "conservative":
            ranked = []
            for s in ("BTCUSDT", "ETHUSDT", "BNBUSDT"):
                r = rows.get(s)
                cw_ok = (r.chan == 1 and r.wyckoff >= 0 and r.zone >= 0) if use_cw and r is not None else True
                if r is not None and cw_ok and r.ema20 > r.ema60 and r.ret24 > 0:
                    ranked.append((float(r.ret24 / r.vol24), s))
            return {s: 0.30 for _, s in sorted(ranked, reverse=True)[:2]}
        ranked = []
        for s, r in rows.items():
            cw_ok = (r.chan == 1 and r.zone >= 0 and r.wyckoff >= 0) if use_cw else True
            if cw_ok and r.ret6 > 0 and r.ret24 > 0:
                score = float((r.ret6 + 0.5 * r.ret24) / r.vol24)
                ranked.append((score, s))
        return {s: 0.25 for _, s in sorted(ranked, reverse=True)[:4]}
    # Aggressive: 15-minute volume breakout with an 8-bar trailing floor.
    ranked = []
    for s, frame in q.items():
        if when not in frame.index:
            continue
        r = frame.loc[when]
        keep = s in current and r.close >= r.prior_lo8 and ((r.chan == 1 and r.wyckoff >= 0) if use_cw else True)
        enter = ((r.chan == 1 and r.wyckoff == 1) if use_cw else True) and r.close > r.prior_hi32 and r.vol_ratio > 1.5
        if keep or enter:
            score = float((r.close / r.prior_hi32 - 1) * min(r.vol_ratio, 5)) if pd.notna(r.prior_hi32) else 0.0
            ranked.append((score + (0.01 if keep else 0), s))
    return {s: 0.35 for _, s in sorted(ranked, reverse=True)[:2]}


@dataclass
class Result:
    metrics: dict
    trades: list[dict]
    equity: list[dict]


def simulate(strategy: str, start: str, end: str, h: dict, q: dict, minutes: dict,
             seconds: SecondPrices | None, extra_bps: float = 2.0, use_cw: bool = True,
             chooser=None, delay_seconds: int = 1) -> Result:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    grid = pd.date_range(start_ts, end_ts, freq="15min", inclusive="left")
    cash = 100_000.0
    positions = {s: 0.0 for s in SYMBOLS}
    marks = {s: math.nan for s in SYMBOLS}
    trades = []
    equity_curve = []
    turnover = 0.0
    fees = 0.0
    missing_fills = 0
    for when in grid:
        active = {s: positions[s] for s in SYMBOLS if positions[s] > 1e-12}
        target = chooser(strategy, when, h, q, active) if chooser is not None else choose(strategy, when, h, q, active, use_cw=use_cw)
        if target is not active:
            # At each decision the price used for valuation is the last fully
            # closed 15-minute candle; execution is delayed by one second.
            for s in SYMBOLS:
                if when in q[s].index:
                    marks[s] = float(q[s].at[when, "close"])
            value = cash + sum(positions[s] * marks[s] for s in SYMBOLS if positions[s] and np.isfinite(marks[s]))
            changes = []
            for s in SYMBOLS:
                if s in target or positions[s] > 1e-12:
                    # This simulator keeps existing quantities unchanged until
                    # their signal exits. A retained position needs no quote.
                    if s in target and positions[s] > 1e-12:
                        continue
                    exec_at = when + pd.Timedelta(seconds=delay_seconds)
                    if seconds is not None:
                        raw = seconds.get(s, exec_at)
                    else:
                        row = minutes[s].loc[exec_at.floor("min"):exec_at.floor("min")]
                        raw = float(row.iloc[0].open) if len(row) else None
                    if raw is None:
                        missing_fills += 1
                        continue
                    # Existing positions keep their quantity. Periodic target-
                    # weight rebalancing adds turnover without a new signal.
                    desired = target.get(s, 0.0) * value / raw
                    changes.append((desired - positions[s], s, raw))
            # Sell before buying; use actual available cash for fee-inclusive buys.
            for delta, s, raw in sorted(changes, key=lambda x: x[0]):
                if delta >= -1e-12:
                    continue
                qty = min(-delta, positions[s])
                px = raw * (1 - extra_bps / 10000)
                proceeds = qty * px
                commission = proceeds * FEE
                positions[s] -= qty
                cash += proceeds - commission
                turnover += proceeds
                fees += commission
                trades.append({"time": exec_at.isoformat(), "symbol": s, "side": "SELL", "quantity": qty, "price": px, "fee": commission})
            for delta, s, raw in sorted(changes, key=lambda x: x[0], reverse=True):
                if delta <= 1e-12:
                    continue
                px = raw * (1 + extra_bps / 10000)
                qty = min(delta, cash / (px * (1 + FEE)))
                if qty * px < 1:
                    continue
                notional = qty * px
                commission = notional * FEE
                positions[s] += qty
                cash -= notional + commission
                turnover += notional
                fees += commission
                trades.append({"time": exec_at.isoformat(), "symbol": s, "side": "BUY", "quantity": qty, "price": px, "fee": commission})
        for s in SYMBOLS:
            if when in q[s].index:
                marks[s] = float(q[s].at[when, "close"])
        value = cash + sum(positions[s] * marks[s] for s in SYMBOLS if positions[s] and np.isfinite(marks[s]))
        equity_curve.append({"time": when.isoformat(), "equity": value})
    # Mark all holdings at the final available minute of the 14-day window.
    # The last decision is 15 minutes before the exact window end.
    final_minute = end_ts - pd.Timedelta(minutes=1)
    for s in SYMBOLS:
        if final_minute in minutes[s].index:
            marks[s] = float(minutes[s].at[final_minute, "close"])
    gross_terminal = cash + sum(positions[s] * marks[s] for s in SYMBOLS if positions[s] and np.isfinite(marks[s]))
    # Report terminal wealth after hypothetical liquidation, including the
    # final sell fee/spread, so dormant holdings are not marked optimistically.
    terminal_exit_cost = sum(positions[s] * marks[s] * (FEE + extra_bps / 10000) for s in SYMBOLS if positions[s] and np.isfinite(marks[s]))
    liquidated_terminal = gross_terminal - terminal_exit_cost
    equity_curve.append({"time": end_ts.isoformat(), "equity": liquidated_terminal})
    arr = np.array([v["equity"] for v in equity_curve])
    peak = np.maximum.accumulate(arr)
    max_dd = float(np.min(arr / peak - 1))
    return Result({"return_pct": float(100 * (liquidated_terminal / 100_000 - 1)), "max_drawdown_pct": 100 * max_dd,
                   "trades": len(trades), "turnover_x": turnover / 100_000, "fees_usd": fees,
                   "terminal_exit_cost_usd": terminal_exit_cost,
                   "missing_execution_prices": missing_fills}, trades, equity_curve)


def btc_buy_hold(start: str, end: str, minutes: dict, seconds: SecondPrices | None, extra_bps: float = 2.0) -> float:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    if seconds is None:
        entry = float(minutes["BTCUSDT"].at[start_ts, "open"])
    else:
        entry = seconds.get("BTCUSDT", start_ts + pd.Timedelta(seconds=1))
    exit_mid = float(minutes["BTCUSDT"].at[end_ts - pd.Timedelta(minutes=1), "close"])
    buy = entry * (1 + extra_bps / 10000)
    sell = exit_mid * (1 - extra_bps / 10000)
    return float(100 * ((sell / buy) * (1 - FEE) / (1 + FEE) - 1))


def main() -> None:
    print("Loading and generating causal features", flush=True)
    minutes = {s: load_minutes(s) for s in SYMBOLS}
    feats = {s: build_features(minutes[s]) for s in SYMBOLS}
    h = {s: feats[s][0] for s in SYMBOLS}
    q = {s: feats[s][1] for s in SYMBOLS}
    report = {"assumptions": {"symbols": SYMBOLS, "fee_per_side": FEE,
             "spread_slippage_bps_per_side": 2, "signal_bars": "closed only",
             "train_execution": "next minute open proxy for t+1s",
             "holdout_execution": "first archived 1s open at or after t+1s, max 10s wait"}, "windows": []}
    for start, end in TRAIN + [HOLDOUT]:
        holdout = (start, end) == HOLDOUT
        print(f"Simulating {start} to {end}, 1s={holdout}", flush=True)
        secs = SecondPrices() if holdout else None
        runs = {}
        for name in ("conservative", "balanced", "aggressive"):
            result = simulate(name, start, end, h, q, minutes, secs)
            runs[name] = result.metrics
            if holdout:
                pd.DataFrame(result.trades).to_csv(ROOT / f"trades_{name}.csv", index=False)
                pd.DataFrame(result.equity).to_csv(ROOT / f"equity_{name}.csv", index=False)
            print(name, result.metrics, flush=True)
        plain = {}
        for name in ("conservative", "balanced", "aggressive"):
            plain[name] = simulate(name, start, end, h, q, minutes, secs, use_cw=False).metrics
        baseline = {"cash_return_pct": 0.0, "btc_buy_hold_return_pct": btc_buy_hold(start, end, minutes, secs)}
        report["windows"].append({"start": start, "end_exclusive": end, "holdout_1s": holdout,
                                  "strategies": runs, "without_chan_wyckoff": plain,
                                  "baselines": baseline})
        if secs is not None:
            report["holdout_execution_wait_seconds"] = {
                "queries": len(secs.waits), "exact_next_second_fraction": float(np.mean(np.array(secs.waits) == 0)),
                "max_additional_wait": max(secs.waits), "median_additional_wait": float(np.median(secs.waits)),
            }
            report["holdout_minute_execution_proxy"] = {
                name: simulate(name, start, end, h, q, minutes, None).metrics
                for name in ("conservative", "balanced", "aggressive")
            }
    # Cost stress on the untouched 1-second holdout.
    secs = SecondPrices()
    report["holdout_cost_stress"] = {}
    for name in ("conservative", "balanced", "aggressive"):
        report["holdout_cost_stress"][name] = simulate(name, *HOLDOUT, h, q, minutes, secs, extra_bps=7).metrics
    (ROOT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Wrote results.json", flush=True)


if __name__ == "__main__":
    main()
