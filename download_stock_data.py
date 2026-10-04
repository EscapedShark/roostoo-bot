#!/usr/bin/env python3
"""Fetch Binance spot archives for Roostoo's currently listed stock tokens."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import urllib.error

from download_data import BASE, OUT, ROOT, fetch


def symbols() -> tuple[str, ...]:
    exchange = json.loads((ROOT / "roostoo_exchange_info_2026-10-01.json").read_text())
    return tuple(sorted(info["Coin"] + "USDT" for info in exchange["TradePairs"].values()
                        if info["AssetType"] == "stock" and info["CanTrade"]))


def targets() -> list[tuple[str, object]]:
    jobs = []
    for symbol in symbols():
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


def main() -> None:
    records, unavailable, errors = [], [], []
    jobs = targets()
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch, job): job for job in jobs}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                records.append(future.result())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    unavailable.append(futures[future][0])
                else:
                    errors.append({"url": futures[future][0], "error": repr(exc)})
            except Exception as exc:
                errors.append({"url": futures[future][0], "error": repr(exc)})
            if count % 100 == 0 or count == len(jobs):
                print(f"{count}/{len(jobs)}: {len(records)} saved, "
                      f"{len(unavailable)} unavailable, {len(errors)} errors", flush=True)
    manifest = {"downloaded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "universe_at_2026_10_01": symbols(),
                "records": sorted(records, key=lambda x: x["path"]),
                "unavailable_404": sorted(unavailable), "errors": errors}
    (ROOT / "data_manifest_stock.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
