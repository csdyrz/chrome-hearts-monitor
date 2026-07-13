# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网全品类上新/补货监控
====================================
轮询 chromehearts.com 的所有品类,发现任何新上架或补货的商品就推送到微信(Server酱)。

官网是 Salesforce Commerce Cloud (Demandware),品类页服务端渲染,
每个商品带 <span class="product-metadata" data-pid/data-name/data-price/...>,
所以用普通 HTTP 请求即可解析,无需浏览器。

判新模型(关键):不是"见过就永久拉黑",而是跟踪每个 SKU 的【可买状态】,
在以下"状态跃迁"时通知,从而能捕捉【下架后再次上新】和【售罄后补货】:
  - 之前不存在 / 已判定下架(delisted)→ 这次出现          =》上新
  - 之前确认售罄(oos)→ 这次有货(in_stock)              =》补货

防误判/防重复推送的几道闸(针对"同一商品短时间反复推送"问题):
  1. 售罄确认去抖:连续 oos_cycles_before_confirm 轮观测到售罄,才把状态记为 oos,
     防止页面/解析抖动反复给"补货"上膛;
  2. 跨品类确定性聚合:同一 pid 出现在多个品类页时,任一处显示有货即记有货,
     消除"先扫到哪个页面听哪个"的顺序随机性(见 merge_present);
  3. 下架档案化:商品连续 absence_cycles_before_relist 轮从干净扫描里消失后标记
     delisted(保留档案而非删除),再次出现算"上新";档案保留通知时间戳,
     delisted_retention_days 天后才真正清理;
  4. 通知冷却:同一商品的同类事件 notify_cooldown_hours 小时内只推一次;
  5. 推送重试封顶:同一批事件连续推送失败 max_push_retries 次后放弃重试
     (防"实际已送达但回包报错"导致每轮重复轰炸),之后由冷却兜底。

品类来源 = 配置静态清单 ∪ 每轮从首页导航自动发现的品类(覆盖官网当前/新增的任何品类)。

用法:
    python chrome_hearts_monitor.py            # 持续监控(本地常驻)
    python chrome_hearts_monitor.py --serve    # 长跑 RUN_SECONDS 秒后退出(GitHub 云端'接力'用)
    python chrome_hearts_monitor.py --once     # 跑一轮就退出(调试看效果)
    python chrome_hearts_monitor.py --selftest # 验证抓取(无需 sendkey)
    python chrome_hearts_monitor.py --test-push# 发测试消息到微信,验证 Server酱
"""
import sys
import os
import re
import json
import time
import random
import logging
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://www.chromehearts.com"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "monitor.log")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}
NON_CATEGORY = {
    "cart", "checkout", "login", "logout", "register", "account", "contact",
    "search", "wishlist", "stores", "locations", "magazine", "about", "faq",
    "privacy", "terms", "shipping", "returns", "careers", "press", "gift-cards",
    "giftcard", "gift-card", "order", "orders", "sitemap",
}

# ---------------------------------------------------------------- 日志
logger = logging.getLogger("ch-monitor")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)


# ---------------------------------------------------------------- 配置 / 状态
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    env_key = os.environ.get("SERVERCHAN_SENDKEY")  # 环境变量优先(GitHub Secret)
    if env_key:
        cfg["serverchan_sendkey"] = env_key.strip()
    return cfg


def load_state():
    """返回 (items:dict, stored_date:str, existed:bool)。
    items = {pid: {"status": in_stock/oos/delisted, "missing": int,
                   "oos_streak": int, "notified": {event: epoch}, ["delisted_at": epoch]}}。
    旧格式条目缺新字段没关系,compute_events 全部用 .get 兼容。"""
    if not os.path.exists(STATE_PATH):
        return {}, "", False
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("items"), dict):
            return data["items"], data.get("date", ""), True
        # 兼容旧格式(seen_pids 列表):迁移为已知商品,状态未知按 in_stock 处理
        if isinstance(data.get("seen_pids"), list):
            items = {pid: {"status": "in_stock", "missing": 0} for pid in data["seen_pids"]}
            return items, data.get("date", ""), True
        return {}, "", True
    except Exception as e:
        logger.warning("状态文件读取失败,当作首次运行: %s", e)
        return {}, "", False


def save_state(items, day):
    """确定性写入,便于 git 仅在真有变化(含每日心跳)时提交。"""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items, "date": day},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def git_commit_state(day):
    """长跑模式下把变化后的 state.json 提交回仓库,保证'接力'的下一棒不丢基线、不重复推。
    仅在环境变量 COMMIT_STATE=1(由 GitHub Actions 设置)时启用;本地运行完全不碰 git。
    save_state 是确定性写入,内容不变就没有 git diff,因此绝大多数轮次是 no-op。"""
    if os.environ.get("COMMIT_STATE") != "1":
        return
    import subprocess

    def run(args):
        return subprocess.run(args, cwd=HERE, capture_output=True, text=True)
    try:
        if not run(["git", "status", "--porcelain", "state.json"]).stdout.strip():
            return  # 状态无变化,跳过
        run(["git", "config", "user.name", "github-actions[bot]"])
        run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
        run(["git", "add", "state.json"])
        run(["git", "commit", "-m", f"chore: update state {day}"])
        if run(["git", "push"]).returncode != 0:  # 远程被推进则 rebase 再推,防丢状态
            run(["git", "pull", "--rebase", "--autostash"])
            run(["git", "push"])
        logger.info("state.json 已提交回仓库。")
    except Exception as e:
        logger.warning("提交 state.json 失败(忽略,下轮再试): %s", e)


# ---------------------------------------------------------------- 品类发现 / 抓取
def discover_categories(cfg):
    """静态清单 ∪ 首页导航里出现的品类。"""
    cats = list(dict.fromkeys(cfg.get("categories", [])))
    seen = set(cats)
    if not cfg.get("auto_discover_categories", True):
        return cats
    try:
        html = requests.get(BASE_URL + "/", headers=HEADERS,
                            timeout=cfg.get("request_timeout_seconds", 25)).text
        for href in re.findall(r'href="(?:https://www\.chromehearts\.com)?(/[a-zA-Z0-9\-]+)"', html):
            slug = href.strip("/").lower()
            if slug and slug not in NON_CATEGORY and slug not in seen:
                seen.add(slug)
                cats.append(slug)
                logger.info("自动发现新品类: %s", slug)
    except Exception as e:
        logger.warning("首页品类发现失败(用静态清单兜底): %s", e)
    return cats


def fetch_category(slug, cfg):
    """抓一个品类页。404 视为空品类返回 []。其它失败抛异常。"""
    url = f"{BASE_URL}/{slug}"
    retries = cfg.get("max_retries_per_category", 2)
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS,
                             timeout=cfg.get("request_timeout_seconds", 25))
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return parse_products(r.text, slug)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def parse_products(html, slug):
    """从品类页 HTML 解析商品。以 product-metadata 为权威来源。"""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for meta in soup.select("span.product-metadata[data-pid]"):
        pid = (meta.get("data-pid") or "").strip()
        if not pid:
            continue
        name = (meta.get("data-name") or "").strip()
        price = (meta.get("data-price") or "").strip()
        category = (meta.get("data-category") or "").strip()

        tile = meta.find_parent(class_="product") or meta.parent
        oos = False
        href = None
        img = None
        if tile is not None:
            text = tile.get_text(" ", strip=True).lower()
            oos = ("out of stock" in text) or ("sold out" in text)
            a = tile.find("a", href=True)
            if a:
                href = urljoin(BASE_URL, a["href"].split("?")[0])
            im = tile.find("img")
            if im and im.get("src"):
                img = urljoin(BASE_URL, im["src"])
        products.append({
            "pid": pid,
            "name": name or pid,
            "price": price,
            "category": category or slug,
            "url": href or f"{BASE_URL}/{slug}",
            "img": img,
            "oos": oos,
            "found_slug": slug,
        })
    return products


def merge_present(present, products):
    """把一个品类页的商品合并进本轮 present(原地修改)。

    同一 pid 会出现在多个品类页,而不同页面模板的售罄角标渲染可能不一致;
    若按"先扫到谁听谁的"(setdefault),结果会随品类顺序/失败情况随机变化,
    造成 oos↔in_stock 反复横跳、"补货"被重复触发。
    改为确定性聚合:任一处显示有货,即按有货记。"""
    for p in products:
        q = present.get(p["pid"])
        if q is None or (q["oos"] and not p["oos"]):
            present[p["pid"]] = p


def scan(cfg):
    """扫描所有品类。返回 (present:dict[pid->product], 在架总数, 品类数, 抓取失败品类数)。"""
    present = {}
    total = 0
    failed = 0
    cats = discover_categories(cfg)
    for slug in cats:
        try:
            products = fetch_category(slug, cfg)
        except Exception as e:
            failed += 1
            logger.warning("品类 %s 抓取失败(本轮跳过): %s", slug, e)
            continue
        total += len(products)
        merge_present(present, products)
        time.sleep(random.uniform(0.3, 0.7))
    return present, total, len(cats), failed


def scan_trustworthy(failed, present_count, known_count, ratio):
    """判断本轮扫描是否可信(用于决定是否处理'消失/下架')。
    有抓取失败、或在架数骤降到基线的 ratio 以下(疑似被风控喂空页),都视为不可信。"""
    if failed > 0:
        return False
    if known_count <= 0:
        return True
    return present_count >= known_count * ratio


# ---------------------------------------------------------------- 判新核心(纯函数,便于测试)
def compute_events(old_items, present, scan_clean, cfg, now=None):
    """根据上一轮状态与本轮在架商品,算出需要通知的商品和新状态。

    old_items: {pid: {"status","missing","oos_streak","notified",...}}
        status ∈ in_stock / oos / delisted。delisted 是"已下架档案":条目不删除,
        保留 notified 时间戳,这样商品短期内反复消失又出现时,通知冷却仍然生效。
    present:   {pid: product}
    scan_clean: 本轮扫描是否可信。不可信时:① 不处理"消失"(防偶发失败误判下架);
        ② 不做 oos→in_stock 的补货跃迁(防风控喂的残缺页缺售罄角标,把售罄误判回有货)。
    now: 注入当前时间戳(epoch 秒),便于测试;默认取 time.time()。
    返回 (to_notify:list[product(带 event)], new_items:dict)
    """
    if now is None:
        now = time.time()
    notify_oos = cfg.get("notify_out_of_stock", True)
    threshold = cfg.get("absence_cycles_before_relist", 2)
    oos_confirm = cfg.get("oos_cycles_before_confirm", 3)
    cooldown = cfg.get("notify_cooldown_hours", 12) * 3600
    retention = cfg.get("delisted_retention_days", 45) * 86400
    new_items = {pid: dict(v) for pid, v in old_items.items()}
    to_notify = []

    for pid, p in present.items():
        seen = "oos" if p["oos"] else "in_stock"
        prev = old_items.get(pid)
        event = None
        if prev is None or prev.get("status") == "delisted":
            event = "上新"                      # 从未见过 / 已判下架后重新出现
            entry = {"status": "in_stock", "missing": 0, "oos_streak": 0,
                     "notified": dict(prev["notified"]) if prev and prev.get("notified") else {}}
        else:
            entry = dict(prev)
            entry["missing"] = 0
        # 售罄一律要连续 oos_confirm 轮观测一致才确认(新条目也不例外,否则首见误判 oos
        # 会立刻给"补货"上膛,下一轮翻回有货就误推),防解析/页面抖动造成反复推送
        if seen == "oos":
            entry["oos_streak"] = entry.get("oos_streak", 0) + 1
            if entry.get("status") == "in_stock" and entry["oos_streak"] >= oos_confirm:
                entry["status"] = "oos"
        else:
            entry["oos_streak"] = 0
            if entry.get("status") == "oos" and scan_clean:
                event = "补货"                  # 确认售罄后又有货;不可信轮保持 oos 留待重判
                entry["status"] = "in_stock"

        if event and (seen == "in_stock" or notify_oos):  # 过滤门按本轮观测值,与旧语义一致
            last = entry.get("notified", {}).get(event, 0)
            if now - last >= cooldown:
                entry.setdefault("notified", {})[event] = int(now)
                q = dict(p)
                q["event"] = event
                to_notify.append(q)
            else:
                logger.info("冷却压制:%s %s 距上次同类推送仅 %.0f 分钟(<%dh),不重复推。",
                            pid, event, (now - last) / 60.0, cooldown // 3600)
        new_items[pid] = entry

    if scan_clean:  # 仅在干净扫描时处理"消失",避免偶发抓取失败误判为下架
        for pid in list(new_items):
            if pid in present:
                continue
            entry = new_items[pid]
            if entry.get("status") == "delisted":
                if now - entry.get("delisted_at", now) >= retention:
                    del new_items[pid]          # 下架档案过期清理(再出现仍按上新处理)
                continue
            entry["missing"] = entry.get("missing", 0) + 1
            if entry["missing"] >= threshold:
                entry["status"] = "delisted"    # 判定下架:保留档案而非删除,让冷却继续生效
                entry["delisted_at"] = int(now)
                entry["missing"] = 0
                entry["oos_streak"] = 0
    return to_notify, new_items


# ---------------------------------------------------------------- 通知
def push_serverchan(sendkey, title, desp):
    if not sendkey:
        logger.warning("未配置 serverchan_sendkey,跳过微信推送。")
        return False
    api = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(api, data={"title": title[:100], "desp": desp}, timeout=20)
        try:
            j = r.json()
        except Exception:
            logger.error("Server酱 返回非 JSON(HTTP %s): %s", r.status_code, r.text[:200])
            return False
        if j.get("code") == 0:
            return True
        logger.error("Server酱 推送失败: %s", j)
        return False
    except Exception as e:
        logger.error("Server酱 推送异常: %s", e)
        return False


def desktop_toast(title, message):
    """Windows 桌面通知(尽力而为,失败不影响主流程)。"""
    try:
        import subprocess

        def _san(s):
            return str(s).replace('"', "'").replace("`", "'").replace("$", "")
        title, message = _san(title), _san(message)
        ps = f'''
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $t.GetElementsByTagName('text')
$texts.Item(0).AppendChild($t.CreateTextNode("{title}")) | Out-Null
$texts.Item(1).AppendChild($t.CreateTextNode("{message}")) | Out-Null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Chrome Hearts Monitor").Show($n)
'''
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10)
    except Exception as e:
        logger.debug("桌面通知失败(忽略): %s", e)
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass


def build_message(new_products, cap):
    n = len(new_products)
    shown = new_products[:cap]
    title = f"🔥 Chrome Hearts 上新/补货 {n} 件"
    lines = [f"### 🔥 Chrome Hearts {n} 件动态\n",
             f"_{datetime.now():%Y-%m-%d %H:%M:%S}_\n"]
    for p in shown:
        tag = "🆕上新" if p.get("event") == "上新" else "🔄补货"
        stock = "❌已售罄" if p["oos"] else "✅有货"
        price = f"${p['price']}" if p["price"] else ""
        lines.append(f"**{tag} {p['name']}** {price}  {stock}")
        lines.append(f"- 分类:{p['category']}")
        lines.append(f"- 链接:{p['url']}")
        if p["img"]:
            lines.append(f"\n![]({p['img']})\n")
        lines.append("")
    if n > len(shown):
        lines.append(f"……另有 {n - len(shown)} 件,详见官网。")
    return title, "\n".join(lines)


def notify(cfg, new_products):
    """推送。返回微信是否成功(决定是否提交状态)。"""
    cap = cfg.get("max_items_per_message", 40)
    title, desp = build_message(new_products, cap)
    for p in new_products:
        logger.info("%s: %s %s %s -> %s", p.get("event", "上新"), p["name"],
                    p["price"], "OOS" if p["oos"] else "IN-STOCK", p["url"])
    ok = push_serverchan(cfg.get("serverchan_sendkey", ""), title, desp)
    logger.info("微信推送 %s", "成功 ✅" if ok else "失败 ⚠️(下轮重试)")
    if cfg.get("desktop_notification", True):
        names = "、".join(p["name"] for p in new_products[:3])
        desktop_toast(f"Chrome Hearts 上新/补货 {len(new_products)} 件", names)
    return ok


# ---------------------------------------------------------------- 一轮处理
_push_fail = {"fp": None, "count": 0}       # 同一批事件连续推送失败计数(进程内)
_health_breach = {"count": 0}               # 在架数持续低于基线的连续轮数(进程内)


def register_push_failure(fp, max_retries):
    """记录一次推送失败;同一批事件(指纹相同)连续失败满 max_retries 次时返回 True(放弃重试)。
    动机:Server酱可能"实际已送达但回包超时/报错",若无限重试,同一批事件会每轮重发轰炸用户;
    放弃重试后提交新状态,后续由 notified 冷却窗口兜底防重复。"""
    if _push_fail["fp"] != fp:
        _push_fail["fp"], _push_fail["count"] = fp, 0
    _push_fail["count"] += 1
    if _push_fail["count"] >= max_retries:
        _push_fail["fp"], _push_fail["count"] = None, 0
        return True
    return False


def process(cfg, old_items, first_run):
    """跑一轮,返回 (new_items, today)。"""
    present, _total, ncat, failed = scan(cfg)
    today = date.today().isoformat()
    n_present = len(present)  # 唯一 SKU 数(跨品类已去重),口径与健康度守门一致
    known = sum(1 for v in old_items.values() if v.get("status") != "delisted")
    ratio = cfg.get("min_scan_health_ratio", 0.8)
    scan_clean = scan_trustworthy(failed, n_present, known, ratio)
    if scan_clean:
        _health_breach["count"] = 0
    elif failed == 0 and known:
        # 在架数骤降但各品类抓取全成功:先按不可信处理;若持续太多轮,说明是官网真实缩水
        # 而非风控喂空页,必须接受新基线,否则守门会永久卡死(下架永远无法处理)。
        _health_breach["count"] += 1
        grace = cfg.get("health_breach_grace_rounds", 30)
        if _health_breach["count"] >= grace:
            logger.warning("在架数已连续 %d 轮低于基线(%d < %d×%.2f),视为官网真实缩水,接受新基线。",
                           _health_breach["count"], n_present, known, ratio)
            scan_clean = True
        else:
            logger.warning("本轮在架数骤降(%d < 基线%d×%.2f),疑似抓取异常,暂不处理下架/补货(连续第 %d 轮)。",
                           n_present, known, ratio, _health_breach["count"])
    suppress = first_run and cfg.get("first_run_silent", True)

    if suppress:
        # 基线条目同样遵循"oos 需连续确认"口径:先记 in_stock、种下 streak,由后续轮确认
        new_items = {pid: {"status": "in_stock", "missing": 0,
                           "oos_streak": 1 if p["oos"] else 0, "notified": {}}
                     for pid, p in present.items()}
        logger.info("扫描完成:品类 %d 个,在架 %d 件 —— 首次运行,建立基线 %d 个,不推送。",
                    ncat, n_present, len(new_items))
        save_state(new_items, today)
        return new_items, today

    to_notify, new_items = compute_events(old_items, present, scan_clean, cfg)
    logger.info("扫描完成:品类 %d 个(失败 %d),在架 %d 件,待通知 %d 件(上新/补货)。",
                ncat, failed, n_present, len(to_notify))

    if to_notify:
        fp = tuple(sorted((q["pid"], q["event"]) for q in to_notify))
        if notify(cfg, to_notify):
            _push_fail["fp"], _push_fail["count"] = None, 0
            save_state(new_items, today)        # 推送成功才提交新状态
            return new_items, today
        if register_push_failure(fp, cfg.get("max_push_retries", 3)):
            logger.error("同一批 %d 件事件连续推送失败达上限,放弃重试并提交状态"
                         "(防'实际已送达却报错'造成每轮重复轰炸;请检查 Server酱 配置/配额,可能漏推)。",
                         len(to_notify))
            save_state(new_items, today)
            return new_items, today
        logger.warning("推送失败(连续第 %d 次),不提交状态,下轮重试这 %d 件,避免漏推。",
                       _push_fail["count"], len(to_notify))
        save_state(old_items, today)            # 仅做每日心跳,事件留待下轮重算
        return old_items, today

    save_state(new_items, today)                # 无需通知:提交(含消失计数/心跳)
    return new_items, today


def monitor_loop(max_seconds=None, commit=False):
    """持续监控。max_seconds 不为空时跑满该时长就正常退出(GitHub 长跑'接力'用);
    commit=True 时每轮把变化的 state.json 提交回仓库(配合 COMMIT_STATE=1 生效)。"""
    cfg = load_config()
    items, _d, existed = load_state()
    first_run = not existed
    known = sum(1 for v in items.values() if v.get("status") != "delisted")
    logger.info("启动监控 | 静态品类 %d(+首页自动发现)| 间隔 %ds | 微信 %s | %s%s",
                len(cfg["categories"]), cfg["poll_interval_seconds"],
                "已配置" if cfg.get("serverchan_sendkey") else "未配置",
                "首次将建立基线" if first_run else f"已知商品 {known} 个",
                f" | 本次长跑 {max_seconds}s" if max_seconds else "")
    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    while True:
        try:
            items, day = process(cfg, items, first_run)
            first_run = False
            if commit:
                git_commit_state(day)
        except KeyboardInterrupt:
            logger.info("收到中断,退出。")
            break
        except Exception as e:
            logger.exception("本轮异常: %s", e)
        if deadline is not None and time.monotonic() >= deadline:
            logger.info("达到本次长跑时长上限,正常退出,交棒下一次运行。")
            break
        interval = cfg["poll_interval_seconds"]
        time.sleep(interval + random.uniform(0, interval * 0.2))


# ---------------------------------------------------------------- 命令行入口
def cmd_selftest():
    cfg = load_config()
    products_all = []
    for slug in ["scents", "socks", "intimates", "baccarat"]:
        try:
            ps = fetch_category(slug, cfg)
            products_all += ps
            logger.info("[selftest] %s 抓到 %d 件", slug, len(ps))
            for p in ps[:2]:
                logger.info("    %s | $%s | %s", p["name"], p["price"],
                            "OOS" if p["oos"] else "有货")
        except Exception as e:
            logger.error("[selftest] %s 失败: %s", slug, e)
    logger.info("[selftest] 合计 %d 件,%s", len(products_all),
                "抓取逻辑正常 ✅" if products_all else "没抓到商品,需检查 ⚠️")


def cmd_test_push():
    cfg = load_config()
    ok = push_serverchan(cfg.get("serverchan_sendkey", ""),
                         "✅ Chrome Hearts 监控测试",
                         "这是一条测试消息。收到说明 Server酱 配置成功。")
    logger.info("测试推送 %s", "成功 ✅" if ok else "失败 ⚠️,检查 sendkey")


def main():
    args = set(sys.argv[1:])
    if "--selftest" in args:
        cmd_selftest()
    elif "--test-push" in args:
        cmd_test_push()
    elif "--serve" in args:
        run_seconds = int(os.environ.get("RUN_SECONDS", "20700"))  # ~5h45m,留足 6h job 上限余量
        monitor_loop(max_seconds=run_seconds, commit=True)
    elif "--once" in args:
        cfg = load_config()
        items, _d, existed = load_state()
        process(cfg, items, first_run=not existed)
    else:
        monitor_loop()


if __name__ == "__main__":
    main()
