# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

监控 Chrome Hearts 官网(`chromehearts.com`)全品类的上新/补货,发现状态跃迁就推送到微信(Server酱)。单文件 Python 脚本 + GitHub Actions 云端定时跑。

## 常用命令

```powershell
python chrome_hearts_monitor.py            # 持续监控(本地常驻,monitor_loop)
python chrome_hearts_monitor.py --serve    # 长跑 RUN_SECONDS 秒后退出(GitHub 云端"接力"用)
python chrome_hearts_monitor.py --once     # 跑一轮就退出(调试看效果)
python chrome_hearts_monitor.py --selftest # 验证抓取逻辑(无需 sendkey)
python chrome_hearts_monitor.py --test-push# 发测试消息验证 Server酱配置

python test_logic.py                       # 跑判新核心逻辑测试(纯函数,无网络,退出码非0即失败)

pip install requests beautifulsoup4        # 仅有的两个运行时依赖
```

没有 lint/build 步骤;`test_logic.py` 是唯一的自动化测试(用 `m.compute_events` / `m.scan_trustworthy` 断言,无 pytest 依赖)。

## 架构要点(读多个文件才能拼出的大图)

数据流:`scan()` 抓所有品类 → `compute_events()` 对比上一轮状态算出要通知什么 → `notify()` 推送 → `save_state()` 落盘。`process()` 把这条链串起来,`monitor_loop()`(本地)和 `--once`(云端)都调它。

- **判新模型是"状态跃迁",不是"见过就永久去重"**。`state.json` 存 `{pid: {"status": in_stock/oos/delisted, "missing": int, "oos_streak": int, "notified": {事件: epoch}}}`。`compute_events`(纯函数,是测试重点)只在两种跃迁时通知:① 之前不存在 / 已判 `delisted` → 出现 = **上新**;② 之前确认 `oos` → 这次 `in_stock` = **补货**。改判新逻辑务必同步更新 `test_logic.py`。

- **防重复推送的几道闸**(2026-07 修复"同一商品短时间反复推送"后加入,细节见脚本顶部 docstring):① 售罄要连续 `oos_cycles_before_confirm`(3)轮观测一致才确认,防页面/解析抖动反复给"补货"上膛;② 同一 pid 跨品类聚合是确定性的(`merge_present`:任一处有货即有货),不随扫描顺序变化;③ 下架不删条目而是标 `delisted` 保留档案(`delisted_retention_days` 天后清理),`notified` 时间戳因此在"反复消失又出现"时仍有效;④ 同一商品同类事件 `notify_cooldown_hours`(12h)内只推一次;⑤ 同一批事件连续推送失败 `max_push_retries`(3)次后放弃重试并提交状态,防"Server酱实际已送达但回包报错"造成每轮重发。

- **下架判定有去抖**:商品要连续 `absence_cycles_before_relist`(2)轮从"可信扫描"里消失,才标记 `delisted`(再出现算上新,但受冷却约束)。

- **扫描健康度守门 `scan_trustworthy`** 防风控误报:只要有品类抓取失败,或在架数骤降到基线(不含 delisted 档案)×`min_scan_health_ratio`(0.8)以下(疑似 Forter 风控喂空页),本轮就**不处理"消失/下架",也不做 oos→in_stock 的补货跃迁**。若骤降在抓取全成功的情况下持续 `health_breach_grace_rounds`(30)轮,视为官网真实缩水,接受新基线(防守门永久卡死)。

- **品类来源 = 静态清单 ∪ 首页自动发现**:`discover_categories` 每轮从首页导航正则抓 slug 并过滤 `NON_CATEGORY`,覆盖官网新增品类。

- **解析依赖官网结构**:官网是 Salesforce Commerce Cloud (Demandware),品类页服务端渲染,权威数据来自 `<span class="product-metadata" data-pid/data-name/data-price>`(`parse_products`)。售罄判定靠 tile 文本里的 "out of stock"/"sold out"。**不需要浏览器**,普通 `requests` 即可。

- **推送成功才提交状态**:`process()` 里若 `notify()` 微信推送失败,就保存旧状态(只做每日心跳),让事件下轮重算,避免漏推。这是 state.json 提交时机的关键设计。

## 配置与密钥

- `config.json` 是运行参数(轮询间隔、品类清单、各阈值、开关)。`serverchan_sendkey` 在本地留空。
- **SendKey 走环境变量优先**:`load_config` 读 `SERVERCHAN_SENDKEY` 覆盖配置;云端存为 GitHub Secret `SERVERCHAN_SENDKEY`,绝不写进仓库。

## 云端部署(GitHub Actions,分钟级)

- 仓库 `https://github.com/csdyrz/chrome-hearts-monitor`,工作流 `.github/workflows/monitor.yml`。
- **关键:不用 `schedule` 跑 `--once`**(GitHub 定时被限流到每隔 1.5~6 小时才跑一次,给不了分钟级)。改为**单 job 内部长跑** `--serve`:一个 job 内每 60s 扫一次、连跑 `RUN_SECONDS`(20700s≈5h45m)后正常退出,进程几乎一直在线 = 分钟级实时。公开仓库 Actions 不限分钟,可全天候免费长跑。
- **接力衔接**:`concurrency: ch-monitor`(cancel-in-progress:false)让下一棒排队等当前这棒结束后无缝顶上;cron `*/5` 仅作兜底重启链条;可选配 `PAT_TOKEN` secret,跑完主动 `gh workflow run` 自我再触发(用默认 `GITHUB_TOKEN` 自触发会被 GitHub 屏蔽,故需 PAT)。
- **state 持久化**:长跑期间由 Python 的 `git_commit_state()` 在每轮状态变化时提交回仓库(仅当 `COMMIT_STATE=1` 时启用,本地不碰 git),保证接力的下一棒 checkout 到最新基线、不重复推。
- 已知边界:一轮全品类扫描本身约 30~60s,叠加 60s 间隔,有效节奏约 1.5~2 分钟;真·秒级需上 VPS 常驻 `monitor_loop`。
