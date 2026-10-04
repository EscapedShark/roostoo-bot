#!/usr/bin/env python3
"""Expand the competition universe to 21 liquid Roostoo crypto pairs."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
from pathlib import Path

from download_data import BASE, OUT, ROOT, fetch

SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "NEARUSDT", "XRPUSDT",
    "SUIUSDT", "ENAUSDT", "BNBUSDT", "DOGEUSDT", "WLDUSDT", "UNIUSDT",
    "AVAXUSDT", "LINKUSDT", "PUMPUSDT", "HBARUSDT", "ONDOUSDT",
    "TAOUSDT", "TRXUSDT", "AAVEUSDT", "ADAUSDT",
)


def targets() -> list[tuple[str, Path]]:
    jobs = []
    for symbol in SYMBOLS:
        for month in ("2026-05", "2026-06", "2026-07", "2026-08"):
            name = f"{symbol}-1m-{month}.zip"
            jobs.append((f"{BASE}/monthly/klines/{symbol}/1m/{name}", OUT / "1m" / symbol / name))
        for day in range(1, 29):
            name = f"{symbol}-1m-2026-09-{day:02d}.zip"
            jobs.append((f"{BASE}/daily/klines/{symbol}/1m/{name}", OUT / "1m" / symbol / name))
        for day in range(15, 29):
            name = f"{symbol}-1s-2026-09-{day:02d}.zip"
            jobs.append((f"{BASE}/daily/klines/{symbol}/1s/{name}", OUT / "1s" / symbol / name))
    return jobs


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
            if count % 50 == 0 or count == len(jobs):
                print(f"{count}/{len(jobs)} complete; {len(errors)} errors", flush=True)
    manifest = {"downloaded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "symbols": SYMBOLS, "records": sorted(records, key=lambda x: x["path"]), "errors": errors}
    (ROOT / "data_manifest_v2.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Saved {len(records)} archives, {len(errors)} errors")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
