# 从 GitHub 拉取并在 AWS 启动机器人

更新日期：2026-10-05（Australia/Sydney）。你已完成 AWS 准备，当前先提交自己的 GitHub 仓库，再到实例拉取代码。`部署/` 包含完整运行层，默认观察模式；原研究目录保持原样。GitHub 提交范围见 [GITHUB_PUBLISH.md](GITHUB_PUBLISH.md)，实现与验证范围见 [README.md](README.md) 和 [VALIDATION.md](VALIDATION.md)。

## 1. 连接团队实例

如果已进入实例的 Session Manager 终端，直接继续第 2 节；以下步骤保留作连接参考。

1. AWS 控制台区域选择 **Sydney / ap-southeast-2**。
2. 打开 **EC2 → Instances**，确认团队是否已有实例。
3. 有实例就使用现有实例；没有则打开 **Launch Templates → Hackathon-Starter-Template → Actions → Launch Instance from Template**。
4. 选择 **Proceed without a key pair**，保持模板默认配置。比赛仅允许一台 `t3.medium`，EBS 上限 30 GB。
5. 等待实例运行及状态检查通过。
6. 勾选实例，选择 **Connect → Session Manager → Connect**。

比赛模板使用 Session Manager；SSH 和 EC2 Instance Connect 不可用。实例、权限、网络等按主办方模板配置。来源：[主办方 AWS 指南](https://roostoo.notion.site/Hackathon-Guide-How-to-Sign-In-AWS-and-Launch-Your-Bot-309ba22fed798071b4dde6d1e8666816)。

## 2. 检查并准备 Python

在 Session Manager 终端执行：

```bash
whoami
pwd
cat /etc/os-release
python3 --version
```

如果系统是 **Amazon Linux 2023**：

```bash
sudo dnf install -y git tmux python3.11 python3.11-pip
python3.11 --version
```

AL2023 的系统 Python 为 3.9，需要单独使用 `python3.11`；不要替换系统 Python。[AWS Python 说明](https://docs.aws.amazon.com/linux/al2023/ug/python.html)。当前固定依赖 `numpy==2.3.5`、`pandas==2.2.3`，NumPy 要求 Python 3.11+。[PyPI 元数据](https://pypi.org/project/numpy/2.3.5/)。本机已在 Python 3.12.14 验证。

若实例使用其他系统，根据实际系统安装 Python 3.11 或 3.12，再继续。

## 3. 上传自己的部署代码

应将本机 **`部署/` 中的内容**作为自己的 GitHub 仓库根目录。`Roostoo-API-Documents` 是官方接口文档仓库，不能代替你的机器人仓库。比赛评审需要开放源码和可追溯提交。

本机目录 `/Users/dxcfw/Desktop/roostoo/部署` 已包含 `.gitignore`；真实 `.env`、运行状态、日志、备份、历史 ZIP 和虚拟环境均不进入仓库。仓库应包含源码、测试、依赖、策略说明、部署说明和空的 `.env.example`。

自己的仓库准备好后，服务器执行；替换下方地址：

```bash
cd ~
git clone https://github.com/EscapedShark/roostoo-bot.git roostoo-bot
cd ~/roostoo-bot
git log -1 --oneline
```

记录拉取的 commit，便于确认云端运行版本与 GitHub 一致。比赛最终提交要求开源；如果仓库仍为私有，实例拉取需要有读取权限的 GitHub 身份，不要把访问令牌写进 clone URL 或机器人 `.env`。

如果你上传的是整个工作区而非部署文件夹内容，后续工作目录改为 `~/roostoo-bot/部署`。

部署目录还提供 `dist/roostoo-deploy.tar.gz`，只包含源码、测试、说明和空配置模板，可作为上传包；包内不包含密钥、状态、日志或历史数据。Session Manager 不提供本地 `scp` 通道，比赛默认仍以自己的 Git 仓库拉取代码。

## 4. 安装和验证

在包含 `bot.py` 的目录执行：

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
```

预热自动从 Binance 获取 21 个标的、固定起点以来的小时线和近期 15 分钟线，并缓存到 `state/candles.sqlite`。首次需要下载历史；重启只更新缺少的部分。成功应显示 `ready_symbols: 21` 和空 `errors`。

`check` 只读检查 Roostoo 服务器时间、全部交易对与新鲜报价、Binance 完整 K 线。它不会下单，也不能在同一目录已有运行进程时抢占锁。

已有本地研究数据时可以选择导入，服务器无需上传整个历史数据目录：

```bash
python bot.py seed --mode observe --archives /path/to/data/1m
```

导入只接受完整的分钟聚合，随后使用公开接口补齐最新行情。行情特征不是从 Roostoo ticker 构造；Binance 与 Roostoo 的 USDT/USD 标识有明确映射，保护版会检查新仓报价与信号价格偏差。

## 5. 先持续运行观察模式

```bash
tmux new -s roostoo-observe
cd ~/roostoo-bot
./run.sh --mode observe
```

如果仓库中保留了 `部署/` 子目录，对应 `cd ~/roostoo-bot/部署`。`run.sh` 自动使用该目录 `.venv`。

按 **Ctrl+B，再按 D** 脱离 tmux；Session Manager 窗口关闭后程序仍运行。重新查看：

```bash
tmux attach -t roostoo-observe
```

在其他 Session Manager 终端可以只读查看：

```bash
cd ~/roostoo-bot
.venv/bin/python bot.py status --mode observe
tail -n 20 logs/observe.jsonl
```

关注 `heartbeat`、`feed_refresh`、`decision`、`order_observed` 和 `execution_paused`。观察模式的成交记录标明 `simulated=true`，不计入比赛交易天数。

BTC 趋势检查为 UTC 00/04/08/12/16/20 点；山寨币日检为 UTC 00:00。没有意向不等于程序失效。处理距收盘超过 30 秒时会记录 `bar_processing_delay` 并暂停该时点的新仓；应提前完成预热，持续等待下个决策窗口。

## 6. 测试账户验证

观察验证完成后，结束观察进程，使用主办方发放的 **测试阶段**密钥编辑服务器 `.env`：

```dotenv
BOT_MODE=test
ROOSTOO_API_KEY=填入测试API_KEY
ROOSTOO_SECRET_KEY=填入测试SECRET_KEY
STRATEGY_VARIANT=protected
RANK_EXIT_DAYS=1
STRUCTURE_EXIT_DAYS=1
```

编辑时可用 `nano .env` 或 `vi .env`。不要把密钥写进 Git 提交、启动命令或日志。

```bash
source .venv/bin/activate
python bot.py check --mode test
tmux new -s roostoo-test
./run.sh --mode test
```

`check --mode test` 会额外只读查询余额、挂单数和空头持仓，仍不发送交易订单。`run --mode test` 会自动执行策略意向。

首次真实模式要求账户是纯可用 USD、没有历史订单、挂单或空头。已经试下单的测试账户不能通过删除状态冒充空白账户；应恢复对应账本，或使用主办方提供的新测试密钥。

**本机已经用你提供的 General Portfolio 测试密钥完成两笔买卖及平仓。** 在 EC2 继续使用这组密钥时，应先恢复已生成的 `state/test-account-transfer.sh` 中的测试账本：

1. 服务器上先完成源码、`.venv` 和相同测试密钥的 `.env` 配置。
2. 确认服务器工作目录包含 `bot.py`，没有运行中的机器人，也没有既有 `state/test.sqlite`。
3. 在本机打开 `state/test-account-transfer.sh`，将完整脚本内容粘贴到该服务器目录的 Session Manager 终端执行。
4. 脚本校验数据、配置和账户指纹，并拒绝覆盖已有账本。随后执行 `python bot.py check --mode test` 和 `./run.sh --mode test`。

迁移脚本和账本位于被 Git 忽略的 `state/`，不包含原始密钥，仍应单独传输而非加入公开仓库。行情缓存可以重新预热。使用一组新的无交易测试密钥时，无需恢复旧账户账本，应使用新的完整 `STATE_DIR`。

通过日志及审计导出确认真实成交、手续费、20/80 分账和余额一致。测试期间验证进程重启可恢复原状态。没有信号时等待策略触发，不用手动 API 下单代替。

## 7. 转正式比赛账户

测试账户完成实际成交验证后，使用 **主赛阶段**密钥：

```dotenv
BOT_MODE=competition
ROOSTOO_API_KEY=填入主赛API_KEY
ROOSTOO_SECRET_KEY=填入主赛SECRET_KEY
COMPETITION_END_UTC=
```

`COMPETITION_END_UTC` 可留空：机器人持续运行，比赛结束后由 Roostoo 统一清仓。如需到点停止发单，可填带时区的 ISO 时间（如 `YYYY-MM-DDTHH:MM:SS+00:00`）；该值在首次启动时写入账户指纹，之后不能修改。

```bash
python bot.py check --mode competition
tmux new -s roostoo-main
./run.sh --mode competition
```

正式模式保存到 `state/competition.sqlite`，独立于测试及观察模式。使用新阶段密钥时同样使用独立的完整 `STATE_DIR`，保留旧状态供审计。不要同时从多台机器使用同一组账户密钥。

比赛进行后保持机器人自主运行，不能根据盘面临时停机、手动覆盖决策或手动下单。代码更新应保留明确 Git 提交和评审依据。最终清仓由 Roostoo 处理；若配置了 `COMPETITION_END_UTC`，系统到时停止发送订单。来源：[官方 FAQ](https://roostoo.notion.site/Roostoo-Quant-Trading-Hackathon-Official-FAQ-313ba22fed798042bab7c93c609d004e)。

## 8. 可选：systemd 自动恢复

`tmux` 保持会话；要在程序异常退出或实例重启后自动启动，可在正式运行前安装提供的 `roostoo-bot.service.example`。

1. 将模板中 `YOUR_LINUX_USERNAME` 替换为 `whoami` 的结果。
2. 将 `WorkingDirectory` 和 `ExecStart` 改为实际路径。
3. `.env` 的 `BOT_MODE` 控制服务模式；确保使用正确阶段的密钥。
4. 避免同时通过 tmux 启动同一目录的机器人。

```bash
sudo cp roostoo-bot.service.example /etc/systemd/system/roostoo-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now roostoo-bot
sudo systemctl status roostoo-bot --no-pager
sudo journalctl -u roostoo-bot -n 30 --no-pager
```

服务设置 `Restart=on-failure`，到达已配置的 `COMPETITION_END_UTC` 正常退出时不会重启。状态存放在部署目录而非临时目录。

## 9. 恢复和保存审计

`execution_paused` 包含具体原因。网络问题会自动重试只读查询；订单提交结果不明确时自动查单，不能删除订单记录或重复发送。唯一查单结果恢复后继续；零个或多个匹配会保持暂停。已知市场单挂起超过 120 秒尝试撤单，再等待查单确认。

重启必须使用同一目录、相同账户和相同策略参数。资金腿不会重新切分。程序保存累计成交进度，重复回报不会重复记账；遗漏收盘期间的持仓峰值会从完整 15 分钟历史补回，不追补过去的交易时点。

运行期间导出审计及备份状态：

```bash
.venv/bin/python bot.py export --mode competition --output logs/competition-audit.jsonl
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

备份文件和审计日志应单独保存，不公开密钥或账户私密数据。运行中不可只复制数据库主文件而遗漏 WAL；恢复时同时保留行情缓存或重新预热。

此次已完成本地部署版本、公开与私有接口检查、观察运行、测试账户实际买卖及重启对账，AWS 准备已由你完成。**机器人在服务器上的安装、预热、账户状态迁移和持续运行仍需执行；主赛模式还需要主赛密钥。**
