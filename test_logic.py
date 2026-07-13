# -*- coding: utf-8 -*-
"""判新核心逻辑测试:覆盖上新/补货/下架再上新/偶发失败/售罄去抖/通知冷却/跨品类聚合/推送重试等场景。"""
import chrome_hearts_monitor as m

CFG = {"notify_out_of_stock": True, "absence_cycles_before_relist": 2,
       "oos_cycles_before_confirm": 3, "notify_cooldown_hours": 12,
       "delisted_retention_days": 45}
T0 = 1_700_000_000  # 固定基准时间(epoch 秒),便于测冷却/档案过期
H = 3600
D = 86400

def prod(pid, oos=False, category="hoodie"):
    return {"pid": pid, "name": pid, "price": "750.00", "category": category,
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
to_notify, items = m.compute_events({}, {"A": prod("A")}, True, CFG, now=T0)
check("A 被通知为上新", names(to_notify) == [("A", "上新")])
check("A 进入状态且 in_stock", items["A"]["status"] == "in_stock")
check("记录了上新通知时间(用于冷却)", items["A"]["notified"]["上新"] == T0)

print("场景2:已知有货 SKU 持续在架 -> 不再重复推")
to_notify, items = m.compute_events({"A": {"status": "in_stock", "missing": 0}},
                                    {"A": prod("A")}, True, CFG, now=T0)
check("无通知", to_notify == [])

print("场景3:上新时售罄(oos) -> 仍通知(notify_oos),但 oos 状态同样需确认")
to_notify, items = m.compute_events({}, {"A": prod("A", oos=True)}, True, CFG, now=T0)
check("A 上新(售罄)被通知", names(to_notify) == [("A", "上新")])
check("首见 oos 只种 streak,不直接确认", items["A"]["status"] == "in_stock" and items["A"]["oos_streak"] == 1)
to_notify, items = m.compute_events(items, {"A": prod("A")}, True, CFG, now=T0 + 120)
check("首见误判 oos 后翻回有货 -> 不误推补货 🎯", to_notify == [])
st3 = items
for i in range(3):
    _, st3 = m.compute_events(st3, {"A": prod("A", oos=True)}, True, CFG, now=T0 + 240 + i * 120)
check("持续 oos 3 轮 -> 确认售罄", st3["A"]["status"] == "oos")
to_notify, st3 = m.compute_events(st3, {"A": prod("A")}, True, CFG, now=T0 + 700)
check("确认后回有货 -> 正常推补货", names(to_notify) == [("A", "补货")])

print("场景4:确认售罄 -> 回有货 -> 通知补货")
st = {"A": {"status": "oos", "missing": 0, "oos_streak": 3}}
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0)
check("A 被通知为补货", names(to_notify) == [("A", "补货")])
check("状态回 in_stock 且 streak 清零", st["A"]["status"] == "in_stock" and st["A"]["oos_streak"] == 0)

print("场景5:有货 -> 售罄要连续观测确认(去抖),期间不通知  ★修复重点")
st = {"A": {"status": "in_stock", "missing": 0, "oos_streak": 0}}
to_notify, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, CFG, now=T0)
check("单轮 oos 不立即改状态(streak=1)", st["A"]["status"] == "in_stock" and st["A"]["oos_streak"] == 1)
to_notify2, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, CFG, now=T0)
to_notify3, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, CFG, now=T0)
check("连续3轮 oos 才确认售罄", st["A"]["status"] == "oos")
check("确认过程全程无通知", to_notify == [] and to_notify2 == [] and to_notify3 == [])

print("场景6:oos↔in_stock 每轮抖动 -> 永不误报补货  ★★线上实锤的bug")
st = {"A": {"status": "in_stock", "missing": 0, "oos_streak": 0}}
all_notified = []
for i in range(6):  # oos,in,oos,in,oos,in 交替
    to_notify, st = m.compute_events(st, {"A": prod("A", oos=(i % 2 == 0))}, True, CFG, now=T0 + i * 120)
    all_notified += to_notify
check("抖动 6 轮零通知", all_notified == [])
check("状态始终 in_stock(去抖挡住了上膛)", st["A"]["status"] == "in_stock")

print("场景7:真补货推送后短期又售罄又回货 -> 冷却压制,超窗才再推  ★★线上实锤的bug")
st = {"A": {"status": "oos", "missing": 0, "oos_streak": 3}}
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0)
check("第一次补货正常推", names(to_notify) == [("A", "补货")])
for i in range(3):  # 再次连续 oos 确认
    _, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, CFG, now=T0 + 600 + i * 120)
check("再次确认售罄", st["A"]["status"] == "oos")
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 1 * H)
check("1小时内再回货 -> 冷却压制不推 🎯", to_notify == [])
check("但状态照常翻回 in_stock", st["A"]["status"] == "in_stock")
for i in range(3):
    _, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, CFG, now=T0 + 2 * H + i * 120)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 13 * H)
check("超过 12h 冷却后回货 -> 重新推", names(to_notify) == [("A", "补货")])

print("场景8:下架档案化 + 再上新 + 冷却  ★★线上实锤的bug")
st = {"A": {"status": "in_stock", "missing": 0, "oos_streak": 0}}
_, st = m.compute_events(st, {}, True, CFG, now=T0)
check("消失1轮后仍在状态(missing=1)", st["A"]["missing"] == 1)
_, st = m.compute_events(st, {}, True, CFG, now=T0 + 120)
check("消失达阈值 -> 标记 delisted(保留档案,不删除)", st["A"]["status"] == "delisted")
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 1 * H)
check("下架后再出现 -> 重新通知上新 🎯", names(to_notify) == [("A", "上新")])
_, st = m.compute_events(st, {}, True, CFG, now=T0 + 2 * H)
_, st = m.compute_events(st, {}, True, CFG, now=T0 + 2 * H + 120)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 3 * H)
check("短期内又消失又出现 -> 冷却压制,不再重复推上新 🎯", to_notify == [])
_, st = m.compute_events(st, {}, True, CFG, now=T0 + 4 * H)
_, st = m.compute_events(st, {}, True, CFG, now=T0 + 4 * H + 120)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 14 * H)
check("超过冷却窗口后再上新 -> 正常推", names(to_notify) == [("A", "上新")])

print("场景9:下架档案过期清理")
st = {"A": {"status": "delisted", "missing": 0, "oos_streak": 0,
            "notified": {"上新": T0}, "delisted_at": T0}}
_, st2 = m.compute_events(st, {}, True, CFG, now=T0 + 1 * D)
check("档案未过期 -> 保留", "A" in st2)
_, st2 = m.compute_events(st, {}, True, CFG, now=T0 + 46 * D)
check("档案超 45 天 -> 清理", "A" not in st2)

print("场景10:偶发抓取失败(scan_clean=False)时,消失不算下架(防误判)")
st = {"A": {"status": "in_stock", "missing": 0, "oos_streak": 0}}
_, st = m.compute_events(st, {}, False, CFG, now=T0)
check("失败轮不增加 missing,A 仍在状态", st["A"]["missing"] == 0 and "A" in st)
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 120)
check("A 重新出现不误报为上新", to_notify == [])

print("场景11:scan_clean=False 时不做补货跃迁(防残缺页误报)  ★修复重点")
st = {"A": {"status": "oos", "missing": 0, "oos_streak": 3}}
to_notify, st = m.compute_events(st, {"A": prod("A")}, False, CFG, now=T0)
check("不可信轮看到'有货'不推补货", to_notify == [])
check("状态保持 oos,留待干净轮重判", st["A"]["status"] == "oos")
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, CFG, now=T0 + 120)
check("下一干净轮确认有货 -> 补货只推这一次", names(to_notify) == [("A", "补货")])

print("场景12:notify_out_of_stock=False 时,售罄上新不推,但确认售罄后回有货要推")
cfg2 = dict(CFG, notify_out_of_stock=False)
to_notify, st = m.compute_events({}, {"A": prod("A", oos=True)}, True, cfg2, now=T0)
check("售罄上新不推(按配置)", to_notify == [])
check("被压制的事件不记通知时间(不占冷却)", st["A"].get("notified", {}) == {})
for i in range(2):  # 连同首见共 3 轮 oos -> 确认
    _, st = m.compute_events(st, {"A": prod("A", oos=True)}, True, cfg2, now=T0 + 120 + i * 120)
check("持续售罄被确认", st["A"]["status"] == "oos")
to_notify, st = m.compute_events(st, {"A": prod("A")}, True, cfg2, now=T0 + 600)
check("随后回有货 -> 补货推送", names(to_notify) == [("A", "补货")])

print("场景13:扫描健康度守门(防风控空页造成的误报)")
check("有失败 -> 不可信", m.scan_trustworthy(1, 50, 50, 0.8) is False)
check("在架骤降到基线八成以下 -> 不可信", m.scan_trustworthy(0, 30, 50, 0.8) is False)
check("在架正常 -> 可信", m.scan_trustworthy(0, 48, 50, 0.8) is True)
check("首轮无基线 -> 可信", m.scan_trustworthy(0, 0, 0, 0.8) is True)
big = {f"P{i}": {"status": "in_stock", "missing": 0} for i in range(50)}
usable = m.scan_trustworthy(0, 2, len(big), 0.8)        # present 仅2件 -> 不可信
_, st = m.compute_events(big, {"P0": prod("P0"), "P1": prod("P1")}, usable, CFG, now=T0)
check("空页骤降时不误删商品(其余48件仍在状态)", len(st) == 50)
check("且无一被标 delisted", all(v.get("status") != "delisted" for v in st.values()))

print("场景14:跨品类聚合确定性(任一处有货即有货,与扫描顺序无关)  ★修复重点")
p1 = {}
m.merge_present(p1, [prod("A", oos=True, category="shop")])
m.merge_present(p1, [prod("A", oos=False, category="hoodie")])
p2 = {}
m.merge_present(p2, [prod("A", oos=False, category="hoodie")])
m.merge_present(p2, [prod("A", oos=True, category="shop")])
check("先oos后有货 -> 有货", p1["A"]["oos"] is False)
check("先有货后oos -> 仍有货(顺序无关)", p2["A"]["oos"] is False)
p3 = {}
m.merge_present(p3, [prod("A", oos=True)])
m.merge_present(p3, [prod("A", oos=True)])
check("两处都 oos -> oos", p3["A"]["oos"] is True)

print("场景15:推送重试封顶(同一批事件连续失败满上限则放弃)")
m._push_fail["fp"], m._push_fail["count"] = None, 0
fp1 = (("A", "上新"),)
check("第1次失败 -> 继续重试", m.register_push_failure(fp1, 3) is False)
check("第2次失败 -> 继续重试", m.register_push_failure(fp1, 3) is False)
check("第3次失败 -> 放弃重试 🎯", m.register_push_failure(fp1, 3) is True)
check("放弃后计数清零", m._push_fail["count"] == 0)
m.register_push_failure(fp1, 3)
m.register_push_failure((("B", "补货"),), 3)
check("事件批次变化 -> 计数重置", m._push_fail["count"] == 1)
m._push_fail["fp"], m._push_fail["count"] = None, 0

print("场景16:兼容旧格式状态(缺 oos_streak/notified 字段)")
old = {"A": {"status": "oos", "missing": 0}, "B": {"status": "in_stock", "missing": 0}}
to_notify, st = m.compute_events(old, {"A": prod("A"), "B": prod("B", oos=True)}, True, CFG, now=T0)
check("旧格式 oos 条目回有货 -> 正常推补货", names(to_notify) == [("A", "补货")])
check("旧格式 in_stock 条目单轮 oos -> 去抖不改状态", st["B"]["status"] == "in_stock" and st["B"]["oos_streak"] == 1)

print(f"\n结果:通过 {passed} / 失败 {failed}")
exit(1 if failed else 0)
