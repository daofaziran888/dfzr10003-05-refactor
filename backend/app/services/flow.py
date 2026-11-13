"""流水状态控制引擎。

值班场景里的两个老问题在这里收口：

1. 一条 ODS 流水被暂停后，清洗调度仍可能读到上一分钟的“可用快照”——
   ODS 接入状态与 DWD 清洗准入必须在**同一次事务**里落库，要么一起生效，要么一起回滚。
2. 同一瞬间连续点击启动/暂停，两个入口各自解释动作、最后到达的请求覆盖先到的——
   以 ``(流水编号, 动作序号)`` 作为幂等键，动作解释只认显式迁移表，并发时只有
   先到且通过校验的一条可以提交。

实现上这是一个进程内的确定性状态机：

* 每条流水一把可重入锁，事务期间对“本流水 + 配对流水”按全局顺序加锁，杜绝互锁；
* 提交前对所有参与写入的状态做快照，写入阶段任何异常都会按快照回滚，绝不留下两份状态；
* 幂等日志记录已提交的 ``(流水编号, 动作序号)``，超时/中断后的重试拿到的是同一份结果；
* 迁移表显式声明每个动作允许的源状态与目标状态，终态（已完成/已归档）拒绝一切旧动作。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.store import store

# 模块约定
ODS_MODULE = "ods"
DWD_MODULE = "dwd"

# 终态：进入之后拒绝任何动作（“完成或归档后拒绝旧动作”）
TERMINAL_STATUSES = ("已完成", "已归档")

# 显式动作迁移表：动作 -> (允许的源状态集合, 目标状态)。
# 草稿动作只能落到运行中；运行中可以暂停、完成；已暂停可以从分区水位恢复、完成或归档。
# 动作归属哪个模块由 TRANSITIONS_BY_MODULE 决定，本表只描述状态解释。
TRANSITIONS: dict[str, dict[str, Any]] = {
    "启动接入": {
        "sources": ("草稿", "已暂停"),
        "target": "运行中",
        "succeeded": "接入流水已启动",
        "resumed": "接入流水已从分区水位恢复",
    },
    "暂停接入": {
        "sources": ("运行中",),
        "target": "已暂停",
        "succeeded": "接入流水已暂停，分区水位已保留",
    },
    "完成接入": {
        "sources": ("运行中", "已暂停"),
        "target": "已完成",
        "succeeded": "接入流水已完成",
    },
    "启动清洗": {
        "sources": ("草稿", "已暂停"),
        "target": "运行中",
        "succeeded": "清洗流水已启动",
        "resumed": "清洗流水已从分区水位恢复",
        # DWD 启动还要过上游 ODS 清洗准入这一关
        "require_upstream_admission": True,
    },
    "暂停清洗": {
        "sources": ("运行中",),
        "target": "已暂停",
        "succeeded": "清洗流水已暂停，分区水位已保留",
    },
    "完成清洗": {
        "sources": ("运行中", "已暂停"),
        "target": "已完成",
        "succeeded": "清洗流水已完成",
    },
    "归档流水": {
        "sources": ("已暂停",),
        "target": "已归档",
        "succeeded": "流水已归档",
    },
}

# 归档动作对 ODS/DWD 两个入口都开放（页面契约里两个页面都有“归档流水”按钮），
# DWD 侧归档同样只允许从已暂停进入。
TRANSITIONS_BY_MODULE: dict[str, set[str]] = {
    ODS_MODULE: {"启动接入", "暂停接入", "完成接入", "归档流水"},
    DWD_MODULE: {"启动清洗", "暂停清洗", "完成清洗", "归档流水"},
}

ADMISSION_OPEN = "允许准入"
ADMISSION_HOLD = "暂停准入"

# 测试/故障演练用：在事务写入阶段注入一次异常，验证快照回滚。
# 键为 (module, 流水编号)，值为剩余触发次数。
_write_faults: dict[tuple[str, str], int] = {}
_fault_lock = threading.Lock()


def arm_write_fault(module: str, serial: str, times: int = 1) -> None:
    """安排接下来 ``times`` 次该流水的事务在写入阶段抛出异常（仅供测试/演练）。"""
    with _fault_lock:
        _write_faults[(module, serial)] = _write_faults.get((module, serial), 0) + times


def _consume_write_fault(module: str, serial: str) -> bool:
    with _fault_lock:
        remaining = _write_faults.get((module, serial), 0)
        if remaining <= 0:
            return False
        if remaining == 1:
            _write_faults.pop((module, serial), None)
        else:
            _write_faults[(module, serial)] = remaining - 1
        return True


class WriteFailed(RuntimeError):
    """写入阶段异常，已按快照回滚；调用方可以原样重试，不会产生两份状态。"""


@dataclass
class FlowState:
    """一条流水的受控状态。状态机字段与展示用的清单行分开存放。"""

    module: str
    serial: str
    status: str
    action_seq: int = 0
    watermark: str = ""
    admission: str = ADMISSION_HOLD
    last_reject_reason: str = ""

    def snapshot(self) -> dict[str, Any]:
        # 只快照事务受控字段；拒绝原因是最近一次失败的注解，不参与状态回滚。
        return {
            "status": self.status,
            "action_seq": self.action_seq,
            "watermark": self.watermark,
            "admission": self.admission,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.status = snap["status"]
        self.action_seq = snap["action_seq"]
        self.watermark = snap["watermark"]
        self.admission = snap["admission"]

    def public_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "动作序号": self.action_seq,
            "分区水位": self.watermark or "未建立水位",
            "清洗准入": self.admission if self.module == DWD_MODULE else "",
            "拒绝原因": self.last_reject_reason or "—",
        }


@dataclass
class JournalEntry:
    """已提交动作的幂等记录：同一 (流水编号, 动作序号) 重试只回放这一条。"""

    key: tuple[str, int]
    action: str
    from_status: str
    to_status: str
    message: str
    result: dict[str, Any] = field(default_factory=dict)


def pair_serial(module: str, serial: str) -> str | None:
    """ODS-0002 与 DWD-0002 按编号尾数配对；非标准编号没有配对流水。"""
    if "-" not in serial:
        return None
    prefix, tail = serial.split("-", 1)
    if module == ODS_MODULE and prefix == "ODS":
        return f"DWD-{tail}"
    if module == DWD_MODULE and prefix == "DWD":
        return f"ODS-{tail}"
    return None


def pair_key_of(module: str, serial: str) -> tuple[str, str] | None:
    """配对流水的状态表键：注意模块随前缀切换，不能沿用当前流水的模块。"""
    paired = pair_serial(module, serial)
    if paired is None:
        return None
    paired_module = DWD_MODULE if module == ODS_MODULE else ODS_MODULE
    return paired_module, paired


class FlowRegistry:
    """全量流水的状态登记表，进程内单例。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], FlowState] = {}
        self._journals: dict[tuple[str, str], dict[int, JournalEntry]] = {}
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._global = threading.RLock()
        self._bootstrapped = False

    # ---------- 初始化与登记 ----------

    def bootstrap(self) -> None:
        """从示例清单装载初始受控状态，只执行一次。"""
        with self._global:
            if self._bootstrapped:
                return
            for module in (ODS_MODULE, DWD_MODULE):
                for row in store.rows(module):
                    serial = str(row.get("流水编号", ""))
                    if not serial:
                        continue
                    watermark = self._initial_watermark(module, serial, row)
                    state = FlowState(
                        module=module,
                        serial=serial,
                        status=str(row.get("status", "草稿")),
                        action_seq=0,
                        watermark=watermark,
                        admission=ADMISSION_OPEN if row.get("status") == "运行中" else ADMISSION_HOLD,
                    )
                    self._states[(module, serial)] = state
                    self._journals[(module, serial)] = {}
                    self._locks[(module, serial)] = threading.RLock()
            # DWD 的初始准入以上游 ODS 为准，同事务口径初始化一次。
            for (module, serial), state in list(self._states.items()):
                if module != DWD_MODULE:
                    continue
                upstream = self._state_of(ODS_MODULE, pair_serial(DWD_MODULE, serial) or "")
                if upstream is not None:
                    state.admission = ADMISSION_OPEN if upstream.status == "运行中" else ADMISSION_HOLD
            # 预热：把动作序号、分区水位、清洗准入投影进清单行，首次列表请求即有值。
            for (module, serial), state in list(self._states.items()):
                self._project_to_store(state)
            self._bootstrapped = True

    def _initial_watermark(self, module: str, serial: str, row: dict[str, Any]) -> str:
        if module == ODS_MODULE:
            return str(row.get("最近接入时间") or "")
        upstream = store.find(ODS_MODULE, int(row["id"])) if str(row.get("id", "")).isdigit() else None
        if upstream is not None:
            return str(upstream.get("最近接入时间") or "")
        paired = pair_serial(module, serial)
        upstream_state = self._state_of(ODS_MODULE, paired or "")
        return upstream_state.watermark if upstream_state else ""

    def register(self, module: str, serial: str, watermark: str = "") -> FlowState:
        """新登记的流水进入状态机：草稿、动作序号 0、未建立水位。"""
        with self._global:
            key = (module, serial)
            if key in self._states:
                return self._states[key]
            state = FlowState(
                module=module,
                serial=serial,
                status="草稿",
                action_seq=0,
                watermark=watermark,
                admission=ADMISSION_HOLD,
            )
            self._states[key] = state
            self._journals[key] = {}
            self._locks[key] = threading.RLock()
            return state

    def _state_of(self, module: str, serial: str) -> FlowState | None:
        if not serial:
            return None
        return self._states.get((module, serial))

    def _lock_for(self, key: tuple[str, str]) -> threading.RLock:
        lock = self._locks.get(key)
        if lock is None:  # 极端并发下的兜底登记
            with self._global:
                lock = self._locks.setdefault(key, threading.RLock())
        return lock

    # ---------- 读取（把受控字段投影回清单行） ----------

    def project(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """把状态机字段合并进清单行；清单原有字段与分页契约保持不变。"""
        self.bootstrap()
        serial = str(row.get("流水编号", ""))
        state = self._state_of(module, serial)
        if state is None:
            return row
        projected = dict(row)
        projected.update(state.public_view())
        if module == DWD_MODULE:
            upstream = self._state_of(ODS_MODULE, pair_serial(module, serial) or "")
            if upstream is not None:
                projected["上游接入状态"] = upstream.status
                projected["清洗准入"] = state.admission
        return projected

    def journal_of(self, module: str, serial: str) -> dict[int, JournalEntry]:
        return self._journals.get((module, serial), {})

    # ---------- 动作提交 ----------

    def submit(
        self,
        module: str,
        serial: str,
        action: str,
        action_seq: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """提交一个动作。

        ``action_seq`` 为页面携带的候选动作序号（幂等键的一部分）；为空时由状态机
        按当前序号 +1 自动分配，兼容旧入口。返回 ``(投影后的清单行, 说明)``，
        校验不通过时清单行为 ``None``，说明即拒绝原因。
        """
        self.bootstrap()
        state = self._state_of(module, serial)
        if state is None:
            return None, f"流水 {serial} 不存在或已归档"
        if action not in TRANSITIONS or action not in TRANSITIONS_BY_MODULE[module]:
            reason = f"动作「{action}」不属于该流水可执行范围，迁移表未收录"
            self._record_rejection(state, reason)
            return None, reason

        candidate, seq_error = self._normalize_seq(state.action_seq, action_seq)
        if seq_error:
            self._record_rejection(state, seq_error)
            return None, seq_error

        # 幂等快查：锁外先挡一次，锁内还会复查（两个入口同时重试的场景）。
        journal = self._journals[(module, serial)]
        duplicated = journal.get(candidate)
        if duplicated is not None:
            return self._replay_or_conflict(module, serial, action, candidate, duplicated)

        pair_key = pair_key_of(module, serial)
        if pair_key is not None and pair_key not in self._states:
            pair_key = None
        # 按全局顺序对“本流水 + 配对流水”加锁，杜绝互锁。
        keys = sorted(key for key in ((module, serial), pair_key) if key is not None)
        locks = [self._lock_for(key) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            return self._submit_locked(module, serial, action, candidate)
        finally:
            for lock in reversed(locks):
                lock.release()

    def _submit_locked(
        self,
        module: str,
        serial: str,
        action: str,
        candidate: int,
    ) -> tuple[dict[str, Any] | None, str]:
        state = self._states[(module, serial)]
        rule = TRANSITIONS[action]
        journal = self._journals[(module, serial)]

        # 1) 锁内复查幂等：先到的请求可能已经提交了同一个序号。
        duplicated = journal.get(candidate)
        if duplicated is not None:
            return self._replay_or_conflict(module, serial, action, candidate, duplicated)

        # 2) 终态拒绝一切旧动作。
        if state.status in TERMINAL_STATUSES:
            reason = (
                f"流水状态为{state.status}（终态），拒绝动作「{action}」"
                f"（动作序号 {candidate}）"
            )
            self._record_rejection(state, reason)
            return None, reason

        # 3) 显式迁移表校验动作解释。
        if state.status not in rule["sources"]:
            reason = (
                f"动作「{action}」不允许从{state.status}发起，"
                f"迁移表仅允许源状态：{'、'.join(rule['sources'])}"
            )
            self._record_rejection(state, reason)
            return None, reason

        # 4) 动作序号必须连续：旧序号/跳号都视为过期动作。
        if candidate <= state.action_seq:
            reason = (
                f"动作序号 {candidate} 已过期，当前最新动作序号为 {state.action_seq}，"
                "请刷新页面后重试"
            )
            self._record_rejection(state, reason)
            return None, reason
        if candidate != state.action_seq + 1:
            reason = (
                f"动作序号 {candidate} 与当前序号 {state.action_seq} 不连续，"
                f"下一合法序号为 {state.action_seq + 1}"
            )
            self._record_rejection(state, reason)
            return None, reason

        # 5) DWD 启动清洗还要过 ODS 清洗准入（与 ODS 状态写入同事务的另一面）。
        pair = None
        pair_key = pair_key_of(module, serial)
        if pair_key is not None:
            pair = self._states.get(pair_key)
        if rule.get("require_upstream_admission"):
            if pair is None:
                reason = "未找到配对的贴源层接入流水，清洗准入无法确认，拒绝启动清洗"
                self._record_rejection(state, reason)
                return None, reason
            if pair.status != "运行中" or state.admission != ADMISSION_OPEN:
                reason = (
                    f"上游接入流水 {pair.serial} 当前为{pair.status}"
                    f"（本流水{state.admission}），清洗准入未放行，拒绝启动清洗"
                )
                self._record_rejection(state, reason)
                return None, reason

        # 6) 开事务：快照所有参与写入的状态。
        snapshots: list[tuple[FlowState, dict[str, Any]]] = [(state, state.snapshot())]
        if pair is not None:
            snapshots.append((pair, pair.snapshot()))

        target = rule["target"]
        resuming = action.startswith("启动") and state.status == "已暂停"
        try:
            # 暂停保留分区水位；恢复沿用该水位，不清空、不前进。
            if action.startswith("暂停"):
                if not state.watermark:
                    state.watermark = self._now_watermark()
            # 状态机字段落库。
            state.status = target
            state.action_seq = candidate
            state.last_reject_reason = ""

            # ODS 接入状态与 DWD 清洗准入在同一次事务里写入。
            if module == ODS_MODULE and pair is not None:
                pair.admission = ADMISSION_OPEN if target == "运行中" else ADMISSION_HOLD

            # 故障注入：模拟写入超时/异常，验证下面的回滚不留两份状态。
            if _consume_write_fault(module, serial):
                raise RuntimeError("模拟写入中断")

            # 同步投影到清单行（接入状态同步到清洗清单的双页面结果）。
            self._project_to_store(state)
            if pair is not None:
                self._project_to_store(pair)

        except Exception as exc:  # 任何写入异常：整单回滚。
            for flow_state, snap in snapshots:
                flow_state.restore(snap)
            reason = f"写入失败已回滚（{exc}），状态维持{state.status}，可原样重试"
            # 回滚后记录拒绝原因本身不改变受控状态。
            state.last_reject_reason = reason
            self._project_to_store(state)
            if pair is not None:
                self._project_to_store(pair)
            raise WriteFailed(reason) from exc

        # 7) 写幂等日志，提交完成。
        message = rule["resumed"] if resuming and "resumed" in rule else rule["succeeded"]
        if resuming and state.watermark:
            message = f"{message}（水位 {state.watermark}）"
        entry = JournalEntry(
            key=(serial, candidate),
            action=action,
            from_status=snapshots[0][1]["status"],
            to_status=target,
            message=message,
            # 深拷贝提交时刻的清单行：后续动作不会污染幂等重放结果。
            result=dict(self._row_of(module, serial)),
        )
        journal[candidate] = entry
        return entry.result, message

    # ---------- 内部工具 ----------

    @staticmethod
    def _normalize_seq(current: int, raw: Any) -> tuple[int, str | None]:
        if raw is None or raw == "":
            return current + 1, None
        try:
            candidate = int(raw)
        except (TypeError, ValueError):
            return -1, f"动作序号「{raw}」不是合法整数，请求被拒绝"
        if candidate < 0:
            return -1, f"动作序号 {candidate} 非法，序号必须从 0 开始递增"
        return candidate, None

    def _replay_or_conflict(
        self,
        module: str,
        serial: str,
        action: str,
        candidate: int,
        entry: JournalEntry,
    ) -> tuple[dict[str, Any] | None, str]:
        """幂等键已存在：同动作回放原结果；不同动作说明两个入口抢同一序号，拒绝。"""
        if entry.action == action:
            row = self._row_of(module, serial)
            return row, f"动作序号 {candidate} 已提交过，幂等返回原结果：{entry.message}"
        state = self._states[(module, serial)]
        reason = (
            f"动作序号 {candidate} 已被动作「{entry.action}」占用，"
            f"后到动作「{action}」拒绝提交（同一幂等键只接受先到且通过校验的请求）"
        )
        self._record_rejection(state, reason)
        return None, reason

    def _record_rejection(self, state: FlowState, reason: str) -> None:
        state.last_reject_reason = reason
        self._project_to_store(state)

    def _project_to_store(self, state: FlowState) -> None:
        row = self._find_row(state.module, state.serial)
        if row is None:
            return
        row["status"] = state.status
        row["pending"] = state.status not in TERMINAL_STATUSES
        row["abnormal"] = False
        row["动作序号"] = state.action_seq
        row["分区水位"] = state.watermark or "未建立水位"
        row["拒绝原因"] = state.last_reject_reason or "—"
        if state.module == DWD_MODULE:
            self._sync_admission_into_row(row, state)
        else:
            # 接入状态同步到清洗清单：只写准入口径，不覆盖 DWD 自身的动作序号/拒绝原因。
            paired = self._state_of(DWD_MODULE, pair_serial(state.module, state.serial) or "")
            if paired is not None:
                paired_row = self._find_row(DWD_MODULE, paired.serial)
                if paired_row is not None:
                    self._sync_admission_into_row(paired_row, paired, upstream_status=state.status)

    def _sync_admission_into_row(
        self,
        dwd_row: dict[str, Any],
        dwd_state: FlowState,
        upstream_status: str | None = None,
    ) -> None:
        """把清洗准入与上游接入状态写进 DWD 清单行（双页面同步的唯一落点）。"""
        dwd_row["清洗准入"] = dwd_state.admission
        if upstream_status is None:
            upstream = self._state_of(
                ODS_MODULE, pair_serial(DWD_MODULE, dwd_state.serial) or ""
            )
            upstream_status = upstream.status if upstream is not None else "—"
        dwd_row["上游接入状态"] = upstream_status

    def _find_row(self, module: str, serial: str) -> dict[str, Any] | None:
        for row in store.rows(module):
            if str(row.get("流水编号", "")) == serial:
                return row
        return None

    def _row_of(self, module: str, serial: str) -> dict[str, Any]:
        row = self._find_row(module, serial)
        if row is None:
            return {}
        return self.project(module, row)

    @staticmethod
    def _now_watermark() -> str:
        # 进程内没有时钟注入需求时用固定格式；真实项目替换成分区时间戳。
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M")


registry = FlowRegistry()
registry.bootstrap()
