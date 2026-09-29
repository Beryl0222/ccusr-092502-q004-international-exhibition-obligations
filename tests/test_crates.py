"""箱件放行：三职责闸门、暂停隔离、保管链与取消追溯。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from src.domain import (
    SUSPEND_OBLIGATION_EXPIRED,
    SUSPEND_VERSION_CANCELLED,
    DomainError,
    build_projection,
)

from tests._fixtures import Scenario


class CrateGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        self.s.bootstrap_version()
        self.s.list_crate("CR-1")

    def test_gate_requires_all_three_roles(self) -> None:
        with self.assertRaisesRegex(DomainError, "三个职责"):
            self.s.service.list_crate(
                "CR-X", "V1",
                {"COLLECTION": "OBL-COL", "INSURANCE": "OBL-INS"}, at=self.s.t)

    def test_gate_obligation_role_must_match(self) -> None:
        with self.assertRaisesRegex(DomainError, "不能作为 INSURANCE 闸门"):
            self.s.service.list_crate(
                "CR-X", "V1",
                {"COLLECTION": "OBL-COL", "INSURANCE": "OBL-TRN",
                 "TRANSPORT": "OBL-TRN"}, at=self.s.t)

    def test_release_requires_three_signatures(self) -> None:
        self.s.accept_all()
        self.s.fulfill_gates()
        with self.assertRaisesRegex(DomainError, "缺少馆藏职责确认"):
            self.s.service.release_crate("CR-1", at=self.s.t)
        self.s.service.sign_gate("CR-1", "COLLECTION", "馆藏主管", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "缺少保险职责确认"):
            self.s.service.release_crate("CR-1", at=self.s.t)
        self.s.service.sign_gate("CR-1", "INSURANCE", "保险主管", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "缺少运输职责确认"):
            self.s.service.release_crate("CR-1", at=self.s.t)
        self.s.service.sign_gate("CR-1", "TRANSPORT", "运输主管", at=self.s.t)
        self.s.service.release_crate("CR-1", at=self.s.t)

    def test_gate_cannot_sign_before_obligation_fulfilled(self) -> None:
        self.s.accept_all()
        with self.assertRaisesRegex(DomainError, "尚未满足"):
            self.s.service.sign_gate("CR-1", "COLLECTION", "主管", at=self.s.t)

    def test_gate_signature_is_single_use(self) -> None:
        self.s.accept_all()
        self.s.fulfill_gates()
        self.s.service.sign_gate("CR-1", "COLLECTION", "主管甲", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "不可重复签署"):
            self.s.service.sign_gate("CR-1", "COLLECTION", "主管乙", at=self.s.t)

    def test_cannot_hand_over_before_release(self) -> None:
        with self.assertRaisesRegex(DomainError, "尚未放行"):
            self.s.service.hand_over("CR-1", "承运商", "月台", at=self.s.t)

    def test_double_release_rejected(self) -> None:
        self.s.accept_all()
        self.s.fulfill_gates()
        self.s.sign_all("CR-1")
        self.s.service.release_crate("CR-1", at=self.s.t)
        with self.assertRaisesRegex(DomainError, "已放行"):
            self.s.service.release_crate("CR-1", at=self.s.t)


class SuspensionIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        self.s.bootstrap_version()
        # 额外承诺：权利许可，期限短，仅关联 CR-2 与宣传素材
        self.s.service.define_obligation(
            "V1", "OBL-RGT", "联盟宣传部", self.s.t + timedelta(days=5),
            title="权利许可",
            alternatives=[{"code": "ALT-RGT", "description": "临时授权函"}],
            at=self.s.t)
        self.s.list_crate("CR-1")
        self.s.list_crate("CR-2")
        self.s.service.link_crate("CR-2", "OBL-RGT", at=self.s.t)
        for code in ("OBL-COL", "OBL-INS", "OBL-TRN", "OBL-RGT"):
            self.s.service.accept_obligation(code, at=self.s.t)
        self.s.fulfill_gates()
        self.s.sign_all("CR-1")
        self.s.service.release_crate("CR-1", at=self.s.t)
        self.s.service.hand_over(
            "CR-1", "联盟场馆组", "联盟展厅", final=True,
            insurance_party="联盟自保单元", at=self.s.t)
        self.s.service.approve_publication(
            "V1", "PRESS-1", obligation_codes=["OBL-RGT"], at=self.s.t)

    def test_expiry_suspends_only_linked_crate_and_publication(self) -> None:
        self.s.advance(days=8)  # OBL-RGT 过期；闸门承诺期限 10/12/14 天未到
        count = self.s.service.sweep_expirations(at=self.s.t)
        self.assertEqual(count, 1)
        chain = self.s.service.export_chain()
        cr1 = next(c for c in chain["crates"] if c["code"] == "CR-1")
        cr2 = next(c for c in chain["crates"] if c["code"] == "CR-2")
        self.assertEqual(cr1["status"], "DELIVERED")
        self.assertEqual(cr2["status"], "SUSPENDED")
        self.assertEqual(
            cr2["suspensions"][0]["reason_key"], "OBL-RGT")
        press = chain["publications"][0]
        self.assertTrue(press["suspended"])

        # 暂停的 CR-2 不可放行（即使三闸门承诺都已满足）
        for role, signer in (("COLLECTION", "馆藏主管"),
                             ("INSURANCE", "保险主管"),
                             ("TRANSPORT", "运输主管")):
            self.s.service.sign_gate("CR-2", role, signer, at=self.s.t)
        with self.assertRaisesRegex(DomainError, "OBL-RGT 过期，箱件暂停"):
            self.s.service.release_crate("CR-2", at=self.s.t)

    def test_alternative_fulfilment_resumes_linked_objects(self) -> None:
        self.s.advance(days=8)
        self.s.service.sweep_expirations(at=self.s.t)
        self.s.service.submit_evidence(
            "OBL-RGT", "EV-RGT-ALT", "宣传专员",
            alternative_code="ALT-RGT", at=self.s.t)
        self.s.service.review_evidence(
            "OBL-RGT", "EV-RGT-ALT", "复核人 陈洁", True, at=self.s.t)
        chain = self.s.service.export_chain()
        cr2 = next(c for c in chain["crates"] if c["code"] == "CR-2")
        self.assertEqual(cr2["status"], "LISTED")
        self.assertEqual(cr2["suspensions"], [])
        self.assertFalse(chain["publications"][0]["suspended"])
        # 恢复事件本身也留在日志中，暂停/恢复成对可审计
        types = [e["event_type"] for e in self.s.store.iter_events()]
        self.assertIn("CRATE_RESUMED", types)
        self.assertIn("PUBLICATION_RESUMED", types)

    def test_two_expired_obligations_are_independently_accounted(self) -> None:
        # 同一箱件关联两个先后过期的承诺，暂停原因不得互相覆盖
        self.s.service.define_obligation(
            "V1", "OBL-CUST", "合规处", self.s.t + timedelta(days=7),
            title="出境许可",
            alternatives=[{"code": "ALT-CUST", "description": "电子担保"}],
            at=self.s.t)
        self.s.service.accept_obligation("OBL-CUST", at=self.s.t)
        self.s.service.link_crate("CR-2", "OBL-CUST", at=self.s.t)
        self.s.advance(days=6)  # RGT（5天）过期，CUST（7天）未到
        self.s.service.sweep_expirations(at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(
            set(p.crates["CR-2"].active_suspensions),
            {(SUSPEND_OBLIGATION_EXPIRED, "OBL-RGT")})
        self.s.advance(days=2)  # CUST 也过期
        self.s.service.sweep_expirations(at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(
            set(p.crates["CR-2"].active_suspensions),
            {(SUSPEND_OBLIGATION_EXPIRED, "OBL-RGT"),
             (SUSPEND_OBLIGATION_EXPIRED, "OBL-CUST")})
        # 只恢复其中一个，另一个暂停仍在
        self.s.service.submit_evidence(
            "OBL-CUST", "EV-C", "合规员", alternative_code="ALT-CUST", at=self.s.t)
        self.s.service.review_evidence(
            "OBL-CUST", "EV-C", "复核人", True, at=self.s.t)
        p = build_projection(self.s.store.iter_events())
        self.assertEqual(
            set(p.crates["CR-2"].active_suspensions),
            {(SUSPEND_OBLIGATION_EXPIRED, "OBL-RGT")})


class CancelTraceabilityTest(unittest.TestCase):
    def test_released_crate_keeps_custody_and_insurance_after_cancel(self) -> None:
        s = Scenario()
        s.bootstrap_version()
        s.release_ready_crate()
        s.service.hand_over(
            "CR-1", "承运商", "出库月台",
            insurance_party="联盟保险经纪人", at=s.t)
        s.advance(days=1)
        s.service.hand_over(
            "CR-1", "联盟场馆组", "展厅入库区",
            insurance_party="联盟自保单元", at=s.t)
        # 版本取消：在途箱件被暂停，但位置与保险责任历史保留
        s.service.cancel_version("V1", "突发撤展", at=s.t)
        chain = s.service.export_chain()
        crate = chain["crates"][0]
        self.assertEqual(crate["status"], "SUSPENDED")
        self.assertEqual(crate["current_custodian"], "联盟场馆组")
        self.assertEqual(crate["current_location"], "展厅入库区")
        self.assertEqual(crate["insurance_party"], "联盟自保单元")
        self.assertEqual(len(crate["custody_chain"]), 3)
        reasons = {sp["reason"] for sp in crate["suspensions"]}
        self.assertIn(SUSPEND_VERSION_CANCELLED, reasons)
        # 取消后仍允许登记实际交接（实体位置必须持续可追溯）
        s.service.hand_over(
            "CR-1", "退运承运商", "出境保税仓",
            insurance_party="退运保险承保人", at=s.t)
        chain = s.service.export_chain()
        crate = chain["crates"][0]
        self.assertEqual(crate["current_custodian"], "退运承运商")
        self.assertEqual(len(crate["custody_chain"]), 4)

    def test_unreleased_crate_marked_cancelled_but_history_retained(self) -> None:
        s = Scenario()
        s.bootstrap_version()
        s.list_crate("CR-9")
        s.service.cancel_version("V1", "撤展", at=s.t)
        chain = s.service.export_chain()
        crate = chain["crates"][0]
        self.assertEqual(crate["status"], "CANCELLED")
        self.assertEqual(crate["current_custodian"], "借出机构馆藏处")
        self.assertEqual(crate["current_location"], "借出机构库房")
        # 记录仍在，且永远不能放行
        with self.assertRaisesRegex(DomainError, "已取消"):
            s.service.release_crate("CR-9", at=s.t)


if __name__ == "__main__":
    unittest.main()
