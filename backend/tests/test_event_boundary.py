"""统一事件边界结构性改造的回归测试。

覆盖：编号语义兼容、成功事件单一性、失败两侧不变、重复/并发只推进一次、
审计与业务同源、历史审计不补造、负责人变更单事件、批量父事务与整批回滚。

直接用标准库 unittest 运行：.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime

from app import seed
from app.event_rules import RULES, check_record_no
from app.events import EventBoundary, event_boundary
from app.services.audit import AuditService
from app.services.catalog import CatalogService
from app.services.lifecycle import LifecycleService
from app.services.recycle import RecycleService
from app.services.retention import RetentionService
from app.store import store


class EventBoundaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一份全新内存库与全新边界，避免事件序号相互污染。
        store._tables = {  # noqa: SLF001
            name: [dict(row) for row in rows] for name, rows in seed.SEED_ROWS.items()
        }
        self.boundary = EventBoundary()
        # 服务内均以 `from app.events import event_boundary` 绑定全局对象，
        # 逐模块替换为当前用例的新边界实例。
        import app.services.lifecycle as life_mod
        import app.services.catalog as cat_mod
        import app.services.retention as ret_mod
        import app.services.recycle as rec_mod
        import app.services.audit as audit_mod

        for module in (life_mod, cat_mod, ret_mod, rec_mod, audit_mod):
            module.event_boundary = self.boundary
        self.lifecycle = LifecycleService()
        self.catalog = CatalogService()
        self.retention = RetentionService()
        self.recycle = RecycleService()
        self.audit = AuditService()

    # ---------------------------------------------------------- 编号语义核对

    def test_record_number_semantics_compatibility(self) -> None:
        """LIFE/CATA/RETN/RECY/AUDI 五个编号前缀按字段语义兼容核对。"""
        self.assertTrue(check_record_no(RULES["lifecycle"], "LIFE-0001"))
        self.assertTrue(check_record_no(RULES["catalog"], "CATA-0001"))
        self.assertTrue(check_record_no(RULES["recycle"], "RECY-0001"))
        self.assertTrue(check_record_no(RULES["audit"], "AUDI-0001"))
        # RETN- 是语义编号，RETE- 是改造前历史编号，两者都必须可读可动作。
        self.assertTrue(check_record_no(RULES["retention"], "RETN-0001"))
        self.assertTrue(check_record_no(RULES["retention"], "RETE-0001"))
        self.assertFalse(check_record_no(RULES["lifecycle"], "CATA-0001"))

    def test_legacy_audit_records_remain_readable_and_not_backfilled(self) -> None:
        """历史审计记录按原值可读，改造不补造、不改写。"""
        legacy = store.find("audit", 1)
        self.assertEqual(legacy["记录编号"], "AUDI-0001")
        self.assertEqual(legacy["变更对象"], "变更审计追踪样例1")
        self.assertNotIn("event_id", legacy)
        events, _ = self.boundary.list_events()
        self.assertEqual(events, [])

    # ------------------------------------------------------------ 成功单事件

    def test_success_produces_exactly_one_complete_event_and_audit_row(self) -> None:
        before_audit = len(store.rows("audit"))
        outcome = self.lifecycle.run_action(1, "启用策略", operator="值班管理员")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.record_no, "LIFE-0001")
        entry = store.find("lifecycle", 1)
        self.assertEqual(entry["status"], "生效中")
        self.assertEqual(entry["event_id"], outcome.event_id)
        self.assertEqual(entry["event_source"], "表生命周期管理")
        self.assertIsInstance(datetime.strptime(entry["event_time"], "%Y-%m-%d %H:%M:%S"), datetime)

        # 只有一条成功事件，字段完整。
        events, total = self.boundary.list_events(module="lifecycle")
        self.assertEqual(total, 1)
        event = events[0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["before_value"], "草稿")
        self.assertEqual(event["after_value"], "生效中")
        self.assertEqual(event["source_module"], "表生命周期管理")
        self.assertEqual(event["record_no"], "LIFE-0001")
        self.assertTrue(event["occurred_at"])
        self.assertEqual(event["operator"], "值班管理员")

        # 同一事件投影成一条审计记录，变更对象/类型/前后值/复核状态齐全。
        self.assertEqual(len(store.rows("audit")), before_audit + 1)
        audit_row = store.rows("audit")[-1]
        self.assertEqual(audit_row["变更对象"], "表生命周期管理/LIFE-0001")
        self.assertEqual(audit_row["变更类型"], "启用策略")
        self.assertEqual(audit_row["变更前值"], "草稿")
        self.assertEqual(audit_row["变更后值"], "生效中")
        self.assertEqual(audit_row["复核状态"], "已记录")
        self.assertEqual(audit_row["event_id"], outcome.event_id)
        self.assertEqual(audit_row["source_record_no"], "LIFE-0001")

    def test_audit_list_and_detail_read_same_event_by_record_no(self) -> None:
        outcome = self.recycle.run_action(1, "恢复数据")
        items, total = self.audit.list_entries(record_no="RECY-0001")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["event_id"], outcome.event_id)
        detail = self.audit.get_by_record_no(items[0]["记录编号"])
        self.assertEqual(detail["event_id"], outcome.event_id)
        chain = self.audit.events_for_record(items[0]["记录编号"])
        self.assertEqual([e["event_id"] for e in chain], [outcome.event_id])

    # ------------------------------------------------------------ 失败原子性

    def test_failure_changes_neither_side_but_keeps_error_summary(self) -> None:
        before_status = store.find("catalog", 1)["status"]
        before_audit = len(store.rows("audit"))
        before_events = len(self.boundary.list_events()[0])

        outcome = self.catalog.run_action(1, "废弃资产")  # 草稿态不允许废弃

        self.assertFalse(outcome.ok)
        self.assertIsNotNone(outcome.error_summary)
        self.assertEqual(store.find("catalog", 1)["status"], before_status)
        self.assertEqual(len(store.rows("audit")), before_audit)
        # 失败只多一条 failed 事件，不进业务表、不进审计表。
        events = self.boundary.list_events()[0]
        self.assertEqual(len(events), before_events + 1)
        failed = events[-1]
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["audit_record_no"])
        self.assertEqual(failed["record_no"], "CATA-0001")
        self.assertIn("重复推进", failed["error_summary"])

    def test_unknown_action_and_missing_record_are_rejected(self) -> None:
        missing = self.lifecycle.run_action(999, "启用策略")
        self.assertFalse(missing.ok)
        self.assertIsNone(missing.entry)
        unknown = self.lifecycle.run_action(1, "黑客动作")
        self.assertFalse(unknown.ok)
        # 两次失败都没有改动生命周期表。
        self.assertEqual(store.find("lifecycle", 1)["status"], "草稿")

    # ------------------------------------------------------ 重复/并发幂等

    def test_duplicate_action_has_only_one_effective_advance(self) -> None:
        first = self.lifecycle.run_action(1, "启用策略", request_id="r-1")
        second = self.lifecycle.run_action(1, "启用策略", request_id="r-2")
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        success_events = self.boundary.list_events(status="success")[0]
        self.assertEqual(len(success_events), 1)
        self.assertEqual(store.find("lifecycle", 1)["status"], "生效中")

    def test_same_request_id_replays_first_outcome(self) -> None:
        first = self.catalog.run_action(1, "提交复核", request_id="idem-1")
        replay = self.catalog.run_action(1, "提交复核", request_id="idem-1")
        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 1)

    def test_concurrent_actions_only_one_wins(self) -> None:
        outcomes: list[bool] = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            result = self.recycle.run_action(1, "恢复数据")
            outcomes.append(result.ok)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(store.find("recycle", 1)["status"], "已恢复")
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 1)

    # ------------------------------------------------------------ 负责人变更

    def test_owner_change_emits_single_event_with_old_and_new_owner(self) -> None:
        before_audit = len(store.rows("audit"))
        outcome = self.catalog.change_owner(1, "李四", operator="主管")
        self.assertTrue(outcome.ok)
        row = store.find("catalog", 1)
        self.assertEqual(row["负责人"], "李四")
        self.assertEqual(row["event_id"], outcome.event_id)
        self.assertEqual(len(store.rows("audit")), before_audit + 1)
        audit_row = store.rows("audit")[-1]
        self.assertEqual(audit_row["变更类型"], "变更负责人")
        self.assertIn("数据资产盘点样例1", audit_row["变更前值"])
        self.assertIn("李四", audit_row["变更后值"])
        self.assertEqual(audit_row["source_module"], "数据资产盘点")
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 1)
        # 再改成相同值必须被拒绝。
        again = self.catalog.change_owner(1, "李四")
        self.assertFalse(again.ok)

    # ------------------------------------------------------------ 审计自动作

    def test_audit_self_action_updates_same_row_without_new_audit_row(self) -> None:
        projected = self.lifecycle.run_action(1, "启用策略")
        audit_id = projected.audit_row["id"]
        count_before = len(store.rows("audit"))
        review = self.audit.run_action(audit_id, "标记复核", operator="复核员")
        self.assertTrue(review.ok)
        self.assertEqual(len(store.rows("audit")), count_before)
        row = store.find("audit", audit_id)
        self.assertEqual(row["复核状态"], "已复核")
        self.assertEqual(row["status"], "已复核")
        # 审计动作自身也只产生一条事件，且事件与该行同源。
        self.assertEqual(row["event_id"], review.event_id)
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 2)

    def test_legacy_audit_row_keeps_original_values_through_new_action(self) -> None:
        """历史审计记录原值不可被改写；对它的新动作只更新状态并登记新事件。"""
        legacy = store.find("audit", 1)
        original = {key: legacy[key] for key in ("变更对象", "变更类型", "变更前值", "变更后值")}
        review = self.audit.run_action(1, "标记复核", operator="复核员")
        self.assertTrue(review.ok)
        row = store.find("audit", 1)
        for key, value in original.items():
            self.assertEqual(row[key], value)
        self.assertEqual(row["复核状态"], "已复核")
        # 没有新增审计行，只多了一条审计模块自身的事件。
        self.assertEqual(len(store.rows("audit")), 3)
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 1)

    # ------------------------------------------------------------ 批量入口

    def test_batch_shares_parent_tx_and_rolls_back_as_a_whole(self) -> None:
        ok_batch = self.boundary.submit_batch(
            [
                {"module": "lifecycle", "entry_id": 1, "action": "启用策略"},
                {"module": "recycle", "entry_id": 1, "action": "恢复数据"},
            ]
        )
        self.assertTrue(ok_batch["ok"])
        ptx = ok_batch["parent_tx_id"]
        self.assertTrue(ptx)
        ptx_events = [
            e for e in self.boundary.list_events()[0] if e.get("parent_tx_id") == ptx
        ]
        self.assertEqual(len(ptx_events), 2)

        audit_snapshot = len(store.rows("audit"))
        bad_batch = self.boundary.submit_batch(
            [
                {"module": "catalog", "entry_id": 2, "action": "提交复核"},  # 已盘点，源状态不符
                {"module": "recycle", "entry_id": 3, "action": "恢复数据"},
            ]
        )
        self.assertFalse(bad_batch["ok"])
        self.assertTrue(bad_batch["parent_tx_id"])
        # 整批回滚：业务表与审计表都回到提交前。
        self.assertEqual(len(store.rows("audit")), audit_snapshot)
        self.assertEqual(store.find("catalog", 2)["status"], "已盘点")
        self.assertEqual(store.find("recycle", 3)["status"], "已清理")

    def test_rejected_batch_does_not_poison_idempotency(self) -> None:
        """被拒整批不产生幂等缓存；同 request_id 重试是一次全新的有效推进。"""
        bad_batch = self.boundary.submit_batch(
            [
                {"module": "lifecycle", "entry_id": 1, "action": "启用策略",
                 "request_id": "batch-rq-1"},
                {"module": "catalog", "entry_id": 2, "action": "提交复核",
                 "request_id": "batch-rq-2"},  # 源状态不符，预校验整批拒绝
            ]
        )
        self.assertFalse(bad_batch["ok"])
        self.assertEqual(store.find("lifecycle", 1)["status"], "草稿")
        self.assertNotIn("batch-rq-1", self.boundary._idempotent)  # noqa: SLF001
        retry = self.lifecycle.run_action(1, "启用策略", request_id="batch-rq-1")
        self.assertTrue(retry.ok)
        self.assertEqual(store.find("lifecycle", 1)["status"], "生效中")
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 1)

    def test_single_submit_unexpected_error_leaves_no_half_event(self) -> None:
        """单动作落库阶段意外异常：业务表与审计表都恢复，只多一条 failed 事件。"""
        from unittest import mock

        audit_before = len(store.rows("audit"))
        with mock.patch.object(
            EventBoundary, "_append_audit_row", side_effect=RuntimeError("审计投影失败")
        ):
            outcome = self.lifecycle.run_action(1, "启用策略")
        self.assertFalse(outcome.ok)
        self.assertIn("已回滚", outcome.error_summary)
        self.assertEqual(store.find("lifecycle", 1)["status"], "草稿")
        self.assertEqual(len(store.rows("audit")), audit_before)
        self.assertEqual(len(self.boundary.list_events(status="success")[0]), 0)
        self.assertEqual(len(self.boundary.list_events(status="failed")[0]), 1)

    def test_execution_phase_failure_physically_restores_both_tables(self) -> None:
        """执行阶段意外失败时，物理快照恢复业务表与审计表，两侧都不留痕。"""
        from unittest import mock

        real_submit = EventBoundary.submit
        calls: list[str] = []

        def flaky_submit(self, module, entry_id, action, **kwargs):  # noqa: ANN001
            calls.append(action)
            if len(calls) == 2:
                # 模拟第二条调用在执行阶段抛出未预期异常。
                raise RuntimeError("存储抖动")
            return real_submit(self, module, entry_id, action, **kwargs)

        audit_before = [dict(row) for row in store.rows("audit")]
        life_before = store.find("lifecycle", 1)["status"]
        with mock.patch.object(EventBoundary, "submit", flaky_submit):
            result = self.boundary.submit_batch(
                [
                    {"module": "lifecycle", "entry_id": 1, "action": "启用策略"},
                    {"module": "recycle", "entry_id": 1, "action": "恢复数据"},
                ]
            )
        self.assertFalse(result["ok"])
        self.assertEqual(store.find("lifecycle", 1)["status"], life_before)
        self.assertEqual(
            [row["记录编号"] for row in store.rows("audit")],
            [row["记录编号"] for row in audit_before],
        )

    def test_retention_historical_prefix_still_advances(self) -> None:
        outcome = self.retention.run_action(1, "启用规则")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.record_no, "RETE-0001")
        self.assertEqual(store.find("retention", 1)["status"], "生效中")

    def test_four_business_modules_and_audit_come_from_same_event(self) -> None:
        calls = [
            (self.lifecycle, 1, "启用策略", "LIFE-0001"),
            (self.catalog, 1, "提交复核", "CATA-0001"),
            (self.retention, 1, "启用规则", "RETE-0001"),
            (self.recycle, 1, "恢复数据", "RECY-0001"),
        ]
        for service, entry_id, action, record_no in calls:
            outcome = service.run_action(entry_id, action)
            self.assertTrue(outcome.ok, msg=outcome.error_summary)
            linked = self.audit.list_entries(record_no=record_no)[0]
            self.assertEqual(len(linked), 1)
            self.assertEqual(linked[0]["event_id"], outcome.event_id)


if __name__ == "__main__":
    unittest.main()
