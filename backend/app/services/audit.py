"""变更审计追踪业务规则。

审计页不再由人工另行补写：四个业务面的成功动作经统一事件边界投影到这里，
列表与详情都按业务记录编号读取同一事件。审计自身的复核/归档动作同样走边界，
只回写同一条审计记录，不新增审计行。改造前的历史审计记录保持原值可读、不补造。
"""
from __future__ import annotations

from typing import Any

from app.event_rules import RULES
from app.events import EventOutcome, event_boundary
from app.store import store

MODULE = "audit"
_RULE = RULES[MODULE]
REQUIRED_FIELDS = ["记录编号", "变更对象", "变更类型"]
STATUS_ORDER = list(_RULE.status_order)
ACTION_RULES = dict(_RULE.action_targets)
NEGATIVE_ACTIONS = list(_RULE.negative_actions)


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        record_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if record_no:
            # 既支持按审计编号查，也支持按来源业务记录编号（LIFE-/CATA-/…）查。
            rows = [
                row
                for row in rows
                if row.get("记录编号") == record_no
                or row.get("source_record_no") == record_no
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_record_no(self, record_no: str) -> dict[str, Any] | None:
        """按审计记录编号读取单条，供审计详情与事件流共同定位。"""
        for row in store.rows(MODULE):
            if row.get("记录编号") == record_no:
                return row
        return None

    def events_for_record(self, record_no: str) -> list[dict[str, Any]]:
        """审计详情按编号读取事件流；历史记录无事件时返回空列表，不补造。

        既支持来源业务记录编号（LIFE-/CATA-/…），也支持审计编号（AUDI-…）。
        """
        events = event_boundary.events_for_record(record_no)
        if events:
            return events
        row = self.get_by_record_no(record_no)
        source_no = row.get("source_record_no") if row else None
        if source_no and source_no != record_no:
            return event_boundary.events_for_record(source_no)
        return []

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
        # 手工登记的历史口径记录：标记为 legacy，事件边界不会回填或改写它们。
        entry["legacy"] = True
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
        """审计动作同样走统一事件边界；self 投影只回写，不产生新审计行。"""
        return event_boundary.submit(
            MODULE,
            entry_id,
            action,
            values=values,
            operator=operator,
            request_id=request_id,
            parent_tx_id=parent_tx_id,
        )
