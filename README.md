# Roostoo 策略部署版

代码仓库：[EscapedShark/roostoo-bot](https://github.com/EscapedShark/roostoo-bot)。

这是本地研究目录 `策略/roostoo_chan_wyckoff/` 的独立部署版本：保留原始策略，补上在线行情、Roostoo 执行、分账、成交恢复和运行入口。本目录内容就是 GitHub 仓库根目录。上传步骤见 [GITHUB_PUBLISH.md](GITHUB_PUBLISH.md)，固定信号说明见 [STRATEGY.md](STRATEGY.md)，AWS 运行步骤见 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)。

**默认 `observe`：读取真实公开行情，使用本地模拟资金与模拟成交，不发送交易请求。** `test` 和 `competition` 会使用对应密钥真实调用 Roostoo 模拟交易账户。模式名称无法验证密钥属于哪个比赛阶段，需使用主办方发给该阶段的密钥。

## 快速运行

使用 Python 3.11 或 3.12。在本目录执行：

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

`run --once` 会按选择的模式处理一次新收盘边界；在真实下单模式使用时，也可能发送订单。重复运行已处理过的边界不会重复下单，会等待下一个边界。冷启动时距收盘超过 30 秒会暂停新仓，等待下一次合格的决策窗口。

## 策略配置

默认 `STRATEGY_VARIANT=protected`、`RANK_EXIT_DAYS=1`、`STRUCTURE_EXIT_DAYS=1`。这沿用原 `protected_strategy.py` 的默认全套保护：CPI、12% 账户回撤、盘中止损、新仓价差及波动检查。它与研究报告中的“仅 CPI”实验不同。

选择 `baseline` 使用原冻结策略的收盘信号，不启用 CPI、账户回撤及盘中止损。执行层仍要求可信数据、报价、余额和及时处理。`2/2` 是研究中的滞后确认参数；本部署没有根据收益重新选择参数。配置和账户指纹写入状态，恢复时必须匹配。

资金首次按 BTC 20%、山寨币 80% 分成两腿；两腿现金、持仓、手续费各自滚动。山寨币新仓目标为该腿当时净值的 50%，最多两项可交易持仓；保留仓位不重新配平。买单预留默认 0.5% 的费用与价格变化空间，数量向下取整，所以实际投入略低于意向目标。

## 模块

| 文件 | 职责 |
| --- | --- |
| `bot.py` / `run.sh` | 入口与持续运行。 |
| `roostoo_bot/config.py` | 配置、账户及策略指纹；默认观察模式。 |
| `roostoo_bot/feed.py` | Binance 完整 1h/15m K 线、历史预热、连续性检查、特征。 |
| `roostoo_bot/api.py` | Roostoo 签名、服务器时间、公开和私有接口、持久化限流。 |
| `roostoo_bot/store.py` | SQLite 分账、状态、订单日志和累计成交差额。 |
| `roostoo_bot/execution.py` | 数量换算、先卖后买、恢复查单、余额核对。 |
| `roostoo_bot/runner.py` | UTC 调度、保护检查、异常恢复、心跳与日志。 |
| `competition_strategy.py` / `protected_strategy.py` | 原策略的逐字节副本。 |
| `backtest.py` 等研究依赖 | 复用原特征口径并保留原策略 CLI 的依赖。 |
| `STRATEGY_MANIFEST.json` | 原始源码 SHA-256 清单。 |
| `tests/` | 原策略测试及运行层故障、成交、恢复、特征测试。 |

原研究 `replay` 需要额外的历史 ZIP 文件；在线机器人不需要研究结果 JSON、1 秒回测数据或整个研究数据目录。

## 行情与执行

- 固定从 `2026-05-01T00:00:00Z` 预热完整小时线，不滚动截断缠论历史；7 日收益需要至少 169 根小时线。15 分钟行情保留最近 14 天。更改预热起点须使用新的状态目录。
- 小时和 15 分钟 K 线只使用完全收盘的数据；小时历史存在缺口则拒绝该标的的特征。使用原 `chan_structure()` 和 `wyckoff_events()`。
- UTC 每 15 分钟处理一次；BTC 趋势检查每 4 小时，山寨币排序在 UTC 00:00。停机后的旧交易时点不补下单，恢复已持仓的遗漏收盘峰值。
- 保护层每约 4 秒读取全市场 Roostoo ticker；余额通常每 30 秒核对，成交后核对。报价采用 API 返回的 `ServerTime`，不以接收时间伪造新鲜度。接口未提供每个盘口单独的更新时间。
- 所有 Roostoo 尝试，包括重试，通过滚动 60 秒最多 **28 次**的持久化限流器，低于官方 30 次预算。一个状态目录只允许一个执行进程。
- 先持久化 `SUBMITTING` 再发单。下单 HTTP 只尝试一次；网络超时先查单。订单唯一匹配后恢复，不明确则保留待恢复记录并暂停后续执行。
- 已知订单按累计成交差额记账；状态、费用和账本原子提交。部分成交不制造虚假平仓或持仓。市场单挂起超过 120 秒尝试撤单一次，再通过查单确认。
- 无法交易的卖出零头留在账本中并标记 `dust`，继续计入净值和余额核对，不占策略选币槽位。
- 真实模式首次启动要求账户只有可用 USD、没有历史订单、挂单或空头；已有交易账户必须恢复其原状态目录。余额偏离、未确认订单或策略配置变化会暂停执行。

## 状态与审计

```bash
python bot.py status --mode observe
python bot.py export --mode observe --output logs/observe-audit.jsonl
```

`state/observe.sqlite`、`state/test.sqlite`、`state/competition.sqlite` 分别保存状态；`candles.sqlite` 和 `rate_limit.sqlite` 在同一目录共享。真实账户还绑定 API key 哈希指纹，不可通过删除状态重新分配已有资金。

`logs/<模式>.jsonl` 是轮转运行日志。SQLite 的 `events` 是完整成交审计源，包含 UTC 时间、意向理由、标的、方向、成交价与数量、OrderID、API 回报、资金腿和确认状态。使用 `export` 导出；不记录密钥或签名头。

进程运行时备份数据库应使用 SQLite 备份接口；直接复制运行中的 `.sqlite` 可能遗漏 WAL。状态丢失时不能根据当前余额猜测原分账。

## 验证与限制

本机验证记录见 [VALIDATION.md](VALIDATION.md)，测试成交记录见 [TEST_RUN_REPORT.md](TEST_RUN_REPORT.md)，真实数据特征对照见 [FEATURE_PARITY.json](FEATURE_PARITY.json)。已验证公开行情、观察运行、General Portfolio 测试账户的小额实际买卖、查单、手续费与余额对账及重启恢复。你已完成 AWS 准备；机器人在 EC2 上的运行尚未核验。

余额客户端同时兼容实际服务的 `SpotWallet` 和旧文档的 `Wallet`。使用已完成测试的相同密钥在新机器运行，必须恢复其测试状态；Git 拉取源码本身不包含账本。

UTC 00:00 和每 4 小时的首次冷启动应提前完成预热。主程序刷新行情期间继续处理已持仓的保护检查；行情不完整或处理过晚时不新开仓。长期停机期间无法执行保护卖出；缺少已持仓的遗漏 K 线时暂停后续收盘决策，并继续可用的盘中保护。首次恢复必须保留完整状态和行情缓存。

正式模式必须配置主办方确认的 `COMPETITION_END_UTC`。到时停止发送订单，由 Roostoo 按官方规则进行最终清仓；时间资料存在冲突，因此示例未猜填日期。原 CPI 日历冻结到 2026-10-14；更晚赛段应在主办方允许的代码更新流程中核对日历。

## 官方资料

- [Roostoo API 文档](https://github.com/roostoo/Roostoo-API-Documents)
- [比赛 FAQ](https://roostoo.notion.site/Roostoo-Quant-Trading-Hackathon-Official-FAQ-313ba22fed798042bab7c93c609d004e)
- [AWS 登录与启动指南](https://roostoo.notion.site/Hackathon-Guide-How-to-Sign-In-AWS-and-Launch-Your-Bot-309ba22fed798071b4dde6d1e8666816)
- [Binance K 线接口](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
- [BLS CPI 发布日历](https://www.bls.gov/schedule/news_release/cpi.htm)
