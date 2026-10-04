# 上传 GitHub 与继续部署

更新日期：2026-10-05（Australia/Sydney）。本地研究、部署说明、验证记录已核对。本次上传以 `部署/` 为仓库根目录，保留原始策略与完整运行层；按用户要求新建公开仓库 [EscapedShark/roostoo-bot](https://github.com/EscapedShark/roostoo-bot)。

## 提交范围

| 上传 | 本地保留、单独传输 |
| --- | --- |
| `bot.py`、`run.sh`、`roostoo_bot/` | 实际 `.env` 和所有账户密钥 |
| 原策略及 13 份研究依赖副本 | `state/` 账户账本、行情缓存、限流状态 |
| `tests/`、固定 `requirements.txt` | `logs/` 运行和订单审计 |
| `.env.example`、`.gitignore` | `.venv/`、`data/`、历史行情 ZIP |
| README、策略说明、验证和部署文档 | `backups/`、`dist/` 本地打包产物 |
| SHA-256 策略清单、特征对照 | `state/test-account-transfer.sh` 状态迁移脚本 |

本地 `策略/` 是研究归档；研究版 2/2 和“仅 CPI”实验没有替换部署默认的完整保护 1/1。原研究源码不修改。官方 `Roostoo-API-Documents` 也不作为自己的代码仓库上传。

## 本机提交

在本机包含 `bot.py` 的目录操作：

```bash
cd /Users/dxcfw/Desktop/roostoo/部署
git status --short
git diff --cached --stat
```

首次提交前确认暂存清单没有 `.env`、`state/`、`logs/`、数据、备份或虚拟环境，并扫描暂存内容中是否出现实际凭据。代码验证使用：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

准备好首次提交后，绑定目标仓库，正常推送 `main`。若本地 `origin` 已绑定，直接执行最后一行即可：

```bash
git remote add origin https://github.com/EscapedShark/roostoo-bot.git
git push -u origin main
```

若远端已有提交，先读取远端历史并保留其内容，再整合本地代码；不要用强推覆盖。比赛 FAQ 要求最终提交开源仓库和可追溯修改历史；用户指定为私有仓库时保留该设置，但最终提交前仍需按比赛要求开放。

## AWS 下一步

进入已准备好的实例 Session Manager 终端：

```bash
cd ~
git clone https://github.com/EscapedShark/roostoo-bot.git roostoo-bot
cd ~/roostoo-bot
git log -1 --oneline
```

随后按 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) 的第 2、4、6 节检查 Python、安装依赖、预热，并使用测试账户验证。

同一组 General Portfolio 测试密钥已经有两笔成交。上云继续使用前，必须恢复本机 `state/test-account-transfer.sh` 中的账本；密钥在服务器单独配置，行情缓存可以重新预热。单独保存迁移脚本并通过 Session Manager 传入，不加入 GitHub。

测试账户运行成功后再进入主赛配置：使用主赛密钥，并填写主办方确认的 `COMPETITION_END_UTC`。GitHub 推送成功不代表云端机器人已经启动。
