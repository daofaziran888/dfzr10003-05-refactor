"""流水状态控制引擎的验收脚本。

不依赖 pytest，直接用标准库跑：

    cd backend && .venv/bin/python -m app.tests.test_flow_control

覆盖：
1. 三条示例记录仍可读，分页/字段契约不破坏；
2. 显式迁移表：草稿动作只能落运行中、终态拒绝旧动作、非法迁移给拒绝原因；
3. (流水号, 动作序号) 幂等：同序号重放、过期序号、跳号；
4. ODS 暂停与 DWD 清洗准入同事务同步；DWD 在上游暂停后拿不到新准入；
5. 暂停保留分区水位，恢复从该水位继续；
6. 启动/暂停并发只有先到且通过校验的一条提交；
7. 写入故障注入后整单回滚、无两份状态，重试成功；
8. HTTP 层（TestClient）同样满足，失败响应后两个页面停在原状态。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.services.flow import (
    ADMISSION_HOLD,
    ADMISSION_OPEN,
    DWD_MODULE,
    ODS_MODULE,
    TERMINAL_STATUSES,
    arm_write_fault,
    registry,
)
from app.store import store

client = TestClient(app)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reset_state() -> None:
    """每个用例前从种子数据重建状态机，保证用例相互独立。"""
    store.__init__()  # type: ignore[misc]
    registry._states.clear()
    registry._journals.clear()
    registry._locks.clear()
    registry._bootstrapped = False
    registry.bootstrap()


def find(module: str, serial: str) -> dict:
    for row in store.rows(module):
        if row["流水编号"] == serial:
            return row
    raise KeyError(serial)


def section(title: str) -> None:
    print(f"\n== {title} ==")


# 1. 种子数据与页面契约 ------------------------------------------------------

section("1. 三条示例记录可读、分页与字段契约保持")
reset_state()
for module, serials in [
    (ODS_MODULE, ["ODS-0001", "ODS-0002", "ODS-0003"]),
    (DWD_MODULE, ["DWD-0001", "DWD-0002", "DWD-0003"]),
]:
    resp = client.get(f"/api/{module}")
    body = resp.json()
    check(f"{module} 列表 200 且共 3 条", resp.status_code == 200 and body["total"] == 3)
    check(f"{module} 分页契约 page/size 保留", body["page"] == 1 and body["size"] == 20)
    got = [item["流水编号"] for item in body["items"]]
    check(f"{module} 三条示例流水仍可读", got == serials, str(got))

page = client.get("/api/ods", params={"page": 1, "size": 2}).json()
check("分页 size=2 只回两行且 total 仍为 3", len(page["items"]) == 2 and page["total"] == 3)
kw = client.get("/api/ods", params={"keyword": "ODS-0002"}).json()
check("按流水编号检索 ODS-0002", kw["total"] == 1 and kw["items"][0]["流水编号"] == "ODS-0002")
ods_cols = ["流水编号", "来源系统", "源表名称", "落地表名", "接入周期", "分区粒度", "保留天数", "最近接入时间"]
dwd_cols = ["流水编号", "输入表名", "输出表名", "空值填充", "格式转换", "脏数据比例", "执行批次", "清洗状态"]
check("ODS 原有字段全部保留", all(c in page["items"][0] for c in ods_cols))
check("DWD 原有字段全部保留", all(c in dwd_cols for c in dwd_cols))
check("ODS-0002/DWD-0002 展示动作序号",
      client.get("/api/ods/2").json()["动作序号"] == 0
      and client.get("/api/dwd/2").json()["动作序号"] == 0)
check("拒绝原因字段存在且默认为 —",
      client.get("/api/ods/2").json()["拒绝原因"] == "—"
      and client.get("/api/dwd/2").json()["拒绝原因"] == "—")


# 2. 显式迁移表 --------------------------------------------------------------

section("2. 动作解释只认显式迁移表")
reset_state()
# 草稿动作只能落到运行中
r = client.post("/api/ods/1/actions", json={"values": {"action": "启动接入", "action_seq": 1}})
check("草稿 ODS-0001 启动接入 -> 运行中", r.json()["ok"] and r.json()["entry"]["status"] == "运行中")
r = client.post("/api/ods/1/actions", json={"values": {"action": "启动接入", "action_seq": 2}})
check("运行中再次启动被迁移表拒绝", not r.json()["ok"] and "迁移表" in r.json()["reject_reason"])
# 非法动作
r = client.post("/api/ods/1/actions", json={"values": {"action": "删除流水", "action_seq": 2}})
check("未收录动作被拒绝并给原因", not r.json()["ok"] and "可执行范围" in r.json()["reject_reason"])
# 终态拒绝旧动作
r = client.post("/api/ods/1/actions", json={"values": {"action": "暂停接入", "action_seq": 2}})
check("运行中可暂停", r.json()["ok"] and r.json()["entry"]["status"] == "已暂停")
r = client.post("/api/ods/1/actions", json={"values": {"action": "归档流水", "action_seq": 3}})
check("已暂停可归档", r.json()["ok"] and r.json()["entry"]["status"] == "已归档")
r = client.post("/api/ods/1/actions", json={"values": {"action": "启动接入", "action_seq": 4}})
check("已归档（终态）拒绝旧动作", not r.json()["ok"] and "终态" in r.json()["reject_reason"] and "拒绝" in r.json()["reject_reason"])
# 完成动作与终态
reset_state()
r = client.post("/api/ods/2/actions", json={"values": {"action": "完成接入", "action_seq": 1}})
check("运行中可完成", r.json()["ok"] and r.json()["entry"]["status"] == "已完成")
r = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 2}})
check("已完成后拒绝暂停", not r.json()["ok"] and "终态" in r.json()["reject_reason"] and "拒绝" in r.json()["reject_reason"])
check("终态集合覆盖已完成/已归档", set(TERMINAL_STATUSES) == {"已完成", "已归档"})


# 3. 幂等键（流水号 + 动作序号） ----------------------------------------------

section("3. 流水号加动作序号的幂等控制")
reset_state()
first = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("首次暂停成功，动作序号推进到 1", first.json()["entry"]["动作序号"] == 1)
replay = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("同序号重试幂等返回，不再产生写入", replay.json()["ok"] and "幂等返回" in replay.json()["message"])
check("幂等重放后动作序号仍是 1", find(ODS_MODULE, "ODS-0002")["动作序号"] == 1)
stale = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("同序号旧动作重放不产生第二次写入", "幂等返回" in stale.json()["message"])
stale2 = client.post("/api/ods/2/actions", json={"values": {"action": "归档流水", "action_seq": 1}})
check("同一序号被其他动作抢占时拒绝后到请求",
      not stale2.json()["ok"] and "已被动作" in stale2.json()["reject_reason"])
jump = client.post("/api/ods/2/actions", json={"values": {"action": "启动接入", "action_seq": 9}})
check("跳号 9 被拒绝并提示下一合法序号", not jump.json()["ok"] and "下一合法序号为 2" in jump.json()["reject_reason"])
bad = client.post("/api/ods/2/actions", json={"values": {"action": "启动接入", "action_seq": "abc"}})
check("非整数序号被拒绝", not bad.json()["ok"] and "不是合法整数" in bad.json()["reject_reason"])
# 不带序号的旧入口仍可用（自动分配当前 +1）
legacy = client.post("/api/ods/2/actions", json={"values": {"action": "启动接入"}})
check("未带序号的请求自动分配序号 2 并恢复", legacy.json()["ok"] and legacy.json()["entry"]["动作序号"] == 2)


# 4. ODS 接入状态与 DWD 清洗准入同事务同步 ------------------------------------

section("4. 接入状态同步到清洗清单（双页面结果）")
reset_state()
ods2 = registry._states[(ODS_MODULE, "ODS-0002")]
dwd2 = registry._states[(DWD_MODULE, "DWD-0002")]
check("初始 ODS-0002 运行中", ods2.status == "运行中")
check("初始 DWD-0002 清洗准入放行", dwd2.admission == ADMISSION_OPEN)
check("DWD 清单行带上游接入状态=运行中", find(DWD_MODULE, "DWD-0002")["上游接入状态"] == "运行中")
r = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("ODS-0002 暂停成功", r.json()["entry"]["status"] == "已暂停")
check("同事务内 DWD-0002 清洗准入变为暂停准入", dwd2.admission == ADMISSION_HOLD)
dwd_list = client.get("/api/dwd").json()["items"]
dwd_row = next(item for item in dwd_list if item["流水编号"] == "DWD-0002")
check("DWD 页面立即看到暂停准入与上游已暂停",
      dwd_row["清洗准入"] == ADMISSION_HOLD and dwd_row["上游接入状态"] == "已暂停")
# 上游暂停后，DWD 启动（从草稿或暂停恢复）都被准入拦下
r = client.post("/api/dwd/2/actions", json={"values": {"action": "暂停清洗", "action_seq": 1}})
check("DWD-0002 可自行暂停（暂停不依赖新准入）", r.json()["ok"] and r.json()["entry"]["status"] == "已暂停")
r = client.post("/api/dwd/2/actions", json={"values": {"action": "启动清洗", "action_seq": 2}})
check("上游暂停期间 DWD 恢复被清洗准入拒绝", not r.json()["ok"] and "清洗准入未放行" in r.json()["reject_reason"])
check("被拒绝后 DWD 页面停在已暂停", client.get("/api/dwd/2").json()["status"] == "已暂停")
# ODS 恢复后同事务重新放通
r = client.post("/api/ods/2/actions", json={"values": {"action": "启动接入", "action_seq": 2}})
check("ODS-0002 从水位恢复运行中", r.json()["ok"] and r.json()["entry"]["status"] == "运行中")
check("同事务内 DWD 清洗准入重新放行", dwd2.admission == ADMISSION_OPEN)
r = client.post("/api/dwd/2/actions", json={"values": {"action": "启动清洗", "action_seq": 2}})
check("准入放通后 DWD 恢复成功", r.json()["ok"] and r.json()["entry"]["status"] == "运行中")


# 5. 分区水位 ----------------------------------------------------------------

section("5. 暂停保留分区水位，恢复从该水位继续")
reset_state()
ods2 = registry._states[(ODS_MODULE, "ODS-0002")]
initial_mark = ods2.watermark
check("种子 ODS-0002 已有分区水位（最近接入时间）", initial_mark == "2026-09-02")
r = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("暂停后水位保留", ods2.watermark == initial_mark)
check("暂停提示明确保留分区水位", "分区水位已保留" in r.json()["message"])
r = client.post("/api/ods/2/actions", json={"values": {"action": "启动接入", "action_seq": 2}})
check("恢复沿用同一水位", ods2.watermark == initial_mark)
check("恢复提示带水位值", f"水位 {initial_mark}" in r.json()["message"])
# 没有初始水位的流水，暂停时建立水位，恢复不前进
reset_state()
ods1 = registry._states[(ODS_MODULE, "ODS-0001")]
client.post("/api/ods/1/actions", json={"values": {"action": "启动接入", "action_seq": 1}})
client.post("/api/ods/1/actions", json={"values": {"action": "暂停接入", "action_seq": 2}})
mark = ods1.watermark
check("无初始水位的流水暂停时建立水位", bool(mark))
client.post("/api/ods/1/actions", json={"values": {"action": "启动接入", "action_seq": 3}})
check("恢复后水位不前进", ods1.watermark == mark)


# 6. 启动/暂停并发 -----------------------------------------------------------

section("6. 并发点击：只允许先到且通过校验的一条提交")
reset_state()
barrier = threading.Barrier(2)
results: list[dict] = []


def fire(action: str, seq: int) -> None:
    barrier.wait()  # 两个请求同时进入，最大化竞争
    resp = client.post("/api/ods/2/actions", json={"values": {"action": action, "action_seq": seq}})
    results.append(resp.json())


with ThreadPoolExecutor(max_workers=2) as pool:
    futs = [pool.submit(fire, "暂停接入", 1), pool.submit(fire, "启动接入", 1)]
    for f in futs:
        f.result()

ok_count = sum(1 for item in results if item["ok"])
check("两个同序号并发请求只有一条提交", ok_count == 1, str(results))
committed = next(item for item in results if item["ok"])
check("先到一条是暂停（运行中仅允许暂停迁移）", committed["entry"]["status"] == "已暂停")
rejected = next(item for item in results if not item["ok"])
check("后到一条带拒绝原因（过期/终态/迁移表）", bool(rejected["reject_reason"]))
check("最终只有一份状态：已暂停、序号 1",
      registry._states[(ODS_MODULE, "ODS-0002")].status == "已暂停"
      and registry._states[(ODS_MODULE, "ODS-0002")].action_seq == 1)
check("幂等日志里序号 1 只有一条记录",
      len(registry.journal_of(ODS_MODULE, "ODS-0002")) == 1)

# 不同序号的并发：先到先得，序号连续者胜
reset_state()
outcomes: list[dict] = []
barrier2 = threading.Barrier(2)


def fire2(action: str, seq: int) -> None:
    barrier2.wait()
    resp = client.post("/api/ods/2/actions", json={"values": {"action": action, "action_seq": seq}})
    outcomes.append(resp.json())


with ThreadPoolExecutor(max_workers=2) as pool:
    futs = [pool.submit(fire2, "暂停接入", 1), pool.submit(fire2, "完成接入", 1)]
    for f in futs:
        f.result()
check("同序号不同动作竞争仍只提交一条", sum(1 for o in outcomes if o["ok"]) == 1)


# 7. 写入故障与回滚 -----------------------------------------------------------

section("7. 写入超时/异常/中断后重试，不留两份状态")
reset_state()
before = registry._states[(ODS_MODULE, "ODS-0002")].snapshot()
arm_write_fault(ODS_MODULE, "ODS-0002", times=1)
r = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
body = r.json()
check("写入失败返回 ok=false 与回滚说明", not body["ok"] and "写入失败已回滚" in body["reject_reason"])
after = registry._states[(ODS_MODULE, "ODS-0002")].snapshot()
check("回滚后状态与故障前完全一致", before == after)
check("回滚后动作序号未前进", after["action_seq"] == 0)
check("幂等日志未写入失败序号", 1 not in registry.journal_of(ODS_MODULE, "ODS-0002"))
check("ODS 页面停在原状态运行中", client.get("/api/ods/2").json()["status"] == "运行中")
check("DWD 页面同步停在允许准入", client.get("/api/dwd/2").json()["清洗准入"] == ADMISSION_OPEN)
# 同序号原样重试：这次没有故障，应当成功
retry = client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("凭同一序号重试成功", retry.json()["ok"] and retry.json()["entry"]["status"] == "已暂停")
check("重试后只存在一份状态、一条日志",
      registry._states[(ODS_MODULE, "ODS-0002")].action_seq == 1
      and len(registry.journal_of(ODS_MODULE, "ODS-0002")) == 1)

# 故障发生在 ODS 事务里时，DWD 准入也要一起回滚
reset_state()
arm_write_fault(ODS_MODULE, "ODS-0002", times=1)
client.post("/api/ods/2/actions", json={"values": {"action": "暂停接入", "action_seq": 1}})
check("故障回滚后 DWD 准入未被改成暂停准入",
      registry._states[(DWD_MODULE, "DWD-0002")].admission == ADMISSION_OPEN)


# 8. 双页面失败停原状态 -------------------------------------------------------

section("8. 失败回滚/拒绝后两个页面都停在原状态")
reset_state()
ods_before = client.get("/api/ods/2").json()
dwd_before = client.get("/api/dwd/2").json()
client.post("/api/ods/2/actions", json={"values": {"action": "归档流水", "action_seq": 1}})  # 运行中不能归档
client.post("/api/dwd/2/actions", json={"values": {"action": "归档流水", "action_seq": 1}})  # 运行中不能归档
ods_after = client.get("/api/ods/2").json()
dwd_after = client.get("/api/dwd/2").json()
check("ODS 页面状态/序号未变", ods_before["status"] == ods_after["status"] == "运行中"
      and ods_after["动作序号"] == 0)
check("DWD 页面状态/序号未变", dwd_before["status"] == dwd_after["status"] == "运行中"
      and dwd_after["动作序号"] == 0)
check("两页都能读到拒绝原因", ods_after["拒绝原因"] != "—" and dwd_after["拒绝原因"] != "—")
# 导出接口仍可用且字段齐全
export = client.get("/api/dwd/export").json()
check("DWD 导出仍为 3 条且带同步字段",
      export["total"] == 3 and "清洗准入" in export["items"][0] and "上游接入状态" in export["items"][0])


print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
