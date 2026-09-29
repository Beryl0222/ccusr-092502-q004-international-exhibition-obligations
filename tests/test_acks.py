"""外部回执：外部编号去重、内容冲突转人工、裁决不以到达顺序为准。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from src.domain import (
    ACK_CONFLICT_STATE,
    ACK_RESOLVED_STATE,
    DomainError,
    build_projection,
)

from tests._fixtures import Scenario


class AckDeduplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        self.content = {"decision": "ACCEPT", "items": 12}

    def _send(self, at_offset: int = 0, arrival_offset: int | None = None,
              sender: str = "联盟秘书 A"):
        t = self.s.t + timedelta(hours=at_offset)
        arrival = (self.s.t + timedelta(hours=arrival_offset)) \
            if arrival_offset is not None else t
        return self.s.service.receive_ack(
            "ACK-100", sender, self.content,
            declared_version_ref="V1",
            received_at=t, arrival_at=arrival)

    def test_same_ref_same_content_is_idempotent(self) -> None:
        first = self._send(at_offset=0)
        self.assertFalse(first["deduped"])
        events_before = len(self.s.store)
        # 对方时区网络重发：晚到 9 小时、内容一致
        duplicate = self._send(at_offset=0, arrival_offset=9, sender="联盟秘书 B")
        self.assertTrue(duplicate["deduped"])
        self.assertEqual(len(self.s.store), events_before)
        p = build_projection(self.s.store.iter_events())
        ack = p.acks_by_ref["ACK-100"]
        self.assertEqual(ack.first_sender, "联盟秘书 A")

    def test_same_ref_conflicting_content_goes_to_manual_queue(self) -> None:
        self._send(at_offset=0, arrival_offset=0)
        result = self.s.service.receive_ack(
            "ACK-100", "联盟秘书 B", {"decision": "REJECT", "items": 12},
            declared_version_ref="V1",
            received_at=self.s.t - timedelta(hours=2),
            arrival_at=self.s.t + timedelta(hours=1))
        self.assertEqual(result["state"], ACK_CONFLICT_STATE)
        chain = self.s.service.export_chain()
        self.assertIn("ACK-100", chain["manual_queue"])
        p = build_projection(self.s.store.iter_events())
        ack = p.acks_by_ref["ACK-100"]
        # 到达顺序不决定真假：首收内容保持为候选，冲突内容单独留档
        self.assertEqual(ack.first_content["decision"], "ACCEPT")
        self.assertEqual(ack.contenders[0]["content"]["decision"], "REJECT")
        self.assertEqual(ack.effective_content["decision"], "ACCEPT")

    def test_later_arrival_with_earlier_business_time_still_manual(self) -> None:
        # 先到服务器的内容声称业务时间更晚；系统仍不以后到内容覆盖先到内容
        self.s.service.receive_ack(
            "ACK-200", "夜班补发", {"decision": "CANCEL"},
            received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-200", "白班原件", {"decision": "ACCEPT"},
            received_at=self.s.t - timedelta(hours=8),
            arrival_at=self.s.t + timedelta(minutes=10))
        p = build_projection(self.s.store.iter_events())
        ack = p.acks_by_ref["ACK-200"]
        self.assertTrue(ack.conflict)
        self.assertEqual(ack.first_content["decision"], "CANCEL")

    def test_resolve_first_latest_manual(self) -> None:
        self.s.service.receive_ack(
            "ACK-300", "A", {"v": 1}, received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-300", "B", {"v": 2},
            received_at=self.s.t + timedelta(hours=1),
            arrival_at=self.s.t + timedelta(hours=2))
        with self.assertRaisesRegex(DomainError, "回执不存在"):
            self.s.service.resolve_ack(
                "ACK-NOPE", "FIRST", "协调员", at=self.s.t)
        self.s.service.resolve_ack(
            "ACK-300", "FIRST", "项目联络处", note="电话核实", at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        ack = p.acks_by_ref["ACK-300"]
        self.assertEqual(ack.state, ACK_RESOLVED_STATE)
        self.assertEqual(ack.effective_content, {"v": 1})
        self.assertEqual(ack.resolved_by, "项目联络处")

    def test_manual_resolution_requires_content(self) -> None:
        self.s.service.receive_ack(
            "ACK-400", "A", {"v": 1}, received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-400", "B", {"v": 2},
            received_at=self.s.t + timedelta(hours=1),
            arrival_at=self.s.t + timedelta(hours=1))
        with self.assertRaisesRegex(DomainError, "人工裁决必须提供"):
            self.s.service.resolve_ack("ACK-400", "MANUAL", "协调员", at=self.s.t)
        self.s.service.resolve_ack(
            "ACK-400", "MANUAL", "协调员", content={"v": 3, "note": "折中"},
            at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.acks_by_ref["ACK-400"].effective_content["v"], 3)

    def test_conflicting_ack_cannot_confirm_obligation(self) -> None:
        self.s.bootstrap_version()
        self.s.service.receive_ack(
            "ACK-500", "A", {"decision": "ACCEPT"},
            declared_version_ref="V1", received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-500", "B", {"decision": "REJECT"},
            declared_version_ref="V1",
            received_at=self.s.t, arrival_at=self.s.t + timedelta(hours=1))
        with self.assertRaisesRegex(DomainError, "内容冲突待人工协调"):
            self.s.service.accept_obligation(
                "OBL-COL", confirming_ack_ref="ACK-500", at=self.s.t)

    def test_repeated_conflict_copy_is_deduped(self) -> None:
        self.s.service.receive_ack(
            "ACK-600", "A", {"v": 1}, received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-600", "B", {"v": 2},
            received_at=self.s.t, arrival_at=self.s.t + timedelta(hours=1))
        events_before = len(self.s.store)
        result = self.s.service.receive_ack(
            "ACK-600", "B-AGAIN", {"v": 2},
            received_at=self.s.t, arrival_at=self.s.t + timedelta(hours=2))
        self.assertTrue(result["deduped"])
        self.assertEqual(len(self.s.store), events_before)


if __name__ == "__main__":
    unittest.main()
