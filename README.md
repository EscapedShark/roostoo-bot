# Roostoo Trading Bot (Deployment Build)

Repository: [EscapedShark/roostoo-bot](https://github.com/EscapedShark/roostoo-bot).

This is the standalone deployment build of the local research project `策略/roostoo_chan_wyckoff/`. It keeps the original strategy unchanged and adds the live layer around it: market data feed, Roostoo execution, capital sleeves, fill recovery and a run entry point. This directory is the repository root. The fixed signal rules are described in [STRATEGY.md](STRATEGY.md); additional notes (in Chinese) are in [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) and [GITHUB_PUBLISH.md](GITHUB_PUBLISH.md).

**The default mode is `observe`: it reads real public market data, trades simulated cash with simulated fills, and never sends an order.** `test` and `competition` call the Roostoo mock exchange with real API keys and do place orders. The bot cannot tell which competition stage a key belongs to, so always use the key the organizers issued for that stage.

## Quick start

Use Python 3.11 or 3.12. From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
python -m unittest discover -s tests -v
python bot.py check --mode observe
python bot.py warmup --mode observe
python bot.py run --mode observe --once
./run.sh --mode observe
```

| Command | What it does |
| --- | --- |
| `check` | Read-only: Roostoo server time, all pairs and fresh quotes, closed Binance candles. In `test`/`competition` it also reads balance, pending orders and short positions. Never trades. |
| `warmup` | Downloads hourly and 15-minute Binance candles for all 21 symbols into `state/candles.sqlite`. Success prints `ready_symbols: 21` and empty `errors`. |
| `seed` | Optional: imports local 1-minute archives (`--archives data/1m`) instead of downloading full history. |
| `run` | Continuous loop. `--once` processes one new closed bar and exits; in a live mode it may place orders. |
| `status` / `export` | Print saved account state, or export the full audit trail as JSONL. |

`run --once` processes one new bar boundary in the selected mode. Re-running a boundary that has already been processed never re-sends orders; it waits for the next boundary. On a cold start more than 30 seconds after a bar close, new entries are paused until the next eligible decision window.

## Deploying on AWS EC2 with systemd

The competition instance is a single `t3.medium` in `ap-southeast-2`, reached only through **Session Manager** (no SSH). Run every command below in the Session Manager terminal.

### 1. Install system packages and clone

```bash
cd ~
sudo dnf install -y git tmux nano python3.11 python3.11-pip
git clone https://github.com/EscapedShark/roostoo-bot.git roostoo-bot
cd ~/roostoo-bot
git log -1 --oneline
```

Amazon Linux 2023 ships Python 3.9 as the system Python; NumPy 2.3 needs 3.11+, so use `python3.11` and do not replace the system Python.

### 2. Create the virtual environment and verify

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python bot.py check --mode observe
.venv/bin/python bot.py warmup --mode observe
```

Warmup downloads several months of hourly history on the first run, which can take a few minutes. The candle cache is shared by all modes, so warming up in `observe` also prepares `competition`.

### 3. Configure the competition key

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set these lines (paste the key and secret yourself; never commit them or put them on the command line):

```dotenv
BOT_MODE=competition
ROOSTOO_API_KEY=<competition API key>
ROOSTOO_SECRET_KEY=<competition API secret>
STRATEGY_VARIANT=protected
RANK_EXIT_DAYS=1
STRUCTURE_EXIT_DAYS=1
COMPETITION_END_UTC=
```

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

> Leave `COMPETITION_END_UTC` empty to keep trading until the competition closes; Roostoo performs the final liquidation. If you set it (e.g. `YYYY-MM-DDTHH:MM:SS+00:00`), the bot stops sending orders at that time. The key, strategy variant, parameters and this value form the account fingerprint stored on first launch; changing any of them later makes the bot refuse to start (`saved account/config fingerprint differs`).

Then run the read-only live check:

```bash
.venv/bin/python bot.py check --mode competition
```

It should report `"check": "passed"`, the USD balance, `pending: 0` and `short_positions: 0`. The first live start requires an account holding only free USD with no order history; do not place manual orders on the competition account beforehand.

### 4. Install and start the systemd service

The template uses placeholders. This replaces them with your current user and directory:

```bash
cd ~/roostoo-bot
sed -e "s|/home/YOUR_LINUX_USERNAME/roostoo-bot|$PWD|g" -e "s|YOUR_LINUX_USERNAME|$(whoami)|g" \
  roostoo-bot.service.example | sudo tee /etc/systemd/system/roostoo-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now roostoo-bot
sudo systemctl status roostoo-bot --no-pager
```

`enable` starts the bot again after an instance reboot; `Restart=on-failure` restarts it 10 seconds after a crash. A normal exit at a configured `COMPETITION_END_UTC` is not restarted. The mode comes from `BOT_MODE` in `.env`. Do not also start the bot in tmux; a lock file allows only one process per state directory.

### 5. Monitor

```bash
sudo journalctl -u roostoo-bot -f
tail -f ~/roostoo-bot/logs/competition.jsonl
~/roostoo-bot/.venv/bin/python ~/roostoo-bot/bot.py status --mode competition
```

Press `Ctrl+C` to leave `journalctl -f` or `tail -f`; the service keeps running. Healthy logs show `startup`, then `heartbeat` every minute and `feed_refresh` / `decision` every 15 minutes. `execution_paused` includes its reason. Having no orders is normal: BTC trend checks run at UTC 00/04/08/12/16/20 and altcoin ranking at UTC 00:00.

### 6. Updating code during the competition

```bash
cd ~/roostoo-bot
git pull
sudo systemctl restart roostoo-bot
```

Keep every strategy or code change as a clear Git commit, as the competition rules require. Do not stop the bot, override decisions or trade manually based on market moves. Never run the same competition key from a second machine.

## Strategy configuration

The defaults are `STRATEGY_VARIANT=protected`, `RANK_EXIT_DAYS=1`, `STRUCTURE_EXIT_DAYS=1`. This keeps the full default protection set of the original `protected_strategy.py`: CPI release guard, 12% account drawdown stop, intrabar stops, and spread and volatility checks for new entries. This differs from the "CPI only" experiment in the research report.

`baseline` uses the original frozen closed-bar signals without the CPI guard, account drawdown stop or intrabar stops. The execution layer still requires trustworthy data, quotes, balances and timely processing. `2/2` is the hysteresis parameter studied in research; this deployment did not re-select parameters based on returns. The configuration and account fingerprint are saved in state and must match on restart.

Capital is split once into a BTC sleeve (20%) and an altcoin sleeve (80%); each sleeve's cash, holdings and fees roll forward independently. A new altcoin position targets 50% of that sleeve's current equity, with at most two tradable holdings; existing positions are not rebalanced. Buy orders reserve 0.5% by default for fees and price movement, and quantities are rounded down, so actual exposure is slightly below the target.

## Modules

| File | Responsibility |
| --- | --- |
| `bot.py` / `run.sh` | Entry point and continuous run. |
| `roostoo_bot/config.py` | Configuration, account and strategy fingerprint; defaults to observe mode. |
| `roostoo_bot/feed.py` | Closed Binance 1h/15m candles, historical warmup, continuity checks, features. |
| `roostoo_bot/api.py` | Roostoo signing, server time, public and private endpoints, persistent rate limiter. |
| `roostoo_bot/store.py` | SQLite sleeves, state, order journal and cumulative fill deltas. |
| `roostoo_bot/execution.py` | Quantity conversion, sell-before-buy, order recovery, balance reconciliation. |
| `roostoo_bot/runner.py` | UTC scheduling, protection checks, error recovery, heartbeat and logs. |
| `competition_strategy.py` / `protected_strategy.py` | Byte-for-byte copies of the original strategy. |
| `backtest.py` and other research dependencies | Reuse the original feature definitions and keep the original strategy CLI working. |
| `STRATEGY_MANIFEST.json` | SHA-256 manifest of the original sources. |
| `tests/` | Original strategy tests plus runtime tests for faults, fills, recovery and features. |

The research `replay` command needs extra historical ZIP files. The live bot does not need research result JSON, 1-second backtest data or the research data directory.

## Market data and execution

- Hourly candles are warmed up from a fixed start, `2026-05-01T00:00:00Z`, without truncating the Chan-theory history; the 7-day return needs at least 169 hourly bars. Fifteen-minute data keeps the most recent 14 days. Changing the warmup start requires a new state directory.
- Only fully closed hourly and 15-minute candles are used; a gap in hourly history rejects features for that symbol. Features come from the original `chan_structure()` and `wyckoff_events()`.
- Decisions run every 15 minutes (UTC). BTC trend checks run every 4 hours and altcoin ranking at UTC 00:00. Trade times missed during downtime are not replayed; missed closing peaks of held positions are restored.
- The protection layer reads the full Roostoo ticker about every 4 seconds; balances are reconciled about every 30 seconds and after fills. Quote freshness uses the API's `ServerTime`, never the local receive time. The API provides no per-book update time.
- Every Roostoo attempt, including retries, goes through a persistent rolling limiter of at most **28 calls per 60 seconds**, below the official budget of 30. Only one executing process is allowed per state directory.
- `SUBMITTING` is persisted before an order is sent. An order HTTP request is attempted exactly once; on a network timeout the bot queries orders first. It resumes only on a unique match; otherwise it keeps the record for recovery and pauses further execution.
- Known orders are booked by cumulative fill deltas; state, fees and ledger are committed atomically. Partial fills never create false closes or positions. A market order still pending after 120 seconds gets one cancel attempt, then is confirmed by query.
- Unsellable remainders stay in the ledger marked `dust`; they count toward equity and balance reconciliation but do not take a strategy slot.
- The first live start requires an account with only free USD and no order history, pending orders or shorts. An account that has already traded must restore its original state directory. Balance drift, unconfirmed orders or configuration changes pause execution.

## State and audit

```bash
python bot.py status --mode observe
python bot.py export --mode observe --output logs/observe-audit.jsonl
```

`state/observe.sqlite`, `state/test.sqlite` and `state/competition.sqlite` hold each mode's state; `candles.sqlite` and `rate_limit.sqlite` are shared in the same directory. Live accounts are also bound to a hash of the API key, so existing capital cannot be re-split by deleting state.

`logs/<mode>.jsonl` is the rotating runtime log. The SQLite `events` table is the complete trade audit source: UTC time, intent reason, symbol, side, fill price and quantity, OrderID, API response, sleeve and confirmation status. Use `export` to extract it. Keys and signature headers are never logged.

To back up a database while the bot is running, use the SQLite backup API; copying a live `.sqlite` file directly can miss the WAL:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import sqlite3
Path('backups').mkdir(exist_ok=True)
source = sqlite3.connect('state/competition.sqlite')
target = sqlite3.connect('backups/competition.sqlite')
source.backup(target)
target.close()
source.close()
PY
```

If state is lost, the original sleeve split cannot be inferred from the current balance.

## Validation and limitations

Local validation is recorded in [VALIDATION.md](VALIDATION.md), test-account fills in [TEST_RUN_REPORT.md](TEST_RUN_REPORT.md) and real-data feature parity in [FEATURE_PARITY.json](FEATURE_PARITY.json). Verified so far: public market data, observe runs, small real buy and sell orders on the General Portfolio test account, order queries, fee and balance reconciliation, and restart recovery.

The balance client accepts both the live service's `SpotWallet` and the legacy documented `Wallet`. Running an already-used test key on a new machine requires restoring its test state; cloning the source does not include the ledger.

Finish warmup before a cold start near UTC 00:00 or the 4-hour checks. While market data refreshes, protection checks for held positions continue; incomplete data or late processing blocks new entries. Protective sells cannot run during long downtime; if missed candles for a held position are unavailable, later closed-bar decisions pause while intrabar protection continues. The first recovery must keep the full state and candle cache.

`COMPETITION_END_UTC` is optional. Left empty, the bot keeps running and Roostoo liquidates at the end according to the official rules; when set, the bot stops sending orders at that time. The CPI calendar of the original strategy is frozen up to 2026-10-14; later stages should check the calendar through an allowed code update.

## Official references

- [Roostoo API documentation](https://github.com/roostoo/Roostoo-API-Documents)
- [Competition FAQ](https://roostoo.notion.site/Roostoo-Quant-Trading-Hackathon-Official-FAQ-313ba22fed798042bab7c93c609d004e)
- [AWS sign-in and launch guide](https://roostoo.notion.site/Hackathon-Guide-How-to-Sign-In-AWS-and-Launch-Your-Bot-309ba22fed798071b4dde6d1e8666816)
- [Binance kline endpoint](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
- [BLS CPI release schedule](https://www.bls.gov/schedule/news_release/cpi.htm)
