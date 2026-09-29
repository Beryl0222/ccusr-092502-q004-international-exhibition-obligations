"""HTTP API 端到端测试：真实 socket + 内存事件库。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.api import make_handler
from src.eventstore import EventStore
from tests.test_domain import make_service  # 复用构造器

ROOTS = (
    "agreement_version", "obligation", "loan_object", "crate_transfer",
    "specialist_invitation", "publication", "external_ack", "coordination_case",
)


class ApiServer:
    def __init__(self) -> None:
        self.service = make_service()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ApiServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ApiFlowTest(unittest.TestCase):
    def test_full_handover_flow(self) -> None:
        with ApiServer() as api:
            status, ag = api.call("POST", "/agreements", {
                "agreement_ref": "MOU-1", "title": "联展备忘录"})
            self.assertEqual(status, 201)
            aid = ag["event"]["aggregate_id"]
            self.assertEqual(api.call("POST", f"/agreements/{aid}/activate")[0], 200)

            status, ob = api.call("POST", "/obligations", {
                "code": "O1", "agreement_version_id": aid, "description": "保险",
                "responsible_party_id": "CITIES", "deadline": "2026-10-10T18:00:00+02:00",
                "prerequisite_evidence_kinds": ["INSURANCE_CERTIFICATE"],
                "acceptable_alternatives": {"INSURANCE_CERTIFICATE": "CONDITION_REPORT"},
                "crate_codes": ["K1"], "publication_codes": ["P1"]})
            self.assertEqual(status, 201)
            self.assertEqual(ob["event"]["version"], 1)

            self.assertEqual(api.call("POST", "/crates", {
                "code": "K1", "origin_location": "库房", "origin_party_id": "MUSEUM"})[0], 201)
            self.assertEqual(api.call("POST", "/objects", {
                "code": "ART1", "title": "陶罐", "kind": "PHYSICAL",
                "agreement_version_id": aid, "owner_party_id": "MUSEUM"})[0], 201)
            self.assertEqual(api.call("POST", "/crates/K1/objects",
                                      {"object_code": "ART1"})[0], 200)

            for role, who in [("collection", "MREG"), ("insurance", "INSCO"), ("transport", "DHL")]:
                self.assertEqual(api.call("POST", f"/crates/K1/confirmations/{role}",
                                          {"confirmer_party_id": who})[0], 200)

            # 门控未满足 -> 422
            status, err = api.call("POST", "/crates/K1/release", {"carrier_party_id": "DHL"})
            self.assertEqual(status, 422)
            self.assertIn("暂停", err["error"])

            # 自核 -> 422
            status, err = api.call("POST", "/obligations/O1/evidence", {
                "evidence_kind": "INSURANCE_CERTIFICATE", "evidence_ref": "D",
                "submitted_by": "a", "sign_off_by": "a"})
            self.assertEqual(status, 422)

            # 替代证据解除门控
            self.assertEqual(api.call("POST", "/obligations/O1/alternative-evidence", {
                "satisfies_kind": "INSURANCE_CERTIFICATE",
                "alternative_evidence_kind": "CONDITION_REPORT",
                "evidence_ref": "CR", "submitted_by": "C", "sign_off_by": "M"})[0], 200)

            self.assertEqual(api.call("POST", "/crates/K1/release",
                                      {"carrier_party_id": "DHL"})[0], 200)
            self.assertEqual(api.call("POST", "/crates/K1/deliver", {
                "location": "海外展厅", "received_by_party_id": "CV"})[0], 200)

            status, chain = api.call("GET", "/chain")
            self.assertEqual(status, 200)
            view = next(c for c in chain["crates"] if c["code"] == "K1")
            self.assertEqual(view["status"], "DELIVERED")
            self.assertEqual(view["current_custodian"]["party_id"], "CV")
            self.assertIn("DELIVERED", [e["action"] for e in view["custody_ledger"]])

    def test_ack_conflict_and_resolution(self) -> None:
        with ApiServer() as api:
            self.assertEqual(api.call("POST", "/acks", {
                "ack_ref": "A9", "sender_party_id": "C", "content": {"v": 1}})[0], 202)
            status, dup = api.call("POST", "/acks", {
                "ack_ref": "A9", "sender_party_id": "C", "content": {"v": 1}})
            self.assertEqual(dup["received"][0]["event_type"], "ACK_DUPLICATE_DISCARDED")
            status, conflict = api.call("POST", "/acks", {
                "ack_ref": "A9", "sender_party_id": "C", "content": {"v": 2}})
            self.assertEqual([e["event_type"] for e in conflict["received"]],
                             ["ACK_RECEIVED", "ACK_CONFLICT_ESCALATED"])
            status, cases = api.call("GET", "/cases")
            self.assertEqual(status, 200)
            case_id = cases["items"][0]["id"]
            _, acks = api.call("GET", "/acks")
            chosen = next(a["id"] for a in acks["items"] if a["state"] == "IN_CONFLICT")
            status, resolved = api.call("POST", f"/cases/{case_id}/resolve", {
                "chosen_ack_id": chosen, "resolution": "采用该版", "resolved_by": "OFF"})
            self.assertEqual(status, 200)
            self.assertEqual(resolved["event"]["event_type"], "COORDINATION_RESOLVED")

    def test_late_ack_is_flagged_and_chain_explains_hold(self) -> None:
        with ApiServer() as api:
            _, ag = api.call("POST", "/agreements", {"agreement_ref": "M1", "title": "t"})
            aid = ag["event"]["aggregate_id"]
            api.call("POST", f"/agreements/{aid}/activate")
            _, v2 = api.call("POST", "/agreements", {"agreement_ref": "M2", "title": "t"})
            api.call("POST", f"/agreements/{v2['event']['aggregate_id']}/activate")
            api.call("POST", f"/agreements/{aid}/supersede", {"replaced_by": "M2"})
            _, ack = api.call("POST", "/acks", {
                "ack_ref": "LATE", "sender_party_id": "C", "content": {"ok": True},
                "regarding": {"kind": "agreement_confirmation", "agreement_ref": "M1"}})
            self.assertTrue(ack["received"][0]["event_type"] == "ACK_RECEIVED")
            _, acks = api.call("GET", "/acks")
            self.assertTrue(acks["items"][0]["late_and_not_effective"])

            # 责任链对暂停箱件给出明确原因
            api.call("POST", "/obligations", {
                "code": "O1", "agreement_version_id": v2["event"]["aggregate_id"],
                "description": "d", "responsible_party_id": "C",
                "deadline": "2026-10-01T00:00:00+00:00",
                "prerequisite_evidence_kinds": ["STATUS_REPORT"], "crate_codes": ["K1"]})
            api.call("POST", "/crates", {"code": "K1", "origin_location": "库",
                                         "origin_party_id": "M"})
            api.call("POST", "/admin/sweep-expired", {"now": "2026-10-02T00:00:00+00:00"})
            _, chain = api.call("GET", "/chain")
            view = next(c for c in chain["crates"] if c["code"] == "K1")
            self.assertEqual(view["status"], "HOLD_PENDING_CONDITIONS")
            self.assertTrue(view["hold_reasons"])

    def test_unknown_path_and_bad_json(self) -> None:
        with ApiServer() as api:
            self.assertEqual(api.call("GET", "/nope")[0], 404)
            req = urllib.request.Request(
                f"http://127.0.0.1:{api.port}/agreements",
                data=b"{not json", method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req)
                self.fail("应返回 422")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 422)


if __name__ == "__main__":
    unittest.main()
