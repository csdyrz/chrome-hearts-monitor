# -*- coding: utf-8 -*-
"""判新核心逻辑测试:覆盖上新/补货/下架再上新/偶发失败等场景。"""
import chrome_hearts_monitor as m

CFG = {"notify_out_of_stock": True, "absence_cycles_before_relist": 2}

def prod(pid, oos=False):
    return {"pid": pid, "name": pid, "price": "750.00", "category": "hoodie",
            "url": "u", "img": None, "oos": oos}

def names(to_notify):
    return sorted((p["pid"], p["event"]) for p in to_notify)

passed = failed = 0
def check(desc, cond):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {desc}")
    else:
        failed += 1; print(f"  ❌ {desc}")

print("场景1:全新 SKU 出现 -> 上新")
to_notify, items = m.compute_events({}, {"A": prod("A")}, True, CFG)
check("A 被通知为上新", names(to_notify) == [("A", "上新")])
check("A 进入状态且 in_stock", items["A"]["status"] == "in_stock")

print("场景2:已知有货 SKU 持续在架 -> 不再重复推")
to_notify, items = m.compute_events({"A": {"status": "in_stock", "missing": 0}},
                                    {"A": prod("A")}, True, CFG)
check("无通知", to_notify == [])

print("场景3:上新时售罄(oos) -> 仍通知(用户要求 notify_oos)")
to_notify, items = m.compute_events({}, {"A": prod("A", oos=True)}, True, CFG)
check("A 上新(售罄)被通知", names(to_notify) == [("A", "上新")])
check("状态记为 oos", items["A"]["status"] == "oos")

print("场景4:售罄 -> 补货(同款回有货) -> 通知补货  ★你担心的同源bug")
to_notify, items = m.compute_events({"A": {"status": "oos", "missing": 0}},
                                    {"A": prod("A", oos=False)}, True, CFG)
check("A 被通知为补货", names(to_notify) == [("A", "补货")])

print("场景5:有货 -> 变售罄 -> 不通知(只是卖光,不是上新)")
to_notify, items = m.compute_events({"A": {"status": "in_stock", "missing": 0}},
                                    {"A": prod("A", oos=True)}, True, CFG)
check("无通知", to_notify == [])
check("状态更新为 oos", items["A"]["status"] == "oos")

print("场景6:下架(连续消失到阈值)-> 再次上新 -> 重新通知  ★★你最担心的bug")
st = {"A": {"status": "in_stock", "missing": 0}}
# 第1轮消失(干净扫描)
_, st = m.compute_events(st, {}, True, CFG)
check("消失1轮后仍在状态(missing=1)", st.get("A", {}).get("missing") == 1)
# 第2轮消失 -> 达阈值,移出状态(判定已下架)
_, st = m.compute_events(st, {}, True, CFG)
check("消失达阈值后被移出状态(判定下架)", "A" not in st)
# 再次上新
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG)
check("同款再次上新被重新通知 🎯", names(to_notify) == [("A", "上新")])

print("场景7:偶发抓取失败(scan_clean=False)时,商品暂时消失不算下架(防误判)")
st = {"A": {"status": "in_stock", "missing": 0}}
_, st = m.compute_events(st, {}, False, CFG)  # 失败轮
check("失败轮不增加 missing,A 仍在状态", st["A"]["missing"] == 0 and "A" in st)
# 紧接着干净轮 A 又出现 -> 不应误报(因为没被判下架)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG)
check("A 重新出现不误报为上新", to_notify == [])

print("场景8:短暂闪断(只消失1轮<阈值)后又出现 -> 不误报")
st = {"A": {"status": "in_stock", "missing": 0}}
_, st = m.compute_events(st, {}, True, CFG)        # 消失1轮(<2)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG)
check("闪断后回归不误报", to_notify == [])

print("场景9:notify_out_of_stock=False 时,售罄上新不推,但回有货要推")
cfg2 = {"notify_out_of_stock": False, "absence_cycles_before_relist": 2}
to_notify, st = m.compute_events({}, {"A": prod("A", oos=True)}, True, cfg2)
check("售罄上新不推(按配置)", to_notify == [])
to_notify, st = m.compute_events(st, {"A": prod("A", oos=False)}, True, cfg2)
check("随后回有货 -> 补货推送", names(to_notify) == [("A", "补货")])

print("场景10:阈值=1 时,下架仅1轮即可,再上新立即重新通知")
cfg1 = {"notify_out_of_stock": True, "absence_cycles_before_relist": 1}
st = {"A": {"status": "in_stock", "missing": 0}}
_, st = m.compute_events(st, {}, True, cfg1)            # 干净扫描消失1轮
check("阈值1:消失1轮即判下架(移出状态)", "A" not in st)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, cfg1)
check("阈值1:再上新立即通知", names(to_notify) == [("A", "上新")])

print("场景11:扫描健康度守门(防风控空页造成的误报)")
check("有失败 -> 不可信", m.scan_trustworthy(1, 50, 50, 0.5) is False)
check("在架骤降到基线一半以下 -> 不可信", m.scan_trustworthy(0, 3, 50, 0.5) is False)
check("在架正常 -> 可信", m.scan_trustworthy(0, 48, 50, 0.5) is True)
check("首轮无基线 -> 可信", m.scan_trustworthy(0, 0, 0, 0.5) is True)
# 端到端:阈值=1 但风控喂空页(present 骤降)时,不应把商品判下架
big = {f"P{i}": {"status": "in_stock", "missing": 0} for i in range(50)}
usable = m.scan_trustworthy(0, 2, len(big), 0.5)        # present 仅2件 -> 不可信
_, st = m.compute_events(big, {"P0": prod("P0"), "P1": prod("P1")}, usable, cfg1)
check("空页骤降时不误删商品(其余48件仍在状态)", len(st) == 50)

print(f"\n结果:通过 {passed} / 失败 {failed}")
exit(1 if failed else 0)
