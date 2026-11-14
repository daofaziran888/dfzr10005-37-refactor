"""路由层共用的事件入参映射与结果转换，统一写法，避免各模块各写一遍。"""
from __future__ import annotations

from app.events import EventOutcome
from app.schemas import ActionResult


def extract_context(payload) -> dict[str, str | None]:
    """兼容旧入参：溯源信息既可放在顶层，也可放在 values 里。"""
    values = payload.values or {}
    return {
        "operator": payload.operator or values.get("operator"),
        "request_id": payload.request_id or values.get("request_id"),
        "parent_tx_id": payload.parent_tx_id or values.get("parent_tx_id"),
    }


def to_action_result(outcome: EventOutcome) -> ActionResult:
    """事件结果转旧动作响应：ok/message/entry 语义不变，额外带出事件溯源字段。"""
    return ActionResult(
        ok=outcome.ok,
        message=outcome.message,
        entry=outcome.entry,
        event_id=outcome.event_id,
        record_no=outcome.record_no,
        source_module=outcome.module,
        request_id=outcome.request_id,
        parent_tx_id=outcome.parent_tx_id,
        error_summary=outcome.error_summary,
    )
