"""明细层清洗业务规则。

清洗动作同样走 ``services.flow`` 引擎：启动清洗必须通过上游 ODS 的清洗准入，
准入状态随 ODS 接入状态在同一次事务里同步到本清单。
"""
from __future__ import annotations

from typing import Any

from app.services.flow import (
    DWD_MODULE,
    WriteFailed,
    registry,
)
from app.store import store

MODULE = DWD_MODULE
REQUIRED_FIELDS = ["流水编号", "输入表名", "输出表名"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成", "已归档"]


class DwdService:
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
        registry.register(MODULE, str(entry["流水编号"]))
        return registry.project(MODULE, entry), []

    def run_action(
        self, entry_id: int, action: str, action_seq: Any = None
    ) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"清洗流水 {entry_id} 不存在或已归档"
        serial = str(row["流水编号"])
        try:
            return registry.submit(MODULE, serial, action.strip(), action_seq)
        except WriteFailed as exc:
            return None, str(exc)
