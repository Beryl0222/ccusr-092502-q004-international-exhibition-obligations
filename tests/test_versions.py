"""协议版本生命周期：登记、取代、取消与迟到确认隔离。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from src.domain import (
    VERSION_CANCELLED,
    VERSION_SUPERSEDED,
    DomainError,
    build_projection,
)

from tests._fixtures import Scenario


class VersionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()

    def test_duplicate_version_ref_rejected(self) -> None:
        self.s.bootstrap_version()
        with self.assertRaisesRegex(DomainError, "协议版本已存在"):
            self.s.service.record_version("V1", at=self.s.t)

    def test_supersede_blocks_new_obligations(self) -> None:
        self.s.bootstrap_version()
        self.s.service.record_version("V2", supersedes="V1", at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.versions["V1"].status, VERSION_SUPERSEDED)
        with self.assertRaisesRegex(DomainError, "已被 V2 取代"):
            self.s.service.define_obligation(
                "V1", "OBL-X", "对方", self.s.t + timedelta(days=3), at=self.s.t)

    def test_cancel_superseded_version_rejected(self) -> None:
        self.s.bootstrap_version()
        self.s.service.record_version("V2", supersedes="V1", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "已被 V2 取代"):
            self.s.service.cancel_version("V1", "作废", at=self.s.t)

    def test_late_ack_for_old_version_cannot_confirm_obligation(self) -> None:
        self.s.bootstrap_version()
        # V2 取代 V1，对方时区迟到的确认仍指向 V1
        self.s.service.record_version("V2", supersedes="V1", at=self.s.t)
        self.s.advance(hours=20)
        self.s.service.receive_ack(
            "ACK-LATE", "对方夜班秘书", {"decision": "ACCEPT"},
            declared_version_ref="V1")
        self.s.service.define_obligation(
            "V2", "OBL-NEW", "对方", self.s.t + timedelta(days=5), at=self.s.t)
        with self.assertRaisesRegex(DomainError, "不能用于承诺所在版本 V2"):
            self.s.service.accept_obligation(
                "OBL-NEW", confirming_ack_ref="ACK-LATE", at=self.s.t)
        # 迟到回执自身仍被登记，且没有改变任何版本状态
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.versions["V1"].status, VERSION_SUPERSEDED)
        self.assertIn("ACK-LATE", p.acks_by_ref)

    def test_cancel_records_reason_and_keeps_history(self) -> None:
        self.s.bootstrap_version()
        event = self.s.service.cancel_version("V1", "对方撤展", at=self.s.t)
        self.assertEqual(event["payload"]["reason"], "对方撤展")
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.versions["V1"].status, VERSION_CANCELLED)
        # 历史事件仍可完整回放
        self.assertGreater(len(self.s.store), 1)

    def test_closure_snapshot_survives_supersession(self) -> None:
        self.s.bootstrap_version()
        self.s.release_ready_crate()
        self.s.service.hand_over(
            "CR-1", "联盟场馆组", "联盟库房", final=True,
            insurance_party="联盟自保单元", at=self.s.t)
        event = self.s.service.reconcile_closure("V1", at=self.s.t)
        snapshot = event["payload"]["custody_snapshot"]
        self.assertEqual(snapshot[0]["custodian"], "联盟场馆组")
        self.assertEqual(snapshot[0]["status"], "DELIVERED")
        self.s.service.record_version("V2", supersedes="V1", at=self.s.t)
        # 结项快照是追加事件，取代发生后仍可读出
        again = self.s.service.reconcile_closure("V1", at=self.s.t)
        self.assertEqual(again["payload"]["delivered_count"], 1)


if __name__ == "__main__":
    unittest.main()
