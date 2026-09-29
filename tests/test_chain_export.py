"""责任链导出：为何可移交、当前由谁保管、卡点可读。"""
from __future__ import annotations

import unittest

from tests._fixtures import Scenario


class ChainExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scenario()
        self.s.bootstrap_version()

    def test_unready_crate_explains_each_blocker(self) -> None:
        self.s.list_crate("CR-1")
        self.s.accept_all()
        chain = self.s.service.export_chain()
        crate = chain["crates"][0]
        self.assertFalse(crate["releasable"])
        blockers = " ".join(crate["blocking_reasons"])
        self.assertIn("馆藏", blockers)
        self.assertIn("保险", blockers)
        self.assertIn("运输", blockers)
        # 每个闸门都注明责任方与关联承诺
        for role in ("COLLECTION", "INSURANCE", "TRANSPORT"):
            gate = crate["gates"][role]
            self.assertEqual(gate["obligation_state"], "ACCEPTED")
            self.assertFalse(gate["signed"])
            self.assertTrue(gate["responsible_party"])

    def test_ready_crate_is_releasable_and_origin_custody_shown(self) -> None:
        self.s.list_crate("CR-1")
        self.s.accept_all()
        self.s.fulfill_gates()
        self.s.sign_all("CR-1")
        crate = self.s.service.export_chain()["crates"][0]
        self.assertEqual(crate["status"], "READY")
        self.assertTrue(crate["releasable"])
        self.s.service.release_crate("CR-1", at=self.s.t)
        crate = self.s.service.export_chain()["crates"][0]
        self.assertEqual(crate["status"], "RELEASED")
        self.assertEqual(crate["current_custodian"], "借出机构馆藏处")
        origin = crate["custody_chain"][0]
        self.assertEqual(origin["kind"], "ORIGIN")
        for role in ("COLLECTION", "INSURANCE", "TRANSPORT"):
            self.assertTrue(crate["gates"][role]["signed"])
            self.assertTrue(crate["gates"][role]["evidence_ref"])
            self.assertTrue(crate["gates"][role]["evidence_reviewer"])

    def test_custody_and_insurance_chain_tracks_handovers(self) -> None:
        self.s.release_ready_crate()
        self.s.service.hand_over(
            "CR-1", "承运商押运组", "出库月台",
            insurance_party="联盟保险经纪人", at=self.s.t)
        self.s.advance(days=3)
        self.s.service.hand_over(
            "CR-1", "联盟场馆组", "展厅入库区",
            insurance_party="联盟自保单元", final=True, at=self.s.t)
        crate = self.s.service.export_chain()["crates"][0]
        self.assertEqual(crate["status"], "DELIVERED")
        self.assertEqual(crate["current_custodian"], "联盟场馆组")
        self.assertEqual(crate["current_location"], "展厅入库区")
        self.assertEqual(crate["insurance_party"], "联盟自保单元")
        chain = crate["custody_chain"]
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[1]["insurance_party"], "联盟保险经纪人")
        self.assertTrue(chain[2]["final"])

    def test_manual_queue_and_versions_exported(self) -> None:
        self.s.service.receive_ack(
            "ACK-1", "A", {"v": 1}, received_at=self.s.t, arrival_at=self.s.t)
        self.s.service.receive_ack(
            "ACK-1", "B", {"v": 2}, received_at=self.s.t,
            arrival_at=self.s.t)
        chain = self.s.service.export_chain()
        self.assertEqual(chain["manual_queue"], ["ACK-1"])
        self.assertEqual(chain["versions"][0]["status"], "RECORDED")

    def test_obligation_export_links_crates_and_evidence(self) -> None:
        self.s.list_crate("CR-1")
        self.s.accept_all()
        self.s.fulfill_gates()
        chain = self.s.service.export_chain()
        col = next(o for o in chain["obligations"] if o["code"] == "OBL-COL")
        self.assertIn("CR-1", col["linked_crates"])
        self.assertEqual(col["accepted_evidence"], ["EV-COL"])
        self.assertEqual(col["state"], "FULFILLED")


if __name__ == "__main__":
    unittest.main()
