"""明细层清洗接口：维护清洗流水，覆盖启动清洗、暂停清洗、归档流水等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dwd import DwdService

router = APIRouter(prefix="/api/dwd", tags=["明细层清洗"])

service = DwdService()

LIST_FIELDS = ["流水编号", "输入表名", "输出表名", "空值填充", "格式转换", "脏数据比例", "执行批次", "清洗状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按流水编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按流水编号与状态过滤明细层清洗列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出明细层清洗清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dwd", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清洗流水明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清洗流水 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条清洗流水，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="清洗流水已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条清洗流水执行启动清洗、暂停清洗、完成清洗、归档流水。

    启动清洗必须通过上游 ODS 清洗准入；``action_seq`` 与流水编号组成幂等键，
    校验不通过或写入回滚时 ``ok=false`` 并带回拒绝原因，页面状态保持不变。
    """
    action = str(payload.values.get("action") or "").strip()
    action_seq = payload.values.get("action_seq")
    entry, message = service.run_action(entry_id, action, action_seq)
    if entry is None:
        return ActionResult(ok=False, message=message, reject_reason=message)
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        action_seq=entry.get("动作序号"),
    )
