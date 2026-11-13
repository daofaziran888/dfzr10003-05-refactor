"""贴源层接入业务规则。

状态流转不再由本服务自行解释，而是委托给 ``services.flow`` 里的显式迁移表与
幂等事务引擎：动作序号作为幂等键的一部分，接入状态与 DWD 清洗准入同事务落库。
本服务只保留清单筛选、字段校验与入参适配。
"""
from __future__ import annotations

from typing import Any

from app.services.flow import (
    ODS_MODULE,
    WriteFailed,
    registry,
)
from app.store import store

MODULE = ODS_MODULE
REQUIRED_FIELDS = ["流水编号", "来源系统", "源表名称"]
# 页面契约里的四个状态；已归档由归档动作进入，已完成由完成动作进入。
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成", "已归档"]


class OdsService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [registry.project(MODULE, row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("流水编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return registry.project(MODULE, row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        # 新流水登记进状态机：草稿、动作序号 0、未建立水位。
        registry.register(MODULE, str(entry["流水编号"]))
        return registry.project(MODULE, entry), []

    def run_action(
        self, entry_id: int, action: str, action_seq: Any = None
    ) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"接入流水 {entry_id} 不存在或已归档"
        serial = str(row["流水编号"])
        try:
            return registry.submit(MODULE, serial, action.strip(), action_seq)
        except WriteFailed as exc:
            # 写入超时/异常已回滚：返回原状态行与可读原因，调用方可原样重试。
            return None, str(exc)
        except FlowConflict as exc:  # 迁移表/序号冲突在引擎内已转为拒绝返回，这里兜底
            return None, str(exc)
