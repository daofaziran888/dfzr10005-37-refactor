"""统一事件边界：所有业务动作的状态变更与审计事件只能经此落库。

设计约束（对应结构性改造要求）：

* 成功动作只产生一条完整事件：业务记录状态变更与审计投影在同一把锁、同一调用内
  完成，中途任何一步失败都不会写出“半边事件”。
* 失败动作不改变业务表与审计表任何一侧；失败原因以失败事件留痕，仅出现在事件流中。
* 同一 request_id 的重复提交返回首次结果；对同一记录的并发/重复动作由源状态守卫
  与在途集合共同保证只有一个有效推进。
* 历史审计记录保持原值可读，不补造、不回填；新事件统一写入前值、后值、操作时间、
  来源模块，并由审计列表与详情按记录编号共同读取。
* 保留旧接口入参映射、失败原始错误摘要、批量调用的父事务标识。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.event_rules import ModuleRule, RULES, check_record_no, rule_of
from app.store import store

AUDIT_MODULE = "audit"
DEFAULT_OPERATOR = "系统"


class EventError(Exception):
    """事件边界拒绝受理时抛出；message 即为返回给旧调用方的原始错误摘要。"""

    def __init__(self, message: str, record_no: str | None = None) -> None:
        super().__init__(message)
        self.record_no = record_no


@dataclass
class EventOutcome:
    """一次事件提交的标准结果，服务层与路由层都按这个结构消费。"""

    ok: bool
    message: str
    event_id: str
    parent_tx_id: str | None
    request_id: str | None
    record_no: str | None
    module: str | None
    action: str | None
    entry: dict[str, Any] | None = None
    error_summary: str | None = None
    audit_row: dict[str, Any] | None = None
    operator: str | None = None


@dataclass
class _Plan:
    """批量提交的预校验结果：先全部校验，再统一执行，保证整批同进同退。"""

    module: str
    entry_id: int
    action: str
    values: dict[str, Any]
    operator: str
    request_id: str | None
    parent_tx_id: str | None


class EventBoundary:
    """进程内统一落库边界。存储仍复用内存 Store，事件流与审计投影在此收口。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._seq: int = 0
        self._audit_seq: int = 0
        #: request_id -> 已完成的提交结果，保证重复提交只认第一次。
        self._idempotent: dict[str, EventOutcome] = {}
        #: 正在处理中的 (模块, 记录id, 动作)，并发动作只有一个能进入临界区。
        self._inflight: set[tuple[str, int, str]] = set()

    # ------------------------------------------------------------------ 查询

    def list_events(
        self,
        *,
        module: str | None = None,
        record_no: str | None = None,
        action: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(self._events)
        if module:
            rows = [row for row in rows if row["module"] == module]
        if record_no:
            rows = [row for row in rows if row.get("record_no") == record_no]
        if action:
            rows = [row for row in rows if row.get("action") == action]
        if status:
            rows = [row for row in rows if row["status"] == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._events:
                if row["event_id"] == event_id:
                    return dict(row)
        return None

    def events_for_record(self, record_no: str) -> list[dict[str, Any]]:
        """审计详情按业务记录编号读取同一事件流，列表与详情同源。"""
        with self._lock:
            return [dict(row) for row in self._events if row.get("record_no") == record_no]

    # ------------------------------------------------------------ 动作主入口

    def submit(
        self,
        module: str,
        entry_id: int,
        action: str,
        *,
        values: dict[str, Any] | None = None,
        operator: str | None = None,
        request_id: str | None = None,
        parent_tx_id: str | None = None,
    ) -> EventOutcome:
        """旧业务入口的统一替代：校验、变更、审计在同一事件边界内完成。"""
        values = dict(values or {})
        operator = operator or DEFAULT_OPERATOR
        try:
            rule = rule_of(module)
        except KeyError as exc:
            # 未接入的模块属于编程错误，直接抛出，不制造事件。
            raise EventError(str(exc)) from exc

        # 幂等：同一 request_id 的重复提交原样返回首次结论。
        if request_id and request_id in self._idempotent:
            return self._idempotent[request_id]

        with self._lock:
            inflight_key = (module, entry_id, action)
            if inflight_key in self._inflight:
                return self._fail(
                    rule, entry_id, None, action, operator, request_id, parent_tx_id,
                    f"动作「{action}」正在处理中，重复请求已被拦截",
                )
            self._inflight.add(inflight_key)
            try:
                # 物理快照先于任何写入：校验失败本就无写入，若落库阶段出现意外
                # 异常，也按快照恢复，保证不会留下“半边事件”。
                snapshot = self._snapshot_tables()
                event_cursor = len(self._events)
                try:
                    outcome = self._do_submit(
                        rule, entry_id, action, values, operator, request_id, parent_tx_id
                    )
                except EventError as exc:
                    outcome = self._fail(
                        rule, entry_id, exc.record_no, action, operator, request_id,
                        parent_tx_id, str(exc),
                    )
                except Exception as exc:  # 落库阶段意外异常：两侧恢复后只留失败事件
                    self._restore_tables(snapshot)
                    del self._events[event_cursor:]
                    outcome = self._fail(
                        rule, entry_id, None, action, operator, request_id, parent_tx_id,
                        f"动作执行异常，变更已回滚：{exc}",
                    )
                if request_id is not None:
                    self._idempotent[request_id] = outcome
                return outcome
            finally:
                self._inflight.discard(inflight_key)

    def _do_submit(
        self,
        rule: ModuleRule,
        entry_id: int,
        action: str,
        values: dict[str, Any],
        operator: str,
        request_id: str | None,
        parent_tx_id: str | None,
    ) -> EventOutcome:
        entry = store.find(rule.module, entry_id)
        if entry is None:
            raise EventError(f"{rule.label}记录 {entry_id} 不存在或已归档")
        record_no = str(entry.get(rule.code_field) or "")
        if not check_record_no(rule, record_no):
            raise EventError(
                f"记录编号「{record_no}」不符合{rule.label}编号口径"
                f"（{'/'.join(rule.code_prefixes)} 开头）",
                record_no=record_no,
            )
        if action not in rule.action_targets:
            raise EventError(
                f"动作「{action}」不属于{rule.label}可执行范围", record_no=record_no
            )

        before_status = str(entry.get("status") or "")
        expected_source = rule.action_sources[action]
        if before_status != expected_source:
            # 重复推进：状态已被前一次有效动作改变，本次不允许再产生推进。
            raise EventError(
                f"记录当前状态为「{before_status}」，动作「{action}」仅允许在"
                f"「{expected_source}」状态执行，重复推进已被拦截",
                record_no=record_no,
            )

        target_status = rule.action_targets[action]
        if target_status not in rule.status_order:
            raise EventError(
                f"目标状态「{target_status}」不在允许的状态序列里", record_no=record_no
            )

        # 快照先于一切写入，保证失败时两侧都没有被触碰。
        before_snapshot = self._snapshot(entry)
        event_id = self._next_event_id()
        occurred_at = self._now()

        # 唯一一次业务状态落库。
        entry["status"] = target_status
        entry["pending"] = target_status != rule.status_order[-1]
        entry["abnormal"] = action in rule.negative_actions
        entry["event_id"] = event_id
        entry["event_source"] = rule.label
        entry["event_time"] = occurred_at
        if rule.module == "lifecycle":
            # 生命周期台账的“最后操作”列直接呈现本次动作，保持历史列语义。
            entry["最后操作"] = action
        after_snapshot = self._snapshot(entry)

        audit_row: dict[str, Any] | None = None
        if rule.projection == "audit_row":
            audit_row = self._append_audit_row(
                rule=rule,
                record_no=record_no,
                action=action,
                before_status=before_status,
                target_status=target_status,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                operator=operator,
                occurred_at=occurred_at,
                event_id=event_id,
            )
        else:
            # 审计模块自身动作：只回写同一条审计记录，绝不新增审计行。
            entry["复核状态"] = target_status
            entry["event_id"] = event_id
            entry["event_source"] = rule.label
            entry["event_time"] = occurred_at

        event = {
            "event_id": event_id,
            "parent_tx_id": parent_tx_id,
            "request_id": request_id,
            "module": rule.module,
            "source_module": rule.label,
            "entry_id": entry_id,
            "record_no": record_no,
            "action": action,
            "status": "success",
            "before_value": before_status,
            "after_value": target_status,
            "changed_fields": self._diff_fields(before_snapshot, after_snapshot),
            "operator": operator,
            "occurred_at": occurred_at,
            "error_summary": None,
            "audit_record_no": audit_row["记录编号"] if audit_row else record_no,
        }
        self._events.append(event)
        return EventOutcome(
            ok=True,
            message=f"{rule.label}记录已{action}",
            event_id=event_id,
            parent_tx_id=parent_tx_id,
            request_id=request_id,
            record_no=record_no,
            module=rule.module,
            action=action,
            entry=dict(entry),
            audit_row=audit_row,
            operator=operator,
        )

    # ------------------------------------------------------ 字段变更（如负责人）

    def change_field(
        self,
        module: str,
        entry_id: int,
        field_name: str,
        new_value: Any,
        *,
        operator: str | None = None,
        request_id: str | None = None,
        parent_tx_id: str | None = None,
        action: str | None = None,
    ) -> EventOutcome:
        """业务台账字段变更的统一入口（例如资产负责人变更）。

        与状态动作共用同一条落库边界：一次字段变更只产生一条包含旧值、新值、
        来源模块与落库时间的完整事件。
        """
        operator = operator or DEFAULT_OPERATOR
        rule = rule_of(module)
        action = action or f"变更{field_name}"
        if request_id and request_id in self._idempotent:
            return self._idempotent[request_id]

        with self._lock:
            inflight_key = (module, entry_id, action)
            if inflight_key in self._inflight:
                return self._fail(
                    rule, entry_id, None, action, operator, request_id, parent_tx_id,
                    f"动作「{action}」正在处理中，重复请求已被拦截",
                )
            self._inflight.add(inflight_key)
            try:
                snapshot = self._snapshot_tables()
                event_cursor = len(self._events)
                try:
                    entry = store.find(module, entry_id)
                    if entry is None:
                        raise EventError(f"{rule.label}记录 {entry_id} 不存在或已归档")
                    record_no = str(entry.get(rule.code_field) or "")
                    if not check_record_no(rule, record_no):
                        raise EventError(
                            f"记录编号「{record_no}」不符合{rule.label}编号口径",
                            record_no=record_no,
                        )
                    if not field_name:
                        raise EventError("字段变更缺少字段名", record_no=record_no)
                    old_value = entry.get(field_name)
                    if str(old_value or "") == str(new_value or ""):
                        raise EventError(
                            f"字段「{field_name}」新旧值相同，未产生有效变更",
                            record_no=record_no,
                        )

                    before_snapshot = self._snapshot(entry)
                    event_id = self._next_event_id()
                    occurred_at = self._now()
                    entry[field_name] = new_value
                    entry["event_id"] = event_id
                    entry["event_source"] = rule.label
                    entry["event_time"] = occurred_at
                    after_snapshot = self._snapshot(entry)

                    audit_row: dict[str, Any] | None = None
                    if rule.projection == "audit_row":
                        audit_row = self._append_audit_row(
                            rule=rule,
                            record_no=record_no,
                            action=action,
                            before_status=str(entry.get("status") or ""),
                            target_status=str(entry.get("status") or ""),
                            before_snapshot=before_snapshot,
                            after_snapshot=after_snapshot,
                            operator=operator,
                            occurred_at=occurred_at,
                            event_id=event_id,
                            field_name=field_name,
                            old_value=old_value,
                            new_value=new_value,
                        )
                    event = {
                        "event_id": event_id,
                        "parent_tx_id": parent_tx_id,
                        "request_id": request_id,
                        "module": module,
                        "source_module": rule.label,
                        "entry_id": entry_id,
                        "record_no": record_no,
                        "action": action,
                        "status": "success",
                        "before_value": "" if old_value is None else str(old_value),
                        "after_value": "" if new_value is None else str(new_value),
                        "changed_fields": self._diff_fields(before_snapshot, after_snapshot),
                        "operator": operator,
                        "occurred_at": occurred_at,
                        "error_summary": None,
                        "audit_record_no": audit_row["记录编号"] if audit_row else record_no,
                    }
                    self._events.append(event)
                    outcome = EventOutcome(
                        ok=True,
                        message=f"{rule.label}记录{field_name}已变更",
                        event_id=event_id,
                        parent_tx_id=parent_tx_id,
                        request_id=request_id,
                        record_no=record_no,
                        module=module,
                        action=action,
                        entry=dict(entry),
                        audit_row=audit_row,
                        operator=operator,
                    )
                except EventError as exc:
                    outcome = self._fail(
                        rule, entry_id, exc.record_no, action, operator, request_id,
                        parent_tx_id, str(exc),
                    )
                except Exception as exc:  # 落库阶段意外异常：两侧恢复后只留失败事件
                    self._restore_tables(snapshot)
                    del self._events[event_cursor:]
                    outcome = self._fail(
                        rule, entry_id, None, action, operator, request_id, parent_tx_id,
                        f"字段变更执行异常，变更已回滚：{exc}",
                    )
                if request_id is not None:
                    self._idempotent[request_id] = outcome
                return outcome
            finally:
                self._inflight.discard(inflight_key)

    # ------------------------------------------------------------- 批量入口

    def submit_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        parent_tx_id: str | None = None,
    ) -> dict[str, Any]:
        """批量动作：共享同一个父事务标识，任一调用失败则整批不落库。

        两阶段：先在无副作用阶段完成全部校验，再统一执行；执行阶段理论上不会再失败，
        若出现异常则按失败处理并保证此前本批没有写入任何业务/审计数据。
        """
        parent_tx_id = parent_tx_id or f"PTX-{self._next_seq()}-{self._now_compact()}"
        plans: list[_Plan] = []
        with self._lock:
            # 先把模块与动作口径全部校验一遍，任何一条不合法都直接整批拒绝。
            for raw in calls:
                module = str(raw.get("module") or "").strip()
                action = str(raw.get("action") or "").strip()
                entry_id = raw.get("entry_id")
                try:
                    rule = rule_of(module)
                    entry_id = int(entry_id)
                except (KeyError, TypeError, ValueError) as exc:
                    return self._batch_rejected(
                        parent_tx_id, f"批量参数不合法：{exc}", plans
                    )
                plans.append(
                    _Plan(
                        module=module,
                        entry_id=entry_id,
                        action=action,
                        values=dict(raw.get("values") or {}),
                        operator=str(raw.get("operator") or DEFAULT_OPERATOR),
                        request_id=raw.get("request_id"),
                        parent_tx_id=parent_tx_id,
                    )
                )
                entry = store.find(module, entry_id)
                record_no = str(entry.get(rule.code_field) or "") if entry else None
                if entry is None:
                    return self._batch_rejected(
                        parent_tx_id,
                        f"{rule.label}记录 {entry_id} 不存在或已归档", plans,
                    )
                if action not in rule.action_targets:
                    return self._batch_rejected(
                        parent_tx_id,
                        f"动作「{action}」不属于{rule.label}可执行范围", plans,
                    )
                if str(entry.get("status") or "") != rule.action_sources[action]:
                    return self._batch_rejected(
                        parent_tx_id,
                        f"记录 {record_no} 当前状态不允许执行动作「{action}」，"
                        "重复推进已被拦截",
                        plans,
                    )

            # 全部预校验通过，统一执行；每个成功动作各产生一条完整事件。
            # 执行前做物理快照：若执行阶段出现意外失败，按快照恢复业务表与审计表，
            # 保证整批要么全部生效，要么两侧都不留痕。
            snapshot = self._snapshot_tables()
            event_cursor = len(self._events)
            results: list[EventOutcome] = []
            failed_outcome: EventOutcome | None = None
            unexpected: Exception | None = None
            try:
                for plan in plans:
                    outcome = self.submit(
                        plan.module,
                        plan.entry_id,
                        plan.action,
                        values=plan.values,
                        operator=plan.operator,
                        request_id=plan.request_id,
                        parent_tx_id=parent_tx_id,
                    )
                    if not outcome.ok:
                        failed_outcome = outcome
                        break
                    results.append(outcome)
            except Exception as exc:  # 执行阶段意外异常：同样整批回滚，不留半边
                unexpected = exc

            if failed_outcome is not None or unexpected is not None:
                self._restore_tables(snapshot)
                # 清掉本批已缓存的幂等结果，避免回滚后同 request_id 重放陈旧成功结论。
                stale_keys = [
                    key
                    for key, cached in self._idempotent.items()
                    if cached.parent_tx_id == parent_tx_id
                ]
                for key in stale_keys:
                    del self._idempotent[key]
                # 回滚后重新登记失败事件（业务表已恢复，失败事件只进事件流）。
                del self._events[event_cursor:]
                if unexpected is not None:
                    failed_module = plans[len(results)].module
                    failure = self._fail(
                        rule_of(failed_module),
                        plans[len(results)].entry_id,
                        None,
                        plans[len(results)].action,
                        plans[len(results)].operator,
                        plans[len(results)].request_id,
                        parent_tx_id,
                        f"批量动作执行异常，整批已回滚：{unexpected}",
                    )
                else:
                    failure = self._fail(
                        rule_of(failed_outcome.module),
                        failed_outcome.entry_id,
                        failed_outcome.record_no,
                        failed_outcome.action or "",
                        failed_outcome.operator if failed_outcome.operator else DEFAULT_OPERATOR,
                        failed_outcome.request_id,
                        parent_tx_id,
                        failed_outcome.error_summary or "批量动作执行失败，整批未生效",
                    )
                return {
                    "ok": False,
                    "parent_tx_id": parent_tx_id,
                    "message": failure.error_summary or "批量动作执行失败，整批未生效",
                    "results": [self._outcome_json(item) for item in results]
                    + [self._outcome_json(failure)],
                }
            return {
                "ok": True,
                "parent_tx_id": parent_tx_id,
                "message": f"批量动作全部成功，共 {len(results)} 条事件",
                "results": [self._outcome_json(item) for item in results],
            }

    def _batch_rejected(
        self, parent_tx_id: str, reason: str, plans: list[_Plan]
    ) -> dict[str, Any]:
        """整批预校验拒绝：不写业务、不写审计，仅留一条失败事件承载错误摘要。"""
        event = {
            "event_id": self._next_event_id(),
            "parent_tx_id": parent_tx_id,
            "request_id": None,
            "module": plans[-1].module if plans else None,
            "source_module": RULES[plans[-1].module].label if plans else None,
            "entry_id": plans[-1].entry_id if plans else None,
            "record_no": None,
            "action": plans[-1].action if plans else None,
            "status": "failed",
            "before_value": None,
            "after_value": None,
            "changed_fields": [],
            "operator": DEFAULT_OPERATOR,
            "occurred_at": self._now(),
            "error_summary": reason,
            "audit_record_no": None,
        }
        self._events.append(event)
        return {
            "ok": False,
            "parent_tx_id": parent_tx_id,
            "message": reason,
            "results": [],
        }

    @staticmethod
    def _snapshot_tables() -> dict[str, list[dict[str, Any]]]:
        """对全部业务表做深拷贝快照，供批量动作整批回滚。"""
        return {
            name: [dict(row) for row in rows]
            for name, rows in store._tables.items()  # noqa: SLF001 - 边界层统一接管存储
        }

    @staticmethod
    def _restore_tables(snapshot: dict[str, list[dict[str, Any]]]) -> None:
        for name, rows in snapshot.items():
            store._tables[name] = [dict(row) for row in rows]  # noqa: SLF001

    # --------------------------------------------------------------- 审计投影

    def _append_audit_row(
        self,
        *,
        rule: ModuleRule,
        record_no: str,
        action: str,
        before_status: str,
        target_status: str,
        before_snapshot: dict[str, Any],
        after_snapshot: dict[str, Any],
        operator: str,
        occurred_at: str,
        event_id: str,
        field_name: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
    ) -> dict[str, Any]:
        """成功事件投影成一条审计记录；变更对象/类型/前后值/复核状态全部落值。"""
        audit_no = self._next_audit_no()
        if field_name is not None:
            change_type = f"{action}"
            before_text = f"{field_name}: {'' if old_value is None else old_value}"
            after_text = f"{field_name}: {'' if new_value is None else new_value}"
        else:
            change_type = action
            before_text = before_status
            after_text = target_status
        row = {
            "id": max(
                (int(item.get("id", 0)) for item in store.rows(AUDIT_MODULE)), default=0
            ) + 1,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": audit_no,
            "变更对象": f"{rule.label}/{record_no}",
            "变更类型": change_type,
            "变更前值": before_text,
            "变更后值": after_text,
            "操作人": operator,
            "操作时间": occurred_at,
            "复核状态": "已记录",
            "event_id": event_id,
            "source_module": rule.label,
            "source_record_no": record_no,
            "event_time": occurred_at,
            "legacy": False,
            "changed_fields": self._diff_fields(before_snapshot, after_snapshot),
        }
        store.rows(AUDIT_MODULE).append(row)
        return dict(row)

    # --------------------------------------------------------------- 失败留痕

    def _fail(
        self,
        rule: ModuleRule | None,
        entry_id: int | None,
        record_no: str | None,
        action: str,
        operator: str,
        request_id: str | None,
        parent_tx_id: str | None,
        reason: str,
    ) -> EventOutcome:
        """失败事件：只进事件流，绝不动业务表与审计表。"""
        event = {
            "event_id": self._next_event_id(),
            "parent_tx_id": parent_tx_id,
            "request_id": request_id,
            "module": rule.module if rule else None,
            "source_module": rule.label if rule else None,
            "entry_id": entry_id,
            "record_no": record_no,
            "action": action,
            "status": "failed",
            "before_value": None,
            "after_value": None,
            "changed_fields": [],
            "operator": operator,
            "occurred_at": self._now(),
            "error_summary": reason,
            "audit_record_no": None,
        }
        self._events.append(event)
        return EventOutcome(
            ok=False,
            message=reason,
            event_id=event["event_id"],
            parent_tx_id=parent_tx_id,
            request_id=request_id,
            record_no=record_no,
            module=rule.module if rule else None,
            action=action,
            entry=None,
            error_summary=reason,
        )

    # ------------------------------------------------------------------ 工具

    def _next_event_id(self) -> str:
        self._seq += 1
        return f"EVT-{self._seq:06d}"

    def _next_audit_no(self) -> str:
        """审计编号在历史最大编号之后顺延，绝不与已存在的 AUDI- 记录撞号。"""
        max_seq = self._audit_seq
        for item in store.rows(AUDIT_MODULE):
            no = str(item.get("记录编号") or "")
            if no.startswith("AUDI-"):
                try:
                    max_seq = max(max_seq, int(no[len("AUDI-"):]))
                except ValueError:
                    continue
        self._audit_seq = max_seq + 1
        return f"AUDI-{self._audit_seq:04d}"

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _now_compact() -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S%f")

    @staticmethod
    def _snapshot(entry: dict[str, Any]) -> dict[str, str]:
        """剔除事件自身的回写字段，保留业务字段原值用于前后值比对。"""
        skip = {"event_id", "event_source", "event_time"}
        return {
            key: ("" if value is None else str(value))
            for key, value in entry.items()
            if key not in skip
        }

    @staticmethod
    def _diff_fields(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        keys = sorted(set(before) | set(after))
        return [
            {"field": key, "before": before.get(key, ""), "after": after.get(key, "")}
            for key in keys
            if before.get(key, "") != after.get(key, "")
        ]

    @staticmethod
    def _outcome_json(outcome: EventOutcome) -> dict[str, Any]:
        return {
            "ok": outcome.ok,
            "message": outcome.message,
            "event_id": outcome.event_id,
            "parent_tx_id": outcome.parent_tx_id,
            "request_id": outcome.request_id,
            "record_no": outcome.record_no,
            "module": outcome.module,
            "action": outcome.action,
            "error_summary": outcome.error_summary,
        }


# 全局唯一边界：所有写入方共用，禁止各写一遍。
event_boundary = EventBoundary()
