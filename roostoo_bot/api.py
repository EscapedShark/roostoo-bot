from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from download_v2_data import SYMBOLS
from protected_strategy import Quote


class APIError(RuntimeError):
    pass


class AmbiguousOrder(APIError):
    """A request may have reached the exchange. Do not resubmit it."""


class LocalSkip(APIError):
    """No order HTTP request was sent."""


def pair(symbol: str) -> str:
    if symbol not in SYMBOLS:
        raise ValueError(f"unsupported symbol: {symbol}")
    return symbol[:-4] + "/USD"


def canonical(params: dict) -> str:
    # All supported values are fixed identifiers, decimal strings or integers.
    # Preserve '/' as required by the official HMAC example.
    for value in params.values():
        if any(char in str(value) for char in "&=\r\n"):
            raise ValueError("invalid API parameter")
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


def transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=timeout) as response:
        return json.load(response)


class RateLimiter:
    """Persist the rolling window across restarts; every attempt consumes a slot."""
    def __init__(self, path: Path, limit: int = 28, clock=time.time, sleep=time.sleep):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (stamp REAL)")
        self.limit, self.clock, self.sleep = limit, clock, sleep

    def acquire(self):
        while True:
            now = self.clock()
            with self.db:
                self.db.execute("DELETE FROM calls WHERE stamp <= ?", (now - 60,))
                rows = self.db.execute("SELECT stamp FROM calls ORDER BY stamp").fetchall()
                if len(rows) < self.limit:
                    self.db.execute("INSERT INTO calls VALUES (?)", (now,))
                    return
            self.sleep(max(0.05, rows[0][0] + 60.05 - now))


class Roostoo:
    def __init__(self, config, limiter, send=transport, clock=time.time, sleep=time.sleep):
        self.config, self.limiter, self.send = config, limiter, send
        self.clock, self.sleep = clock, sleep
        self.offset, self.last_sync = 0.0, 0.0

    def now(self):
        return datetime.fromtimestamp(self.clock() + self.offset, timezone.utc)

    def _request(self, method, endpoint, params=None, signed=False, order=False, before_send=None):
        # Place orders have exactly one HTTP attempt. GET and query_order are read-only.
        attempts = 1 if order else 3
        for attempt in range(attempts):
            self.limiter.acquire()
            if before_send:
                before_send()
            values = dict(params or {})
            if signed or endpoint == "/v3/ticker":
                values["timestamp"] = int((self.clock() + self.offset) * 1000)
            payload = canonical(values)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if signed:
                if not self.config.live:
                    raise APIError("observe mode cannot call signed endpoints")
                headers["RST-API-KEY"] = self.config.api_key
                headers["MSG-SIGNATURE"] = hmac.new(self.config.secret.encode(), payload.encode(),
                                                     hashlib.sha256).hexdigest()
            url = self.config.roostoo_url + endpoint
            if method == "GET" and payload:
                url += "?" + payload
            try:
                result = self.send(method, url, headers, payload.encode() if method == "POST" else None, 10)
                if not isinstance(result, dict):
                    raise ValueError("API response is not an object")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                if order:
                    raise AmbiguousOrder(f"order transport failed: {type(exc).__name__}") from None
                if attempt + 1 == attempts:
                    raise APIError(f"{endpoint} unavailable: {type(exc).__name__}") from None
                # Respect a rate-limit cooldown; API errors never print authentication headers.
                retry_after = getattr(exc, "headers", {}).get("Retry-After", "") if isinstance(exc, HTTPError) else ""
                try:
                    delay = min(60, max(2 ** attempt, float(retry_after))) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                self.sleep(delay)
                continue
            if result.get("Success") is False:
                message = str(result.get("ErrMsg", "unknown rejection"))
                for credential in (self.config.api_key, self.config.secret):
                    if credential:
                        message = message.replace(credential, "REDACTED")
                if endpoint == "/v3/query_order" and message.lower() == "no order matched":
                    return {"OrderMatched": []}
                if endpoint == "/v3/pending_count" and "no pending order" in message.lower():
                    return {"TotalPending": 0}
                raise APIError(f"{endpoint}: {message}")
            return result
        raise AssertionError("unreachable")

    def sync_time(self):
        timing = []
        result = self._request("GET", "/v3/serverTime", before_send=lambda: timing.append(self.clock()))
        before = timing[-1]
        after = self.clock()
        self.offset = float(result["ServerTime"]) / 1000 - (before + after) / 2
        self.last_sync = after
        if after - before > 5:
            raise APIError("server time round trip exceeded 5 seconds")
        return {"clock_offset_seconds": round(self.offset, 3)}

    def exchange_info(self):
        result = self._request("GET", "/v3/exchangeInfo")
        if not result.get("IsRunning"):
            raise APIError("exchange is not running")
        rules = result.get("TradePairs", {})
        for symbol in SYMBOLS:
            row = rules.get(pair(symbol), {})
            if not row.get("CanTrade") or row.get("Unit") != "USD":
                raise APIError(f"pair not tradable in USD: {symbol}")
        return {symbol: rules[pair(symbol)] for symbol in SYMBOLS}

    def quotes(self):
        result = self._request("GET", "/v3/ticker")
        # This is the server's timestamp, never the local time of receipt.
        stamp = datetime.fromtimestamp(float(result["ServerTime"]) / 1000, timezone.utc).isoformat()
        data = result.get("Data", {})
        return {symbol: Quote(float(data[pair(symbol)]["MaxBid"]),
                              float(data[pair(symbol)]["MinAsk"]), stamp)
                for symbol in SYMBOLS if pair(symbol) in data}

    def balance(self):
        result = self._request("GET", "/v3/balance", signed=True)
        # The current service returns SpotWallet/MarginWallet, while the public
        # README still shows the legacy Wallet envelope.
        margin = result.get("MarginWallet")
        if margin not in (None, {}):
            raise APIError("nonempty margin wallet is unsupported by this spot-only strategy")
        wallet = result.get("SpotWallet", result.get("Wallet"))
        if not isinstance(wallet, dict) or not wallet:
            raise APIError("balance response has no usable SpotWallet/Wallet")
        for asset, row in wallet.items():
            if not isinstance(row, dict) or "Free" not in row or "Lock" not in row:
                raise APIError(f"incomplete balance row: {asset}")
        return wallet

    def orders(self, order_id=None, **filters):
        params = {"order_id": str(order_id)} if order_id is not None else filters
        return self._request("POST", "/v3/query_order", params, signed=True).get("OrderMatched", [])

    def pending_count(self):
        return int(self._request("GET", "/v3/pending_count", signed=True)["TotalPending"])

    def short_positions(self):
        return self._request("GET", "/v6/short_positions", signed=True)

    def cancel(self, order_id):
        # Cancellation is journaled by the executor and followed by query_order.
        return self._request("POST", "/v3/cancel_order", {"order_id": str(order_id)}, signed=True, order=True)

    def place(self, symbol, side, quantity, before_send):
        params = {"pair": pair(symbol), "side": side, "type": "MARKET", "quantity": quantity}
        result = self._request("POST", "/v3/place_order", params, signed=True,
                               order=True, before_send=before_send)
        detail = result.get("OrderDetail")
        if not isinstance(detail, dict) or not detail.get("OrderID"):
            raise AmbiguousOrder("order response has no usable OrderID")
        return detail
