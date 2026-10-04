#!/usr/bin/env python3
"""Download public Binance spot kline archives used by the backtest."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
BASE = "https://data.binance.vision/data/spot"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT")


def targets() -> list[tuple[str, Path]]:
    jobs = []
    for symbol in SYMBOLS:
        for month in ("2026-06", "2026-07", "2026-08"):
            name = f"{symbol}-1m-{month}.zip"
            jobs.append((f"{BASE}/monthly/klines/{symbol}/1m/{name}", OUT / "1m" / symbol / name))
        for day in range(1, 29):
            name = f"{symbol}-1m-2026-09-{day:02d}.zip"
            jobs.append((f"{BASE}/daily/klines/{symbol}/1m/{name}", OUT / "1m" / symbol / name))
        for day in range(15, 29):
            name = f"{symbol}-1s-2026-09-{day:02d}.zip"
            jobs.append((f"{BASE}/daily/klines/{symbol}/1s/{name}", OUT / "1s" / symbol / name))
    return jobs


def fetch(job: tuple[str, Path]) -> dict:
    url, path = job
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        req = urllib.request.Request(url, headers={"User-Agent": "roostoo-research/1.0"})
        with urllib.request.urlopen(req, timeout=60) as response:
            content = response.read()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(path)
    raw = path.read_bytes()
    return {"path": str(path.relative_to(ROOT)), "url": url, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def main() -> None:
    jobs = targets()
    records = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch, job): job for job in jobs}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                records.append(future.result())
            except Exception as exc:
                errors.append({"url": futures[future][0], "error": repr(exc)})
            if count % 25 == 0 or count == len(jobs):
                print(f"{count}/{len(jobs)} complete; {len(errors)} errors", flush=True)
    manifest = {"downloaded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "records": sorted(records, key=lambda x: x["path"]), "errors": errors}
    (ROOT / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Saved {len(records)} archives, {len(errors)} errors")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
