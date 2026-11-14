"""统一事件边界接口：事件流查询、单事件读取与批量动作入口。

所有业务动作的状态变更与审计事件都在后端 EventBoundary 内收口；本路由只负责
把同一事件源以只读方式暴露出来，并提供共享父事务标识的批量调用入口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.events import event_boundary
from app.schemas import BatchActionPayload, BatchActionResult, PageResult

router = APIRouter(prefix="/api/events", tags=["统一事件边界"])


@router.get("", response_model=PageResult[dict])
def list_events(
    module: str | None = Query(default=None, description="来源业务模块"),
    record_no: str | None = Query(default=None, description="业务记录编号，如 LIFE-0001"),
    action: str | None = Query(default=None, description="动作名称"),
    status: str | None = Query(default=None, description="success / failed"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模块、记录编号、动作、结果状态过滤事件流。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = event_boundary.list_events(
        module=module,
        record_no=record_no,
        action=action,
        status=status,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/by-record/{record_no}")
def events_by_record(record_no: str) -> dict[str, Any]:
    """按业务记录编号读取完整事件链，审计列表与详情共用此口径。"""
    return {"record_no": record_no, "items": event_boundary.events_for_record(record_no)}


@router.get("/{event_id}")
def get_event(event_id: str) -> dict[str, Any]:
    """按事件编号读取单条事件（含失败事件的原始错误摘要）。"""
    event = event_boundary.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_id} 不存在")
    return event


@router.post("/batch", response_model=BatchActionResult)
def run_batch(payload: BatchActionPayload) -> BatchActionResult:
    """批量动作：整批共享一个父事务标识，任一失败则全部不落库。"""
    if not payload.calls:
        raise HTTPException(status_code=400, detail="批量动作至少包含一次调用")
    result = event_boundary.submit_batch(
        [call.model_dump() for call in payload.calls],
        parent_tx_id=payload.parent_tx_id,
    )
    return BatchActionResult(**result)
