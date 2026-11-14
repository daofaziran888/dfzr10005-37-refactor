"""表生命周期管理业务规则。

状态变更不再在本服务内直接改字段，而是统一提交到事件边界（app.events），
由边界在同一事件内完成业务落库与审计投影；本服务只保留读取与登记口径。
"""
from __future__ import annotations

from typing import Any

from app.event_rules import RULES
from app.events import EventOutcome, event_boundary
from app.store import store

MODULE = "lifecycle"
# 规则收口到 event_rules，这里只做只读引用，禁止再出现第二份动作映射。
_RULE = RULES[MODULE]
REQUIRED_FIELDS = ["策略编号", "表名称", "创建日期"]
STATUS_ORDER = list(_RULE.status_order)
ACTION_RULES = dict(_RULE.action_targets)
NEGATIVE_ACTIONS = list(_RULE.negative_actions)


class LifecycleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str | None = None,
        request_id: str | None = None,
        parent_tx_id: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> EventOutcome:
        """旧入口签名（entry_id, action）保持可用，新增的溯源参数走同一事件边界。"""
        return event_boundary.submit(
            MODULE,
            entry_id,
            action,
            values=values,
            operator=operator,
            request_id=request_id,
            parent_tx_id=parent_tx_id,
        )
