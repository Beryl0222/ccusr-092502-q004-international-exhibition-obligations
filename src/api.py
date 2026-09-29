"""HTTP/JSON API（仅标准库 http.server）。

所有写操作都 POST JSON；路径中的聚合标识既可以是内部 id，也可以是业务编号，
便于联调。命令拒绝统一返回 422 与中文原因，冲突返回 409。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse, parse_qs

from src.domain import DomainError
from src.eventstore import ConcurrencyError, EventStore
from src.service import ObligationService

ROLES = ("COLLECTION", "INSURANCE", "TRANSPORT")


def build_service(store_path: str | None = None) -> ObligationService:
    contract = _load_contract()
    return ObligationService(EventStore(store_path, set(contract["events"])))


def _contract_path() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[1] / "contracts" / "domain.json")


def _load_contract() -> dict[str, Any]:
    from pathlib import Path
    return json.loads(Path(_contract_path()).read_text(encoding="utf-8"))


def make_handler(service: ObligationService) -> type[BaseHTTPRequestHandler]:
    def resolve_obligation(key: str):
        return service.state.obligations.get(key) or service.state.obligation_by_code(key)

    def resolve_crate(key: str):
        return service.state.crates.get(key) or service.state.crate_by_code(key)

    def resolve_publication(key: str):
        return service.state.publications.get(key) or service.state.publication_by_code(key)

    def resolve_invitation(key: str):
        return service.state.invitations.get(key) or service.state.invitation_by_code(key)

    def resolve_agreement(key: str):
        return service.state.agreements.get(key) or service.state.agreement_by_ref(key)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ExhibitionChain/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DomainError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return value

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "events": len(service.store)})
                elif parsed.path == "/chain":
                    self._send(200, service.responsibility_chain())
                elif parsed.path == "/events":
                    self._send(200, {"events": service.store.all_events()})
                elif parsed.path == "/agreements":
                    self._send(200, {"items": [_agreement_dict(a)
                                               for a in service.state.agreements.values()]})
                elif parsed.path == "/obligations":
                    self._send(200, {"items": [_obligation_dict(service, o)
                                               for o in service.state.obligations.values()]})
                elif parsed.path == "/crates":
                    self._send(200, {"items": [_crate_dict(service, c)
                                               for c in service.state.crates.values()]})
                elif parsed.path == "/objects":
                    self._send(200, {"items": [_object_dict(o)
                                               for o in service.state.objects.values()]})
                elif parsed.path == "/publications":
                    self._send(200, {"items": [_publication_dict(service, p)
                                               for p in service.state.publications.values()]})
                elif parsed.path == "/invitations":
                    self._send(200, {"items": [_invitation_dict(i)
                                               for i in service.state.invitations.values()]})
                elif parsed.path == "/cases":
                    self._send(200, {"items": [_case_dict(c)
                                               for c in service.state.cases.values()]})
                elif parsed.path == "/acks":
                    self._send(200, {"items": [_ack_dict(a)
                                               for a in service.state.acks.values()]})
                else:
                    self._send(404, {"error": f"无此路径: {parsed.path}"})
            except Exception as exc:  # 读路径不应失败
                self._send(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.strip("/").split("/")
            query = parse_qs(parsed.query)
            try:
                body = self._read_json()
                with service.store.lock:
                    result = self._route(path, query, body)
                status, payload = result
                self._send(status, payload)
            except DomainError as exc:
                self._send(422, {"error": str(exc)})
            except ConcurrencyError as exc:
                self._send(409, {"error": str(exc)})
            except KeyError as exc:
                self._send(422, {"error": f"缺少字段: {exc.args[0]}"})

        def _route(self, path: list[str], query: dict[str, list[str]], body: dict[str, Any]):
            p = path

            if p == ["agreements"]:
                return 201, _event_response(service.draft_agreement(body))
            if len(p) == 3 and p[0] == "agreements":
                ag = resolve_agreement(p[1])
                if ag is None:
                    raise DomainError(f"协议版本不存在: {p[1]}")
                if p[2] == "activate":
                    return 200, _event_response(service.activate_agreement(ag.id))
                if p[2] == "supersede":
                    return 200, _event_response(service.supersede_agreement(ag.id, body))
                if p[2] == "cancel":
                    return 200, _event_response(service.cancel_agreement(ag.id, body))

            if p == ["obligations"]:
                return 201, _event_response(service.accept_obligation(body))
            if len(p) == 3 and p[0] == "obligations":
                ob = resolve_obligation(p[1])
                if ob is None:
                    raise DomainError(f"承诺不存在: {p[1]}")
                if p[2] == "evidence":
                    return 200, _events_response([service.sign_evidence(ob.id, body, alternative=False)])
                if p[2] == "alternative-evidence":
                    return 200, _events_response([service.sign_evidence(ob.id, body, alternative=True)])

            if p == ["admin", "sweep-expired"]:
                # now 既可放查询参数（需 URL 编码），也可放 JSON body
                now = query.get("now", [None])[0] or body.get("now")
                return 200, {"events": [e["event_type"] for e in service.sweep_expired(now=now)]}

            if p == ["objects"]:
                return 201, _event_response(service.list_object(body))

            if p == ["crates"]:
                return 201, _event_response(service.assemble_crate(body))
            if len(p) >= 3 and p[0] == "crates":
                crate = resolve_crate(p[1])
                if crate is None:
                    raise DomainError(f"箱件不存在: {p[1]}")
                action = p[2]
                if action == "objects":
                    return 200, _event_response(service.pack_object(crate.id, body))
                if action == "release":
                    return 200, _event_response(service.release_crate(crate.id, body))
                if action == "deliver":
                    return 200, _event_response(service.deliver_crate(crate.id, body))
                if action == "return":
                    return 200, _event_response(service.return_crate(crate.id, body))
                if action == "cancel":
                    return 200, _event_response(service.cancel_crate(crate.id, body))
                if action == "confirmations" and len(p) == 4:
                    role = p[3].upper()
                    if role not in ROLES:
                        raise DomainError(f"未知职责: {role}（应为 {', '.join(ROLES)}）")
                    return 200, _event_response(service.confirm_crate_role(crate.id, role, body))

            if p == ["invitations"]:
                return 201, _event_response(service.issue_invitation(body))
            if len(p) == 3 and p[0] == "invitations":
                inv = resolve_invitation(p[1])
                if inv is None:
                    raise DomainError(f"邀请不存在: {p[1]}")
                if p[2] == "acknowledge":
                    return 200, _event_response(service.acknowledge_invitation(inv.id, body))
                if p[2] == "reissue":
                    return 200, _event_response(service.reissue_invitation(inv.id, body))
                if p[2] == "cancel":
                    return 200, _event_response(service.cancel_invitation(inv.id, body))

            if p == ["publications"]:
                return 201, _event_response(service.draft_publication(body))
            if len(p) == 3 and p[0] == "publications":
                pub = resolve_publication(p[1])
                if pub is None:
                    raise DomainError(f"公布版本不存在: {p[1]}")
                if p[2] == "approve":
                    return 200, _event_response(service.approve_publication(pub.id, body))
                if p[2] == "publish":
                    return 200, _event_response(service.publish_publication(pub.id, body))
                if p[2] == "supersede":
                    return 200, _event_response(service.supersede_publication(pub.id, body))
                if p[2] == "cancel":
                    return 200, _event_response(service.cancel_publication(pub.id, body))

            if p == ["acks"]:
                return 202, {"received": [_ack_event_brief(e) for e in service.receive_ack(body)]}

            if len(p) == 3 and p[0] == "cases" and p[2] == "resolve":
                return 200, _event_response(service.resolve_case(p[1], body))

            if p == ["closure", "reconcile"]:
                return 200, _event_response(service.reconcile_closure(body))

            raise DomainError(f"无此路径或方法不支持: /{'/'.join(path)}")

    return Handler


def _event_response(event: dict[str, Any]) -> dict[str, Any]:
    return {"event": {"seq": event["seq"], "event_id": event["event_id"],
                      "event_type": event["event_type"], "aggregate_id": event["aggregate_id"],
                      "version": event["version"], "occurred_at": event["occurred_at"],
                      "payload": event["payload"]}}


def _events_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"events": [_event_response(e)["event"] for e in events]}


def _ack_event_brief(event: dict[str, Any]) -> dict[str, Any]:
    return {"event_type": event["event_type"], "aggregate_id": event["aggregate_id"], "seq": event["seq"]}


def _agreement_dict(a) -> dict[str, Any]:
    return {"id": a.id, "ref": a.ref, "title": a.title, "state": a.state,
            "superseded_by": a.superseded_by, "parties": a.parties}


def _obligation_dict(service: ObligationService, o) -> dict[str, Any]:
    return {"id": o.id, "code": o.code, "agreement_version_id": o.agreement_id,
            "state": service.state.obligation_state(o),
            "responsible_party_id": o.responsible_party_id, "deadline": o.deadline,
            "prerequisite_evidence_kinds": o.prerequisite_evidence_kinds,
            "missing_evidence_kinds": o.missing_kinds(),
            "acceptable_alternatives": o.acceptable_alternatives,
            "crate_codes": o.crate_codes, "publication_codes": o.publication_codes}


def _object_dict(o) -> dict[str, Any]:
    return {"id": o.id, "code": o.code, "title": o.title, "kind": o.kind,
            "state": o.state, "crate_id": o.crate_id, "rights_license_ref": o.rights_license_ref}


def _crate_dict(service: ObligationService, c) -> dict[str, Any]:
    return {"id": c.id, "code": c.code, "state": service.state.crate_status(c),
            "confirmations": list(c.confirmations.keys()),
            "roles_not_applicable": c.roles_not_applicable,
            "object_codes": [service.state.objects[i].code for i in c.object_ids],
            "current_location": c.current_location,
            "current_custodian": (
                service.state.current_custodian(c).custodian_party_id
                if service.state.current_custodian(c) else None)}


def _publication_dict(service: ObligationService, p) -> dict[str, Any]:
    return {"id": p.id, "code": p.code, "title": p.title, "state": p.state,
            "material_ref": p.material_ref, "published_ref": p.published_ref,
            "obligation_codes": p.obligation_codes}


def _invitation_dict(i) -> dict[str, Any]:
    return {"id": i.id, "code": i.code, "specialist_name": i.specialist_name,
            "state": i.state, "reissue_count": i.reissue_count}


def _case_dict(c) -> dict[str, Any]:
    return {"id": c.id, "ack_ref": c.ack_ref, "state": c.state, "ack_ids": c.ack_ids,
            "resolution": c.resolution, "chosen_ack_id": c.chosen_ack_id}


def _ack_dict(a) -> dict[str, Any]:
    return {"id": a.id, "ack_ref": a.ack_ref, "state": a.state,
            "content_hash": a.content_hash, "sender_party_id": a.sender_party_id,
            "case_id": a.case_id, "late_and_not_effective": a.late_and_not_effective,
            "revival_note": a.revival_note}


def serve(store_path: str | None, port: int = 8080, host: str = "127.0.0.1") -> None:
    service = build_service(store_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"国际联展交付责任链服务已启动: http://{host}:{port} (store={store_path or '内存'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
