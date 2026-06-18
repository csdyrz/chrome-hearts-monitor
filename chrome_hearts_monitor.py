# -*- coding: utf-8 -*-
"""
Chrome Hearts 官网服装上新监控
================================
轮询 chromehearts.com 的服装分类页,发现新上架(按商品 SKU 判新)就推送到微信(Server酱)。

官网是 Salesforce Commerce Cloud (Demandware),分类页服务端渲染,
每个商品带 <span class="product-metadata" data-pid/data-name/data-price/...>,
所以用普通 HTTP 请求即可解析,无需浏览器。

用法:
    python chrome_hearts_monitor.py            # 持续监控(主用法)
    python chrome_hearts_monitor.py --once     # 只跑一轮就退出(调试用)
    python chrome_hearts_monitor.py --selftest # 用香水/袜子分类验证抓取逻辑(无需 sendkey)
    python chrome_hearts_monitor.py --test-push# 发一条测试消息到微信,验证 Server酱 配置
"""
import sys
import os
import json
import time
import random
import logging
from datetime import datetime

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
    """返回 (seen_pids:set, existed:bool)。existed=False 表示首次运行。"""
    if not os.path.exists(STATE_PATH):
        return set(), False
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_pids", [])), True
    except Exception as e:
        logger.warning("状态文件读取失败,当作首次运行: %s", e)
        return set(), False


def save_state(seen_pids):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen_pids": sorted(seen_pids),
                   "updated_at": datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)  # 原子替换,避免写一半损坏


# ---------------------------------------------------------------- 抓取 / 解析
def fetch_category(slug, cfg):
    """抓一个分类页,返回商品 dict 列表。失败抛异常。"""
    url = f"{BASE_URL}/{slug}"
    last_err = None
    for attempt in range(cfg.get("max_retries_per_category", 2) + 1):
        try:
            r = requests.get(url, headers=HEADERS,
                             timeout=cfg.get("request_timeout_seconds", 25))
            r.raise_for_status()
            return parse_products(r.text, slug)
        except Exception as e:
            last_err = e
            if attempt < cfg.get("max_retries_per_category", 2):
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def parse_products(html, slug):
    """从分类页 HTML 解析商品。以 product-metadata 标签为权威来源。"""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for meta in soup.select("span.product-metadata[data-pid]"):
        pid = (meta.get("data-pid") or "").strip()
        if not pid:
            continue
        name = (meta.get("data-name") or "").strip()
        price = (meta.get("data-price") or "").strip()
        category = (meta.get("data-category") or "").strip()

        # 找所属 tile,判断库存与取链接/图片
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


# ---------------------------------------------------------------- 通知
def push_serverchan(sendkey, title, desp):
    if not sendkey:
        logger.warning("未配置 serverchan_sendkey,跳过微信推送。")
        return False
    api = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        r = requests.post(api, data={"title": title[:100], "desp": desp}, timeout=20)
        j = r.json()
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
        # 防止商品名里的引号/反引号破坏 PowerShell 命令
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


def build_message(new_products):
    n = len(new_products)
    title = f"🔥 Chrome Hearts 上新 {n} 件"
    lines = [f"### 🔥 Chrome Hearts 上新 {n} 件\n",
             f"_{datetime.now():%Y-%m-%d %H:%M:%S}_\n"]
    for p in new_products:
        stock = "❌已售罄" if p["oos"] else "✅有货"
        price = f"${p['price']}" if p["price"] else ""
        lines.append(f"**{p['name']}** {price}  {stock}")
        lines.append(f"- 分类:{p['category']}")
        lines.append(f"- 链接:{p['url']}")
        if p["img"]:
            lines.append(f"\n![]({p['img']})\n")
        lines.append("")
    return title, "\n".join(lines)


# ---------------------------------------------------------------- 主循环
def run_cycle(cfg, seen, first_run):
    """跑一轮:返回本轮新发现的商品列表(已按 pid 去重)。会就地更新 seen。"""
    new_products = []
    new_pids_this_cycle = set()
    total_seen_now = 0
    for slug in cfg["categories"]:
        try:
            products = fetch_category(slug, cfg)
        except Exception as e:
            logger.warning("分类 %s 抓取失败: %s", slug, e)
            continue
        total_seen_now += len(products)
        for p in products:
            pid = p["pid"]
            if pid in seen or pid in new_pids_this_cycle:
                continue
            new_pids_this_cycle.add(pid)
            if not p["oos"] or cfg.get("notify_out_of_stock", True):
                new_products.append(p)
        # 礼貌性间隔,降低风控触发概率
        time.sleep(random.uniform(0.4, 1.0))

    # 更新 seen(首次运行也要记,只是按配置可不推送)
    seen |= new_pids_this_cycle
    suppress = first_run and cfg.get("first_run_silent", True)
    logger.info("本轮扫描完成:在架商品 %d 件,新增 SKU %d 个%s",
                total_seen_now, len(new_pids_this_cycle),
                "(首次运行,仅建立基线)" if suppress else "")
    return [] if suppress else new_products


def notify(cfg, new_products):
    title, desp = build_message(new_products)
    for p in new_products:
        logger.info("上新: %s %s %s -> %s",
                    p["name"], p["price"], "OOS" if p["oos"] else "IN-STOCK", p["url"])
    ok = push_serverchan(cfg.get("serverchan_sendkey", ""), title, desp)
    logger.info("微信推送 %s", "成功 ✅" if ok else "未发送/失败 ⚠️")
    if cfg.get("desktop_notification", True):
        names = "、".join(p["name"] for p in new_products[:3])
        desktop_toast(f"Chrome Hearts 上新 {len(new_products)} 件", names)


def monitor_loop():
    cfg = load_config()
    seen, existed = load_state()
    first_run = not existed
    logger.info("启动监控 | 分类 %d 个 | 间隔 %ds | 微信 %s | %s",
                len(cfg["categories"]), cfg["poll_interval_seconds"],
                "已配置" if cfg.get("serverchan_sendkey") else "未配置",
                "首次运行将建立基线" if first_run else f"已知 SKU {len(seen)} 个")
    while True:
        try:
            before = len(seen)
            new_products = run_cycle(cfg, seen, first_run)
            if new_products:
                notify(cfg, new_products)
            if len(seen) != before or first_run:
                save_state(seen)
            first_run = False
        except KeyboardInterrupt:
            logger.info("收到中断,退出。")
            break
        except Exception as e:
            logger.exception("本轮异常: %s", e)
        # 间隔加随机抖动
        interval = cfg["poll_interval_seconds"]
        time.sleep(interval + random.uniform(0, interval * 0.2))


# ---------------------------------------------------------------- 命令行入口
def cmd_selftest():
    """用香水/袜子分类验证抓取(这俩常年有货),不需要 sendkey。"""
    cfg = load_config()
    cfg["categories"] = ["scents", "socks"]
    seen = set()
    products_all = []
    for slug in cfg["categories"]:
        try:
            ps = fetch_category(slug, cfg)
            products_all += ps
            logger.info("[selftest] %s 抓到 %d 件", slug, len(ps))
            for p in ps[:3]:
                logger.info("    %s | $%s | %s | %s",
                            p["name"], p["price"], "OOS" if p["oos"] else "有货", p["url"])
        except Exception as e:
            logger.error("[selftest] %s 失败: %s", slug, e)
    logger.info("[selftest] 合计 %d 件,抓取逻辑正常 ✅" if products_all
                else "[selftest] 没抓到商品,需检查 ⚠️", len(products_all))


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
        seen, existed = load_state()
        first_run = not existed
        before = len(seen)
        new_products = run_cycle(cfg, seen, first_run)
        if new_products:
            notify(cfg, new_products)
        # 首次运行即使没货也要落盘建基线,否则云端会把"第一次上新"误当首次而静默
        if len(seen) != before or first_run:
            save_state(seen)
    else:
        monitor_loop()


if __name__ == "__main__":
    main()
