# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网全品类上新监控
================================
轮询 chromehearts.com 的所有品类,发现任何新上架商品(按商品 SKU 判新)就推送到微信(Server酱)。

官网是 Salesforce Commerce Cloud (Demandware),品类页服务端渲染,
每个商品带 <span class="product-metadata" data-pid/data-name/data-price/...>,
所以用普通 HTTP 请求即可解析,无需浏览器。

品类来源 = 配置里的静态清单 ∪ 每轮从首页导航自动发现的品类(确保任何新品类也能覆盖)。

用法:
    python chrome_hearts_monitor.py            # 持续监控(本地常驻用法)
    python chrome_hearts_monitor.py --once     # 只跑一轮就退出(GitHub Actions 云端用法)
    python chrome_hearts_monitor.py --selftest # 用香水/袜子验证抓取(无需 sendkey)
    python chrome_hearts_monitor.py --test-push# 发一条测试消息到微信,验证 Server酱 配置
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
# 首页导航里这些不是商品品类,自动发现时排除
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
    # 环境变量优先(GitHub Actions 用 Secret 注入,避免把 key 写进公开仓库)
    env_key = os.environ.get("SERVERCHAN_SENDKEY")
    if env_key:
        cfg["serverchan_sendkey"] = env_key.strip()
    return cfg


def load_state():
    """返回 (seen_pids:set, stored_date:str, existed:bool)。existed=False 表示首次运行。"""
    if not os.path.exists(STATE_PATH):
        return set(), "", False
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_pids", [])), data.get("date", ""), True
    except Exception as e:
        logger.warning("状态文件读取失败,当作首次运行: %s", e)
        return set(), "", False


def save_state(seen_pids, day):
    """确定性写入:内容只取决于 seen + 日期,便于 git 仅在真有变化(含每日心跳)时提交。"""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen_pids": sorted(seen_pids), "date": day},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)  # 原子替换,避免写一半损坏


# ---------------------------------------------------------------- 品类发现 / 抓取
def discover_categories(cfg):
    """静态清单 ∪ 首页导航里出现的品类(自动覆盖官网当前/新增的任何品类)。"""
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
    """抓一个品类页,返回商品 dict 列表。404 视为空品类返回 []。其它失败抛异常。"""
    url = f"{BASE_URL}/{slug}"
    retries = cfg.get("max_retries_per_category", 2)
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS,
                             timeout=cfg.get("request_timeout_seconds", 25))
            if r.status_code == 404:
                return []  # 该品类当前不存在,视为空,不报错不重试
            r.raise_for_status()
            return parse_products(r.text, slug)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def parse_products(html, slug):
    """从品类页 HTML 解析商品。以 product-metadata 标签为权威来源。"""
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


def scan(cfg, seen):
    """扫描所有品类,返回 (未见过的商品列表, 在架总数, 品类数)。不修改 seen。"""
    candidates = {}
    total = 0
    cats = discover_categories(cfg)
    for slug in cats:
        try:
            products = fetch_category(slug, cfg)
        except Exception as e:
            logger.warning("品类 %s 抓取失败(本轮跳过,不影响判新): %s", slug, e)
            continue
        total += len(products)
        for p in products:
            pid = p["pid"]
            if pid in seen or pid in candidates:
                continue
            candidates[pid] = p
        time.sleep(random.uniform(0.3, 0.7))  # 礼貌性间隔,降低风控概率
    return list(candidates.values()), total, len(cats)


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

        def _san(s):  # 防止商品名里的引号/反引号破坏 PowerShell 命令
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
    title = f"🔥 Chrome Hearts 上新 {n} 件"
    lines = [f"### 🔥 Chrome Hearts 上新 {n} 件\n",
             f"_{datetime.now():%Y-%m-%d %H:%M:%S}_\n"]
    for p in shown:
        stock = "❌已售罄" if p["oos"] else "✅有货"
        price = f"${p['price']}" if p["price"] else ""
        lines.append(f"**{p['name']}** {price}  {stock}")
        lines.append(f"- 分类:{p['category']}")
        lines.append(f"- 链接:{p['url']}")
        if p["img"]:
            lines.append(f"\n![]({p['img']})\n")
        lines.append("")
    if n > len(shown):
        lines.append(f"……另有 {n - len(shown)} 件,详见官网。")
    return title, "\n".join(lines)


def notify(cfg, new_products):
    """推送新品。返回微信推送是否成功(用于决定是否标记已见)。"""
    cap = cfg.get("max_items_per_message", 40)
    title, desp = build_message(new_products, cap)
    for p in new_products:
        logger.info("上新: %s %s %s -> %s",
                    p["name"], p["price"], "OOS" if p["oos"] else "IN-STOCK", p["url"])
    ok = push_serverchan(cfg.get("serverchan_sendkey", ""), title, desp)
    logger.info("微信推送 %s", "成功 ✅" if ok else "失败 ⚠️(下轮重试)")
    if cfg.get("desktop_notification", True):
        names = "、".join(p["name"] for p in new_products[:3])
        desktop_toast(f"Chrome Hearts 上新 {len(new_products)} 件", names)
    return ok


# ---------------------------------------------------------------- 一轮处理(--once 与常驻循环共用)
def process(cfg, seen, first_run):
    """跑一轮:就地更新 seen,落盘状态。返回今天日期字符串。"""
    candidates, total, ncat = scan(cfg, seen)
    cand_pids = {p["pid"] for p in candidates}
    today = date.today().isoformat()
    suppress = first_run and cfg.get("first_run_silent", True)
    logger.info("扫描完成:品类 %d 个,在架 %d 件,未见过 %d 个%s",
                ncat, total, len(cand_pids),
                "(首次运行,仅建立基线,不推送)" if suppress else "")

    if suppress:
        seen |= cand_pids
        save_state(seen, today)
        return today

    to_notify = [p for p in candidates
                 if (not p["oos"]) or cfg.get("notify_out_of_stock", True)]
    if to_notify:
        if notify(cfg, to_notify):
            seen |= cand_pids            # 推送成功才标记已见
        else:
            logger.warning("推送失败,本轮不标记这 %d 个,下轮会重试,避免漏推。", len(to_notify))
    else:
        seen |= cand_pids                # 没有需要推送的(空或全被抑制),直接标记

    # 始终落盘:内容确定性,git 仅在 seen 变化或跨天(心跳,防定时任务被禁用)时才提交
    save_state(seen, today)
    return today


# ---------------------------------------------------------------- 常驻循环(本地用)
def monitor_loop():
    cfg = load_config()
    seen, _stored_date, existed = load_state()
    first_run = not existed
    logger.info("启动监控 | 静态品类 %d 个(+首页自动发现)| 间隔 %ds | 微信 %s | %s",
                len(cfg["categories"]), cfg["poll_interval_seconds"],
                "已配置" if cfg.get("serverchan_sendkey") else "未配置",
                "首次运行将建立基线" if first_run else f"已知 SKU {len(seen)} 个")
    while True:
        try:
            process(cfg, seen, first_run)
            first_run = False
        except KeyboardInterrupt:
            logger.info("收到中断,退出。")
            break
        except Exception as e:
            logger.exception("本轮异常: %s", e)
        interval = cfg["poll_interval_seconds"]
        time.sleep(interval + random.uniform(0, interval * 0.2))


# ---------------------------------------------------------------- 命令行入口
def cmd_selftest():
    """用香水/袜子验证抓取(常年有货),不需要 sendkey。"""
    cfg = load_config()
    products_all = []
    for slug in ["scents", "socks", "intimates", "baccarat"]:
        try:
            ps = fetch_category(slug, cfg)
            products_all += ps
            logger.info("[selftest] %s 抓到 %d 件", slug, len(ps))
            for p in ps[:2]:
                logger.info("    %s | $%s | %s | %s",
                            p["name"], p["price"], "OOS" if p["oos"] else "有货", p["url"])
        except Exception as e:
            logger.error("[selftest] %s 失败: %s", slug, e)
    logger.info("[selftest] 合计 %d 件,%s", len(products_all),
                "抓取逻辑正常 ✅" if products_all else "没抓到商品,需检查 ⚠️")


def cmd_test_push():
    cfg = load_config()
    ok = push_serverchan(cfg.get("serverchan_sendkey", ""),
                         "✅ Chrome Hearts 监控测试",
                         "这是一条测试消息。如果你在微信收到它,说明 Server酱 配置成功。")
    logger.info("测试推送 %s", "成功 ✅,去微信看看" if ok else "失败 ⚠️,检查 sendkey")


def main():
    args = set(sys.argv[1:])
    if "--selftest" in args:
        cmd_selftest()
    elif "--test-push" in args:
        cmd_test_push()
    elif "--once" in args:
        cfg = load_config()
        seen, _stored_date, existed = load_state()
        process(cfg, seen, first_run=not existed)
    else:
        monitor_loop()


if __name__ == "__main__":
    main()
