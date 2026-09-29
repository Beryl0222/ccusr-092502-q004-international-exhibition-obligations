"""HTTP 适配层：路由分发与真实套接字端到端联调。"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from src.api import build_service, dispatch, make_handler, serve
from src.storage import InMemoryEventStore, JsonlEventStore

from tests._fixtures import T0


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service(InMemoryEventStore())

    def test_health_and_unknown_route(self) -> None:
        self.assertEqual(dispatch(self.service, "GET", "/health", None)[0], 200)
        status, body = dispatch(self.service, "DELETE", "/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_missing_field_is_400(self) -> None:
        status, body = dispatch(self.service, "POST", "/versions", {})
        self.assertEqual(status, 400)
        self.assertIn("version_ref", body["error"])

    def test_domain_violation_is_400_not_500(self) -> None:
        status, _ = dispatch(self.service, "POST", "/crates", {
            "code": "CR-X", "version_ref": "V1",
            "gates": {"COLLECTION": "X", "INSURANCE": "Y", "TRANSPORT": "Z"},
        })
        self.assertEqual(status, 400)


class HttpEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "server_journal.jsonl"
        self.server = serve("127.0.0.1", 0, JsonlEventStore(self.journal))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        conn.request(method, path, body=data,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_full_journey_over_http_with_journal_restart(self) -> None:
        t = T0
        iso = lambda dt: dt.isoformat()

        status, _ = self._request("POST", "/versions",
                                  {"version_ref": "V1", "at": iso(t)})
        self.assertEqual(status, 201)
        for code, role, party, days in (
                ("OBL-COL", "COLLECTION", "馆藏部", 10),
                ("OBL-INS", "INSURANCE", "保险经纪", 12),
                ("OBL-TRN", "TRANSPORT", "承运商", 14)):
            status, _ = self._request("POST", "/obligations", {
                "version_ref": "V1", "code": code, "role": role,
                "responsible_party": party,
                "deadline": iso(t + timedelta(days=days))})
            self.assertEqual(status, 201)
        status, _ = self._request("POST", "/crates", {
            "code": "CR-1", "version_ref": "V1",
            "gates": {"COLLECTION": "OBL-COL", "INSURANCE": "OBL-INS",
                      "TRANSPORT": "OBL-TRN"}})
        self.assertEqual(status, 201)

        # 未齐三签时放行被拒
        for code in ("OBL-COL", "OBL-INS", "OBL-TRN"):
            self.assertEqual(self._request("POST", f"/obligations/{code}/accept",
                                           {})[0], 200)
        status, body = self._request("POST", "/crates/CR-1/release", {})
        self.assertEqual(status, 400)
        self.assertIn("馆藏职责确认", body["error"])

        # 经 API 提交证据，提交人与复核人不同；再三职责签署
        for obl, ev, sub in (("OBL-COL", "EV-1", "馆藏员"),
                             ("OBL-INS", "EV-2", "保险经纪"),
                             ("OBL-TRN", "EV-3", "运输代理")):
            self.assertEqual(self._request("POST", f"/obligations/{obl}/evidence",
                                           {"evidence_ref": ev,
                                            "submitter": sub})[0], 201)
            status, body = self._request(
                "POST", f"/obligations/{obl}/evidence/review",
                {"evidence_ref": ev, "reviewer": "联络处 陈洁",
                 "accepted": True})
            self.assertEqual(status, 200, body)
        for role, signer in (("COLLECTION", "馆藏主管"),
                             ("INSURANCE", "保险主管"),
                             ("TRANSPORT", "运输主管")):
            self.assertEqual(
                self._request("POST", "/crates/CR-1/gates/sign",
                              {"role": role, "signer": signer})[0], 200)
        self.assertEqual(self._request("POST", "/crates/CR-1/release", {})[0], 200)
        self.assertEqual(self._request("POST", "/crates/CR-1/handovers", {
            "receiver": "联盟场馆组", "location": "展厅",
            "insurance_party": "联盟自保单元", "final": True})[0], 201)

        # 回执幂等
        ack = {"external_ref": "ACK-1", "sender": "秘书处",
               "content": {"ok": True}}
        self.assertEqual(self._request("POST", "/acks", ack)[0], 200)
        status, body = self._request("POST", "/acks", ack)
        self.assertEqual(status, 200)
        self.assertTrue(body["deduped"])

        # 重启服务：从 JSONL 完整回放，导出仍准确
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = serve("127.0.0.1", 0, JsonlEventStore(self.journal))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

        status, chain = self._request("GET", "/chain", None)
        self.assertEqual(status, 200)
        crate = chain["crates"][0]
        self.assertEqual(crate["status"], "DELIVERED")
        self.assertEqual(crate["current_custodian"], "联盟场馆组")
        self.assertEqual(crate["insurance_party"], "联盟自保单元")

        status, events = self._request("GET", "/events", None)
        self.assertEqual(status, 200)
        self.assertGreater(len(events["events"]), 10)

    def test_invalid_json_body_is_400(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/versions", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn("合法 JSON", json.loads(resp.read().decode("utf-8"))["error"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
