"""变更审计追踪接口：维护变更记录，覆盖标记复核、归档记录等动作。

审计数据来自统一事件边界的投影：四个业务面的成功动作按记录编号在此可读，
历史记录保持原值、不补造。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit import AuditService
from app.routers._common import extract_context, to_action_result

router = APIRouter(prefix="/api/audit", tags=["变更审计追踪"])

service = AuditService()

LIST_FIELDS = ["记录编号", "变更对象", "变更类型", "变更前值", "变更后值", "操作人", "操作时间", "复核状态"]
STATUSES = ["已记录", "已复核", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
    record_no: str | None = Query(default=None, description="按审计编号或来源业务记录编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤变更审计追踪列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, record_no=record_no, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/by-record/{record_no}", response_model=dict)
def get_by_record_no(record_no: str) -> dict[str, Any]:
    """按审计记录编号读取详情，并附上同源事件，列表与详情来自同一事件。"""
    entry = service.get_by_record_no(record_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {record_no} 不存在或已归档")
    return {
        "entry": entry,
        "events": service.events_for_record(record_no),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条变更记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条变更记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="变更记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条变更记录执行标记复核、归档记录；动作同样经统一事件边界落库。"""
    action = str(payload.values.get("action") or "").strip()
    ctx = extract_context(payload)
    outcome = service.run_action(
        entry_id,
        action,
        operator=ctx["operator"],
        request_id=ctx["request_id"],
        parent_tx_id=ctx["parent_tx_id"],
        values=payload.values,
    )
    return to_action_result(outcome)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出变更审计追踪清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "audit", "total": total, "items": items}
