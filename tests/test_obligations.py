"""承诺生命周期：接受期限、前置证据、职责分离与替代方案。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from src.domain import DomainError, build_projection

from tests._fixtures import Scenario


class ObligationLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        self.s.bootstrap_version()

    def test_obligation_requires_existing_version(self) -> None:
        with self.assertRaisesRegex(DomainError, "协议版本不存在"):
            self.s.service.define_obligation(
                "V9", "OBL-X", "对方", self.s.t + timedelta(days=1), at=self.s.t)

    def test_cannot_accept_after_deadline(self) -> None:
        self.s.advance(days=20)
        with self.assertRaisesRegex(DomainError, "已过期限"):
            self.s.service.accept_obligation("OBL-COL", at=self.s.t)

    def test_prerequisite_must_be_fulfilled(self) -> None:
        self.s.service.define_obligation(
            "V1", "OBL-PRE", "馆藏部", self.s.t + timedelta(days=8),
            prerequisite_codes=["OBL-COL"], role="COLLECTION", at=self.s.t)
        self.s.accept_all()
        with self.assertRaisesRegex(DomainError, "前置证据未满足"):
            self.s.service.accept_obligation("OBL-PRE", at=self.s.t)
        self.s.fulfill("OBL-COL", "EV-COL", "馆藏员")
        self.s.service.accept_obligation("OBL-PRE", at=self.s.t)

    def test_accept_is_idempotency_guarded(self) -> None:
        self.s.service.accept_obligation("OBL-COL", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "已被接受"):
            self.s.service.accept_obligation("OBL-COL", at=self.s.t)

    def test_submitter_cannot_review_own_evidence(self) -> None:
        self.s.service.accept_obligation("OBL-COL", at=self.s.t)
        self.s.service.submit_evidence("OBL-COL", "EV-1", "登记员 王敏", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "提交人不得复核自己的证据"):
            self.s.service.review_evidence(
                "OBL-COL", "EV-1", "登记员 王敏", True, at=self.s.t)

    def test_rejected_evidence_does_not_fulfil(self) -> None:
        self.s.service.accept_obligation("OBL-COL", at=self.s.t)
        self.s.service.submit_evidence("OBL-COL", "EV-1", "登记员 王敏", at=self.s.t)
        self.s.service.review_evidence(
            "OBL-COL", "EV-1", "复核人 陈洁", False, note="资料不全", at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.obligations["OBL-COL"].state, "ACCEPTED")
        # 已复核证据不可再次复核
        with self.assertRaisesRegex(DomainError, "已复核"):
            self.s.service.review_evidence(
                "OBL-COL", "EV-1", "复核人 陈洁", True, at=self.s.t)
        # 重新提交新证据，换一名复核人通过
        self.s.service.submit_evidence("OBL-COL", "EV-2", "登记员 王敏", at=self.s.t)
        self.s.service.review_evidence(
            "OBL-COL", "EV-2", "复核人 陈洁", True, at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertTrue(p.obligations["OBL-COL"].is_fulfilled)

    def test_evidence_before_acceptance_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "尚未被对方接受"):
            self.s.service.submit_evidence("OBL-COL", "EV-1", "登记员", at=self.s.t)

    def test_alternative_only_after_expiry(self) -> None:
        self.s.service.accept_obligation("OBL-INS", at=self.s.t)
        # 未过期时不得走替代方案
        with self.assertRaisesRegex(DomainError, "承诺未过期时不得直接使用替代方案"):
            self.s.service.submit_evidence(
                "OBL-INS", "EV-A", "保险经纪",
                alternative_code="ALT-BINDER", at=self.s.t)
        self.s.advance(days=20)
        # 巡检使承诺过期
        self.assertEqual(self.s.service.sweep_expirations(at=self.s.t), 1)
        with self.assertRaisesRegex(DomainError, "仅可按登记的替代方案"):
            self.s.service.submit_evidence("OBL-INS", "EV-B", "保险经纪", at=self.s.t)
        self.s.service.submit_evidence(
            "OBL-INS", "EV-ALT", "保险经纪",
            alternative_code="ALT-BINDER", at=self.s.t)
        self.s.service.review_evidence(
            "OBL-INS", "EV-ALT", "复核人 陈洁", True, at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(p.obligations["OBL-INS"].state, "FULFILLED_ALTERNATIVE")

    def test_unknown_alternative_code_rejected(self) -> None:
        self.s.service.accept_obligation("OBL-INS", at=self.s.t)
        self.s.advance(days=20)
        self.s.service.sweep_expirations(at=self.s.t)
        with self.assertRaisesRegex(DomainError, "仅可按登记的替代方案"):
            self.s.service.submit_evidence(
                "OBL-INS", "EV-X", "保险经纪",
                alternative_code="NO-SUCH", at=self.s.t)


if __name__ == "__main__":
    unittest.main()
