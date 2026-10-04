#!/usr/bin/env python3
"""Continuous Roostoo bot. Observe is the default; check/status never trade."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from roostoo_bot.api import RateLimiter, Roostoo
from roostoo_bot.config import Config
from roostoo_bot.feed import Feed
from roostoo_bot.store import Store, process_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "seed", "warmup", "run", "status", "export"))
    parser.add_argument("--mode", choices=("observe", "test", "competition"))
    parser.add_argument("--once", action="store_true", help="run one decision, still respecting the selected mode")
    parser.add_argument("--archives", type=Path, help="directory containing SYMBOL/1m zip archives")
    parser.add_argument("--output", type=Path, help="export JSONL event destination")
    args = parser.parse_args()
    cfg = Config.read(args.mode)
    if args.command in ("status", "export"):
        path = cfg.state_dir / f"{cfg.mode}.sqlite"
        if not path.exists():
            raise ValueError("this mode has no saved state")
        store = Store(path)
        if args.command == "export":
            output = args.output or cfg.log_dir / f"{cfg.mode}-audit.jsonl"
            output.parent.mkdir(parents=True, exist_ok=True)
            store.export_events(output)
            print(f"Exported: {output}")
        else:
            print(json.dumps({"account": store.account(), "pending": store.pending()}, ensure_ascii=False, indent=2))
        return
    with process_lock(cfg.state_dir / "bot.lock"):
        limiter = RateLimiter(cfg.state_dir / "rate_limit.sqlite")
        api = Roostoo(cfg, limiter)
        clock = api.sync_time()
        if args.command == "check":
            rules, quotes = api.exchange_info(), api.quotes()
            valid = [s for s, q in quotes.items() if q.is_fresh(api.now(), cfg.guard)]
            if len(valid) != len(rules):
                raise ValueError("one or more Roostoo quotes is stale or missing")
            feed = Feed(cfg)
            now = api.now()
            rows = feed.fetch("BTCUSDT", "15m", int(now.timestamp() * 1000) - 3 * 900_000,
                              int(now.timestamp() * 1000) - 1)
            completed = [r for r in rows if int(r[6]) + 1 <= int(now.timestamp() * 1000)]
            if not completed:
                raise ValueError("Binance returned no closed candle")
            output = {"check": "passed", "mode": cfg.mode, **clock, "tradable_symbols": len(rules),
                      "fresh_quotes": len(valid), "binance_closed_candles": len(completed), "orders_sent": 0}
            if cfg.live:
                wallet = api.balance()
                output.update(usd_free=wallet.get("USD", {}).get("Free"), pending=api.pending_count(),
                              short_positions=len(api.short_positions().get("Positions", [])))
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif args.command in ("seed", "warmup"):
            feed = Feed(cfg)
            if args.command == "seed":
                if not args.archives or not args.archives.is_dir():
                    raise ValueError("seed requires --archives pointing to the local data/1m directory")
                feed.seed(args.archives, api.now())
            errors = feed.refresh(api.now())
            print(json.dumps({"ready_symbols": len(feed.frames), "errors": errors}, ensure_ascii=False, indent=2))
            if errors:
                raise ValueError("warmup incomplete; inspect errors before running")
        else:
            from roostoo_bot.runner import Runner, logger
            log = logger(cfg)
            log("startup", variant=cfg.variant, identity=cfg.identity, clock=clock)
            store = Store(cfg.state_dir / f"{cfg.mode}.sqlite")
            Runner(cfg, api, store, Feed(cfg), log).run(once=args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; persistent state was retained.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
