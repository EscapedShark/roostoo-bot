from __future__ import annotations

import concurrent.futures
import json
import math
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from backtest import chan_structure, wyckoff_events
from competition_strategy import HourSignal, iso, utc_time
from download_v2_data import SYMBOLS

WIDTH = {"1h": 3_600_000, "15m": 900_000}
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


def boundary(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)


def hour_features(hours: pd.DataFrame) -> pd.DataFrame:
    """Same causal functions and return/turnover definitions as research."""
    out = hours.join(chan_structure(hours)).join(wyckoff_events(hours))
    out["ret7d"] = out.close / out.close.shift(168) - 1
    out["ret72"] = out.close / out.close.shift(72) - 1
    out["liq24"] = out.quote_volume.rolling(24, min_periods=24).sum()
    return out


@dataclass
class Snapshot:
    hourly: dict = field(default_factory=dict)
    closes: dict = field(default_factory=dict)
    previous: dict = field(default_factory=dict)
    hour_at: dict = field(default_factory=dict)
    close_at: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)


class Feed:
    def __init__(self, config, fetch=None):
        self.config = config
        self.path = config.state_dir / "candles.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            old = db.execute("SELECT value FROM metadata WHERE key='warmup_start'").fetchone()
            if old and old[0] != config.warmup_start:
                raise ValueError("candle cache WARMUP_START differs; use a separate STATE_DIR")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('warmup_start', ?)", (config.warmup_start,))
            db.execute("""CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT, interval TEXT, open_ms INTEGER,
                o REAL, h REAL, l REAL, c REAL, v REAL, q REAL,
                PRIMARY KEY(symbol, interval, open_ms))""")
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.frames, self.tags, self.errors = {}, {}, {}

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _fetch(self, symbol, interval, start, end):
        query = urlencode({"symbol": symbol, "interval": interval,
                           "startTime": start, "endTime": end, "limit": 1000})
        for attempt in range(3):
            try:
                with urlopen(self.config.binance_url + "/api/v3/klines?" + query, timeout=10) as response:
                    rows = json.load(response)
                if not isinstance(rows, list):
                    raise ValueError("Binance did not return klines")
                time.sleep(0.1)  # Modest public-data request weight during warmup.
                return rows
            except (OSError, ValueError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"Binance {symbol}/{interval}: {type(exc).__name__}") from None
                delay = 2 ** attempt
                if isinstance(exc, HTTPError) and exc.code in (418, 429):
                    delay = max(delay, min(60, float(exc.headers.get("Retry-After", "30"))))
                time.sleep(delay)

    def put(self, symbol, interval, rows, now_ms):
        width = WIDTH[interval]
        values = []
        for row in rows:
            start = int(row[0])
            # REST uses milliseconds, unlike post-2025 archive CSV microseconds.
            if start % width or int(row[6]) + 1 != start + width:
                raise ValueError("misaligned or wrong-unit kline timestamp")
            if start + width > now_ms:
                continue
            o, h, l, c, v, q = [float(row[i]) for i in (1, 2, 3, 4, 5, 7)]
            if not all(math.isfinite(x) for x in (o, h, l, c, v, q)) or not (
                    0 < l <= min(o, c) <= max(o, c) <= h and v >= 0 and q >= 0):
                raise ValueError("invalid OHLCV")
            values.append((symbol, interval, start, o, h, l, c, v, q))
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", values)
            if interval == "15m":
                db.execute("DELETE FROM candles WHERE symbol=? AND interval='15m' AND open_ms < ?",
                           (symbol, now_ms - 14 * 86_400_000))
        return len(values)

    def frame(self, symbol, interval, until_ms):
        start = int(utc_time(self.config.warmup_start).timestamp() * 1000)
        with self.connect() as db:
            rows = db.execute("SELECT open_ms,o,h,l,c,v,q FROM candles WHERE symbol=? AND interval=? "
                              "AND open_ms>=? AND open_ms+?<=? ORDER BY open_ms",
                              (symbol, interval, start, WIDTH[interval], until_ms)).fetchall()
        frame = pd.DataFrame(rows, columns=["stamp", *COLUMNS])
        frame.index = pd.to_datetime(frame.pop("stamp") + WIDTH[interval], unit="ms", utc=True)
        return frame.astype(float)

    def update_symbol(self, symbol, now_ms):
        start = int(utc_time(self.config.warmup_start).timestamp() * 1000)
        for interval, width in WIDTH.items():
            with self.connect() as db:
                times = [row[0] for row in db.execute(
                    "SELECT open_ms FROM candles WHERE symbol=? AND interval=? AND open_ms>=? ORDER BY open_ms",
                    (symbol, interval, start))]
            if interval == "15m":
                gaps = [a + width for a, b in zip(times, times[1:]) if b - a != width]
                cursor = max(start, gaps[0] if gaps else (times[-1] if times else now_ms // width * width - 100 * width))
            else:
                gaps = [a + width for a, b in zip(times, times[1:]) if b - a != width]
                cursor = gaps[0] if gaps else (times[-1] if times else start)
            while cursor + width <= now_ms:
                rows = self.fetch(symbol, interval, cursor, now_ms - 1)
                if not rows:
                    break
                self.put(symbol, interval, rows, now_ms)
                next_cursor = int(rows[-1][0]) + width
                if next_cursor <= cursor:
                    raise ValueError("non-advancing Binance page")
                cursor = next_cursor
        hours = self.frame(symbol, "1h", now_ms)
        if len(hours) < 169:
            raise ValueError(f"insufficient history: {len(hours)} closed hours")
        if any((b - a).total_seconds() != 3600 for a, b in zip(hours.index, hours.index[1:])):
            raise ValueError("hourly history contains a gap")
        tag = (len(hours), hours.index[-1], tuple(hours.iloc[-1]))
        if self.tags.get(symbol) != tag:
            computed = hour_features(hours)
            with self.lock:
                self.frames[symbol], self.tags[symbol] = computed, tag

    def refresh(self, now: datetime):
        now_ms = int(now.timestamp() * 1000)
        errors = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {pool.submit(self.update_symbol, symbol, now_ms): symbol for symbol in SYMBOLS}
            for future in concurrent.futures.as_completed(jobs):
                symbol = jobs[future]
                try:
                    future.result()
                except Exception as exc:
                    errors[symbol] = str(exc)
                    # A history gap invalidates the old feature cache too.
                    with self.lock:
                        self.frames.pop(symbol, None)
                        self.tags.pop(symbol, None)
        with self.lock:
            self.errors = errors
        return errors

    def snapshot(self, when: datetime) -> Snapshot:
        ms = int(when.timestamp() * 1000)
        out = Snapshot()
        with self.lock:
            frames, out.errors = dict(self.frames), dict(self.errors)
        for symbol in SYMBOLS:
            q = self.frame(symbol, "15m", ms)
            if len(q) and q.index[-1] == when:
                out.closes[symbol] = float(q.iloc[-1].close)
                out.close_at[symbol] = iso(when)
                if len(q) >= 2 and (q.index[-1] - q.index[-2]).total_seconds() == 900:
                    out.previous[symbol] = float(q.iloc[-2].close)
            h = frames.get(symbol)
            if h is not None:
                closed = h.loc[h.index <= when]
                if len(closed):
                    row = closed.iloc[-1]
                    values = [row[name] for name in ("ret7d", "ret72", "liq24", "chan", "zone", "wyckoff")]
                    if all(math.isfinite(float(x)) for x in values):
                        out.hourly[symbol] = HourSignal.from_mapping(row)
                        out.hour_at[symbol] = closed.index[-1].isoformat()
        return out

    def seed(self, archives: Path, now: datetime, report=print):
        """Import local 1m ZIPs; exclude incomplete aggregates and overlapping duplicates."""
        for symbol in SYMBOLS:
            parts = []
            for path in sorted((archives / symbol).glob("*.zip")):
                # Filter by month before reading large unrelated archives.
                suffix = path.stem.split("-1m-")[-1]
                if suffix[:7] < self.config.warmup_start[:7]:
                    continue
                with zipfile.ZipFile(path) as zf:
                    with zf.open(zf.namelist()[0]) as stream:
                        part = pd.read_csv(stream, header=None, usecols=[0, 1, 2, 3, 4, 5, 7])
                part.columns = ["stamp", *COLUMNS]
                part = part.apply(pd.to_numeric, errors="coerce").dropna()
                if part.empty:
                    continue
                divisor = 1000 if part.stamp.iloc[0] >= 10**14 else 1
                part.index = pd.to_datetime(part.pop("stamp") / divisor, unit="ms", utc=True)
                parts.append(part)
            if not parts:
                report(f"{symbol}: no local archives; REST will warm up")
                continue
            minutes = pd.concat(parts).sort_index()
            minutes = minutes[~minutes.index.duplicated(keep="last")]
            minutes = minutes.loc[(minutes.index >= utc_time(self.config.warmup_start)) & (minutes.index < now)]
            for interval, width in WIDTH.items():
                count = width // 60_000
                grouped = minutes.resample(f"{count}min", label="left", closed="left")
                bars = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                    "volume": "sum", "quote_volume": "sum"})
                bars = bars.loc[grouped.close.count() == count].dropna()
                rows = [[int(stamp.timestamp() * 1000), row.open, row.high, row.low, row.close,
                         row.volume, int(stamp.timestamp() * 1000) + width - 1, row.quote_volume]
                        for stamp, row in bars.iterrows()]
                self.put(symbol, interval, rows, int(now.timestamp() * 1000))
            report(f"{symbol}: imported {len(minutes)} minutes")
