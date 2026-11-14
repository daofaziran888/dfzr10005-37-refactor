"""数据资产盘点接口：维护资产记录，覆盖提交复核、归档资产、废弃资产等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.catalog import CatalogService
from app.routers._common import extract_context, to_action_result

router = APIRouter(prefix="/api/catalog", tags=["数据资产盘点"])

service = CatalogService()

LIST_FIELDS = ["资产编号", "表名称", "资产分类", "业务域", "负责人", "数据等级", "更新频率", "盘点时间"]
STATUSES = ["草稿", "已盘点", "已复核", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按资产编号检索"),
    status: str | None = Query(default=None, description="草稿、已盘点、已复核、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按资产编号与状态过滤数据资产盘点列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条资产记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"资产记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条资产记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="资产记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条资产记录执行提交复核、归档资产、废弃资产；统一走事件边界落库。"""
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


@router.post("/{entry_id}/owner", response_model=ActionResult)
def change_owner(entry_id: int, payload: EntryPayload) -> ActionResult:
    """资产负责人变更：只产生一条含旧负责人、新负责人、来源模块与落库时间的事件。"""
    new_owner = str(payload.values.get("负责人") or payload.values.get("owner") or "").strip()
    if not new_owner:
        return ActionResult(ok=False, message="缺少新负责人（字段名：负责人）")
    ctx = extract_context(payload)
    outcome = service.change_owner(
        entry_id,
        new_owner,
        operator=ctx["operator"],
        request_id=ctx["request_id"],
        parent_tx_id=ctx["parent_tx_id"],
    )
    return to_action_result(outcome)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据资产盘点清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "catalog", "total": total, "items": items}
