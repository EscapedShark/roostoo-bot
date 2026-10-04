from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from competition_strategy import StrategyConfig, utc_time
from protected_strategy import GuardConfig

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Read plain KEY=VALUE; never execute a shell or interpolate a secret."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError(f"invalid .env line {number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    mode: str
    api_key: str
    secret: str
    state_dir: Path
    log_dir: Path
    warmup_start: str
    variant: str
    strategy: StrategyConfig
    guard: GuardConfig
    initial_cash: float = 100_000.0
    fee_reserve: float = 0.005
    paper_fee: float = 0.001
    poll_seconds: float = 4.0
    roostoo_url: str = "https://mock-api.roostoo.com"
    binance_url: str = "https://data-api.binance.vision"
    end_at: str | None = None

    @property
    def live(self) -> bool:
        return self.mode in ("test", "competition")

    @property
    def identity(self) -> str:
        account = hashlib.sha256(self.api_key.encode()).hexdigest()[:20] if self.live else "paper"
        return f"{self.mode}:{account}"

    @property
    def fingerprint(self) -> dict:
        from dataclasses import asdict
        return {"identity": self.identity, "variant": self.variant,
                "warmup_start": self.warmup_start, "strategy": asdict(self.strategy),
                "guard": asdict(self.guard), "fee_reserve": self.fee_reserve,
                "paper_fee": self.paper_fee, "roostoo_url": self.roostoo_url, "end_at": self.end_at}

    @classmethod
    def read(cls, mode: str | None = None) -> "Config":
        load_env(ROOT / ".env")
        env = os.environ
        mode = mode or env.get("BOT_MODE", "observe")
        if mode not in ("observe", "test", "competition"):
            raise ValueError("BOT_MODE must be observe, test or competition")
        key, secret = env.get("ROOSTOO_API_KEY", ""), env.get("ROOSTOO_SECRET_KEY", "")
        if mode != "observe" and (not key or not secret):
            raise ValueError("test/competition require ROOSTOO_API_KEY and ROOSTOO_SECRET_KEY")
        variant = env.get("STRATEGY_VARIANT", "protected")
        if variant not in ("protected", "baseline"):
            raise ValueError("STRATEGY_VARIANT must be protected or baseline")
        start = utc_time(env.get("WARMUP_START", "2026-05-01T00:00:00Z"))
        if start.minute or start.second or start.microsecond:
            raise ValueError("WARMUP_START must be a UTC hour boundary")
        cfg = cls(mode, key if mode != "observe" else "", secret if mode != "observe" else "",
                  Path(env.get("STATE_DIR", str(ROOT / "state"))).resolve(),
                  Path(env.get("LOG_DIR", str(ROOT / "logs"))).resolve(),
                  start.isoformat(), variant,
                  StrategyConfig(int(env.get("RANK_EXIT_DAYS", "1")),
                                 int(env.get("STRUCTURE_EXIT_DAYS", "1"))), GuardConfig(),
                  float(env.get("OBSERVE_INITIAL_CASH", "100000")),
                  float(env.get("BUY_COST_RESERVE", "0.005")),
                  float(env.get("OBSERVE_FEE", "0.001")),
                  float(env.get("POLL_SECONDS", "4")),
                  end_at=utc_time(env["COMPETITION_END_UTC"]).isoformat() if env.get("COMPETITION_END_UTC") else None)
        if not (cfg.initial_cash > 0 and 0.001 <= cfg.fee_reserve <= 0.05 and
                0 <= cfg.paper_fee <= cfg.fee_reserve and cfg.poll_seconds >= 4):
            raise ValueError("invalid cash, fees, reserve or polling interval")
        return cfg
