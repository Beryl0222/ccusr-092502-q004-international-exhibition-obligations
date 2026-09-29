"""领域规则测试：覆盖需求中的关键不变量。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.domain import DomainError, fold
from src.eventstore import EventStore
from src.service import ObligationService

ROOT = Path(__file__).resolve().parents[1]


def make_service() -> ObligationService:
    contract = json.loads((ROOT / "contracts" / "domain.json").read_text(encoding="utf-8"))
    return ObligationService(EventStore(None, set(contract["events"])))


class Scenario:
    """标准联调场景：协议 v1、门控承诺、箱件、物件。"""

    def __init__(self) -> None:
        self.svc = make_service()
        ag = self.svc.draft_agreement({
            "agreement_ref": "MOU-v1", "title": "联展备忘录",
            "parties": [{"id": "MUSEUM"}, {"id": "CITIES"}],
        })
        self.agreement_id = ag["aggregate_id"]
        self.svc.activate_agreement(self.agreement_id)
        ob = self.svc.accept_obligation({
            "code": "OBL-1", "agreement_version_id": self.agreement_id,
            "description": "保险与状况", "responsible_party_id": "CITIES",
            "deadline": "2026-10-01T18:00:00+02:00",
            "prerequisite_evidence_kinds": ["INSURANCE_CERTIFICATE"],
            "acceptable_alternatives": {"INSURANCE_CERTIFICATE": "CONDITION_REPORT"},
            "crate_codes": ["CRATE-1"], "publication_codes": ["PUB-1"],
        })
        self.obligation_id = ob["aggregate_id"]
        cr = self.svc.assemble_crate({
            "code": "CRATE-1", "origin_location": "本馆库房", "origin_party_id": "MUSEUM",
        })
        self.crate_id = cr["aggregate_id"]
        self.svc.list_object({
            "code": "OBJ-1", "title": "青铜鼎", "kind": "PHYSICAL",
            "agreement_version_id": self.agreement_id, "owner_party_id": "MUSEUM",
        })
        self.svc.pack_object(self.crate_id, {"object_code": "OBJ-1"})

    def crate(self):
        return self.svc.state.crate_by_code("CRATE-1")


class TripleConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()

    def test_three_roles_must_all_confirm_before_release(self) -> None:
        svc, cid = self.s.svc, self.s.crate_id
        svc.confirm_crate_role(cid, "COLLECTION", {"confirmer_party_id": "MUSEUM-REG"})
        svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "INSCO"})
        with self.assertRaises(DomainError):  # 缺运输
            svc.release_crate(cid, {"carrier_party_id": "DHL"})
        svc.confirm_crate_role(cid, "TRANSPORT", {"confirmer_party_id": "DHL"})

    def test_same_party_cannot_hold_two_roles(self) -> None:
        svc, cid = self.s.svc, self.s.crate_id
        svc.confirm_crate_role(cid, "COLLECTION", {"confirmer_party_id": "SAME"})
        with self.assertRaisesRegex(DomainError, "三种职责须由不同责任方"):
            svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "SAME"})

    def test_role_cannot_confirm_twice(self) -> None:
        svc, cid = self.s.svc, self.s.crate_id
        svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "INSCO"})
        with self.assertRaisesRegex(DomainError, "不得重复确认"):
            svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "INSCO-2"})

    def test_submitter_cannot_review_own_evidence(self) -> None:
        with self.assertRaisesRegex(DomainError, "提交人不得复核自己的证据"):
            self.s.svc.sign_evidence(self.s.obligation_id, {
                "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "D1",
                "submitted_by": "alice", "sign_off_by": "alice",
            }, alternative=False)

    def test_gate_blocks_release_until_evidence_signed(self) -> None:
        svc, cid = self.s.svc, self.s.crate_id
        for role, who in [("COLLECTION", "MREG"), ("INSURANCE", "INSCO"), ("TRANSPORT", "DHL")]:
            svc.confirm_crate_role(cid, role, {"confirmer_party_id": who})
        with self.assertRaisesRegex(DomainError, "暂停状态"):
            svc.release_crate(cid, {"carrier_party_id": "DHL"})
        svc.sign_evidence(self.s.obligation_id, {
            "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "I1",
            "submitted_by": "CITIES", "sign_off_by": "MUSEUM-CUR",
        }, alternative=False)
        svc.release_crate(cid, {"carrier_party_id": "DHL"})
        self.assertEqual(self.s.crate().state, "RELEASED_TO_CARRIER")


class ExpiryScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        svc = self.s.svc
        # 第二个无关箱件与无关公布
        cr2 = svc.assemble_crate({"code": "CRATE-2", "origin_location": "二库", "origin_party_id": "MUSEUM"})
        self.crate2_id = cr2["aggregate_id"]
        svc.draft_publication({"code": "PUB-1", "title": "门控稿", "material_ref": "M1",
                               "agreement_version_id": self.s.agreement_id,
                               "obligation_codes": ["OBL-1"]})
        svc.draft_publication({"code": "PUB-2", "title": "无关稿", "material_ref": "M2",
                               "agreement_version_id": self.s.agreement_id,
                               "obligation_codes": []})

    def test_expiry_suspends_only_linked_crate_and_publication(self) -> None:
        svc = self.s.svc
        svc.sweep_expired(now="2026-10-02T00:00:00+02:00")
        self.assertEqual(svc.state.crate_status(self.s.crate()), "HOLD_PENDING_CONDITIONS")
        self.assertEqual(svc.state.crate_status(svc.state.crate_by_code("CRATE-2")), "ASSEMBLED")
        self.assertEqual(svc.state.publication_by_code("PUB-1").state, "HELD")
        self.assertEqual(svc.state.publication_by_code("PUB-2").state, "DRAFT")

    def test_late_completion_with_alternative_lifts_hold_but_keeps_trace(self) -> None:
        svc = self.s.svc
        svc.sweep_expired(now="2026-10-02T00:00:00+02:00")
        ob = svc.state.obligation_by_code("OBL-1")
        self.assertEqual(svc.state.obligation_state(ob), "BLOCKED")
        svc.sign_evidence(self.s.obligation_id, {
            "satisfies_kind": "INSURANCE_CERTIFICATE",
            "alternative_evidence_kind": "CONDITION_REPORT",
            "evidence_ref": "CR-1", "submitted_by": "CITIES", "sign_off_by": "CUR",
        }, alternative=True)
        ob = svc.state.obligation_by_code("OBL-1")
        self.assertEqual(svc.state.obligation_state(ob), "RECOVERED")  # 逾期补全留痕
        self.assertEqual(svc.state.crate_status(self.s.crate()), "ASSEMBLED")  # 箱件暂停解除
        self.assertEqual(svc.state.publication_by_code("PUB-1").state, "DRAFT")  # 回到暂停前

    def test_unlisted_alternative_is_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "替代证据类型不被接受"):
            self.s.svc.sign_evidence(self.s.obligation_id, {
                "satisfies_kind": "INSURANCE_CERTIFICATE",
                "alternative_evidence_kind": "STATUS_REPORT",
                "evidence_ref": "X", "submitted_by": "C", "sign_off_by": "M",
            }, alternative=True)

    def test_satisfied_gate_keeps_approved_publication_releasable(self) -> None:
        # 批准要求门控已满足；满足后的承诺不会再被过期扫描判为暂停，
        # 因此已批准未发布的版本在扫描后仍可发布（已发布记录则连暂停都不受影响）。
        svc = self.s.svc
        pid = svc.state.publication_by_code("PUB-1").id  # setUp 已创建并门控 OBL-1
        svc.sign_evidence(self.s.obligation_id, {
            "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "I1",
            "submitted_by": "CITIES", "sign_off_by": "CUR",
        }, alternative=False)
        svc.approve_publication(pid, {"approved_by": "CUR"})
        svc.sweep_expired(now="2026-10-02T00:00:00+02:00")
        self.assertEqual(svc.state.publication_by_code("PUB-1").state,
                         "APPROVED_FOR_RELEASE")
        svc.publish_publication(pid, {"published_ref": "URL-9"})
        # 再扫描也不得影响已发布记录
        svc.sweep_expired(now="2026-11-01T00:00:00+02:00")
        self.assertEqual(svc.state.publication_by_code("PUB-1").state, "PUBLISHED")


class AgreementVersioningTest(unittest.TestCase):
    def test_late_confirmation_cannot_revive_superseded_version(self) -> None:
        s = Scenario()
        svc = s.svc
        v2 = svc.draft_agreement({"agreement_ref": "MOU-v2", "title": "v2"})
        svc.activate_agreement(v2["aggregate_id"])
        svc.supersede_agreement(s.agreement_id, {"replaced_by": "MOU-v2"})
        with self.assertRaisesRegex(DomainError, "迟到确认不得使其重新生效"):
            svc.accept_obligation({
                "code": "OBL-LATE", "agreement_version_id": s.agreement_id,
                "description": "迟到", "responsible_party_id": "C",
                "deadline": "2026-11-01T00:00:00+00:00",
                "prerequisite_evidence_kinds": ["STATUS_REPORT"],
            })
        self.assertEqual(svc.state.agreement_by_ref("MOU-v1").state, "SUPERSEDED")

    def test_late_inbound_ack_is_logged_but_not_effective(self) -> None:
        s = Scenario()
        svc = s.svc
        v2 = svc.draft_agreement({"agreement_ref": "MOU-v2", "title": "v2"})
        svc.activate_agreement(v2["aggregate_id"])
        svc.supersede_agreement(s.agreement_id, {"replaced_by": "MOU-v2"})
        events = svc.receive_ack({
            "ack_ref": "ACK-LATE", "sender_party_id": "CITIES",
            "content": {"confirm": True},
            "regarding": {"kind": "agreement_confirmation", "agreement_ref": "MOU-v1"},
        })
        self.assertTrue(events[0]["payload"]["late_and_not_effective"])
        self.assertEqual(svc.state.agreement_by_ref("MOU-v1").state, "SUPERSEDED")


class ExternalAckTest(unittest.TestCase):
    def _three(self, svc):
        svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 1}})
        dup = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 1}})
        conflict = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 2}})
        return dup, conflict

    def test_duplicate_is_deduped_and_conflict_escalated_not_order_decided(self) -> None:
        svc = make_service()
        dup, conflict = self._three(svc)
        self.assertEqual(dup[0]["event_type"], "ACK_DUPLICATE_DISCARDED")
        self.assertEqual([e["event_type"] for e in conflict],
                         ["ACK_RECEIVED", "ACK_CONFLICT_ESCALATED"])
        # 首版与冲突版都被标记 IN_CONFLICT，系统不自动把首个当真
        case = next(iter(svc.state.cases.values()))
        self.assertEqual(case.state, "OPEN")
        self.assertEqual(len(case.ack_ids), 2)
        self.assertTrue(
            all(svc.state.acks[i].state == "IN_CONFLICT" for i in case.ack_ids)
        )

    def test_resolution_keeps_all_versions_traceable(self) -> None:
        svc = make_service()
        first = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 1}})[0]
        svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 2}})
        case = next(iter(svc.state.cases.values()))
        svc.resolve_case(case.id, {
            "chosen_ack_id": first["aggregate_id"],
            "resolution": "人工核对后采用首版", "resolved_by": "OFFICER",
        })
        self.assertEqual(case.chosen_ack_id, first["aggregate_id"])
        # 原始两条回执都仍在事件流中
        self.assertEqual(len(svc.store.events(first["aggregate_id"])), 1)

    def test_post_resolution_matching_is_dup_contradiction_opens_new_case(self) -> None:
        svc = make_service()
        first = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 1}})[0]
        svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 2}})
        case = next(iter(svc.state.cases.values()))
        svc.resolve_case(case.id, {"chosen_ack_id": first["aggregate_id"],
                                   "resolution": "r", "resolved_by": "o"})
        same = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 1}})
        self.assertEqual(same[0]["event_type"], "ACK_DUPLICATE_DISCARDED")
        other = svc.receive_ack({"ack_ref": "A1", "sender_party_id": "C", "content": {"v": 9}})
        self.assertEqual(other[-1]["event_type"], "ACK_CONFLICT_ESCALATED")


class CancellationTraceTest(unittest.TestCase):
    def test_cancelled_crate_keeps_location_custody_and_insurance(self) -> None:
        s = Scenario()
        svc = s.svc
        svc.cancel_crate(s.crate_id, {
            "final_location": "本馆库房B区", "custodian_party_id": "MUSEUM-STORE",
            "insurance_responsible_party_id": "INSCO", "reason": "签证延误",
        })
        crate = s.crate()
        self.assertEqual(crate.state, "CANCELLED")
        self.assertEqual(crate.current_location, "本馆库房B区")
        last = crate.ledger[-1]
        self.assertEqual(last.custodian_party_id, "MUSEUM-STORE")
        self.assertIn("INSCO", last.note)

    def test_published_record_cannot_be_deleted(self) -> None:
        s = Scenario()
        svc = s.svc
        svc.sign_evidence(s.obligation_id, {
            "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "I",
            "submitted_by": "C", "sign_off_by": "M",
        }, alternative=False)
        svc.draft_publication({"code": "PUB-1", "title": "稿", "material_ref": "M",
                               "agreement_version_id": s.agreement_id,
                               "obligation_codes": ["OBL-1"]})
        pid = svc.state.publication_by_code("PUB-1").id
        svc.approve_publication(pid, {"approved_by": "M"})
        svc.publish_publication(pid, {"published_ref": "URL-1"})
        with self.assertRaisesRegex(DomainError, "已发布记录不得删除"):
            svc.cancel_publication(pid, {"reason": "撤回"})
        svc.supersede_publication(pid, {"replaced_by": "PUB-2"})
        chain = svc.responsibility_chain()
        view = next(p for p in chain["publications"] if p["code"] == "PUB-1")
        self.assertEqual(view["state"], "SUPERSEDED")
        self.assertEqual(view["published_ref"], "URL-1")  # 原发布记录仍可追溯


class DigitalReproductionTest(unittest.TestCase):
    def test_physical_roles_can_be_na_but_insurance_still_required(self) -> None:
        s = Scenario()
        svc = s.svc
        ob = svc.accept_obligation({
            "code": "OBL-DIGITAL", "agreement_version_id": s.agreement_id,
            "description": "数字复制证书", "responsible_party_id": "MUSEUM",
            "deadline": "2026-10-10T00:00:00+00:00",
            "prerequisite_evidence_kinds": ["DIGITAL_REPRODUCTION_CERTIFICATE"],
            "crate_codes": ["CRATE-D"],
        })
        cr = svc.assemble_crate({
            "code": "CRATE-D", "origin_location": "机房", "origin_party_id": "MUSEUM",
            "roles_not_applicable": {
                "COLLECTION": "数字复制品无实体出库",
                "TRANSPORT": "纯网络交付无承运人",
            },
        })
        cid = cr["aggregate_id"]
        with self.assertRaisesRegex(DomainError, "不适用"):
            svc.confirm_crate_role(cid, "COLLECTION", {"confirmer_party_id": "X"})
        with self.assertRaises(DomainError):  # 缺保险，不能放行
            svc.release_crate(cid, {"carrier_party_id": "NET"})
        svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "INSCO"})
        with self.assertRaisesRegex(DomainError, "暂停状态"):  # 门控承诺未满足
            svc.release_crate(cid, {"carrier_party_id": "NET"})
        # 数字复制证书签收（提交人与复核人不同）后，仅保险一岗即可放行
        svc.sign_evidence(ob["aggregate_id"], {
            "evidence_kind": "DIGITAL_REPRODUCTION_CERTIFICATE", "evidence_ref": "DRC-1",
            "submitted_by": "MUSEUM", "sign_off_by": "CITIES-REG",
        }, alternative=False)
        svc.release_crate(cid, {"carrier_party_id": "NET"})
        self.assertEqual(svc.state.crate_by_code("CRATE-D").state, "RELEASED_TO_CARRIER")


class EventImmutabilityTest(unittest.TestCase):
    def test_corrections_are_appended_not_rewritten(self) -> None:
        s = Scenario()
        svc = s.svc
        before = len(svc.store)
        svc.sign_evidence(s.obligation_id, {
            "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "I1",
            "submitted_by": "C", "sign_off_by": "M",
        }, alternative=False)
        # 同类型证据不可覆盖；只能以新事件存在
        with self.assertRaises(DomainError):
            svc.sign_evidence(s.obligation_id, {
                "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "I2-CORRECTED",
                "submitted_by": "C", "sign_off_by": "M2",
            }, alternative=False)
        self.assertEqual(len(svc.store), before + 1)

    def test_replay_reconstructs_identical_state(self) -> None:
        s = Scenario()
        svc = s.svc
        svc.sweep_expired(now="2026-10-02T00:00:00+02:00")
        rebuilt = fold(svc.store.all_events())
        self.assertEqual(
            rebuilt.crate_status(rebuilt.crate_by_code("CRATE-1")),
            svc.state.crate_status(svc.state.crate_by_code("CRATE-1")),
        )
        self.assertEqual(len(rebuilt.obligations), len(svc.state.obligations))


if __name__ == "__main__":
    unittest.main()
