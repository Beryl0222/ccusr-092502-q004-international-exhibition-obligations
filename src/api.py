"""仅依赖标准库的 JSON HTTP 适配层。

路由分发为纯函数 ``dispatch``，便于测试不经过套接字；``serve`` 启动
ThreadingHTTPServer 供联调使用。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .domain import (
    DomainError,
    ResponsibilityChainService,
    parse_time,
)
from .storage import EventStore, InMemoryEventStore, JsonlEventStore


# ---------------------------------------------------------------- 分发


def _require(body: dict[str, Any], key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise DomainError(f"缺少字段: {key}")
    return body[key]


def dispatch(service: ResponsibilityChainService, method: str, path: str,
             body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    body = body or {}
    parts = [p for p in path.strip("/").split("/") if p] if path != "/" else []

    try:
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}

        if method == "GET" and path == "/events":
            return 200, {"events": list(service.store.iter_events())}

        if method == "GET" and path == "/chain":
            return 200, service.export_chain()

        if method == "POST" and parts == ["maintenance", "sweep-expirations"]:
            at = parse_time(body["at"]) if body.get("at") else None
            return 200, {"newly_expired": service.sweep_expirations(at)}

        if method == "POST" and parts == ["versions"]:
            event = service.record_version(
                version_ref=_require(body, "version_ref"),
                title=body.get("title", ""),
                supersedes=body.get("supersedes"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if method == "POST" and len(parts) == 3 and parts[0] == "versions" and parts[2] == "cancel":
            event = service.cancel_version(
                version_ref=parts[1],
                reason=_require(body, "reason"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if method == "POST" and parts == ["obligations"]:
            event = service.define_obligation(
                version_ref=_require(body, "version_ref"),
                code=_require(body, "code"),
                responsible_party=_require(body, "responsible_party"),
                deadline=_require(body, "deadline"),
                title=body.get("title", ""),
                role=body.get("role"),
                prerequisite_codes=body.get("prerequisite_codes"),
                alternatives=body.get("alternatives"),
                linked_crates=body.get("linked_crates"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "obligations" and parts[2] == "accept"):
            event = service.accept_obligation(
                code=parts[1],
                confirming_ack_ref=body.get("confirming_ack_ref"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "obligations" and parts[2] == "evidence"):
            event = service.submit_evidence(
                obligation_code=parts[1],
                evidence_ref=body.get("evidence_ref") or f"ev-{uuid4().hex[:12]}",
                submitter=_require(body, "submitter"),
                kind=body.get("kind", ""),
                alternative_code=body.get("alternative_code"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if (method == "POST" and len(parts) == 4
                and parts[0] == "obligations" and parts[2] == "evidence"
                and parts[3] == "review"):
            # 路径形式 /obligations/{code}/evidence/review，证据号在正文
            event = service.review_evidence(
                obligation_code=parts[1],
                evidence_ref=_require(body, "evidence_ref"),
                reviewer=_require(body, "reviewer"),
                accepted=bool(_require(body, "accepted")),
                note=body.get("note"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if method == "POST" and parts == ["crates"]:
            event = service.list_crate(
                code=_require(body, "code"),
                version_ref=_require(body, "version_ref"),
                gates=_require(body, "gates"),
                description=body.get("description", ""),
                origin_custodian=body.get("origin_custodian"),
                origin_location=body.get("origin_location"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "crates" and parts[2] == "link"):
            event = service.link_crate(
                crate_code=parts[1],
                obligation_code=_require(body, "obligation_code"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if (method == "POST" and len(parts) == 4
                and parts[0] == "crates" and parts[2] == "gates"
                and parts[3] == "sign"):
            event = service.sign_gate(
                crate_code=parts[1],
                role=_require(body, "role"),
                signer=_require(body, "signer"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "crates" and parts[2] == "release"):
            event = service.release_crate(
                crate_code=parts[1],
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "crates" and parts[2] == "handovers"):
            event = service.hand_over(
                crate_code=parts[1],
                receiver=_require(body, "receiver"),
                location=_require(body, "location"),
                insurance_party=body.get("insurance_party"),
                final=bool(body.get("final", False)),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if method == "POST" and parts == ["acks"]:
            result = service.receive_ack(
                external_ref=_require(body, "external_ref"),
                sender=_require(body, "sender"),
                content=_require(body, "content"),
                declared_version_ref=body.get("declared_version_ref"),
                received_at=body.get("received_at"),
                arrival_at=parse_time(body["arrival_at"]) if body.get("arrival_at") else None,
            )
            return 200, result

        if (method == "POST" and len(parts) == 3
                and parts[0] == "acks" and parts[2] == "resolve"):
            event = service.resolve_ack(
                external_ref=parts[1],
                resolution=_require(body, "resolution"),
                coordinator=_require(body, "coordinator"),
                content=body.get("content"),
                note=body.get("note"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        if method == "POST" and parts == ["publications"]:
            event = service.approve_publication(
                version_ref=_require(body, "version_ref"),
                material_ref=_require(body, "material_ref"),
                channel=body.get("channel", ""),
                obligation_codes=body.get("obligation_codes"),
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 201, {"event": event}

        if (method == "POST" and len(parts) == 3
                and parts[0] == "versions" and parts[2] == "closure"):
            event = service.reconcile_closure(
                version_ref=parts[1],
                at=parse_time(body["at"]) if body.get("at") else None,
            )
            return 200, {"event": event}

        return 404, {"error": f"无此路由: {method} {path}"}
    except DomainError as exc:
        return 400, {"error": str(exc)}


# ---------------------------------------------------------------- HTTP 包装


def build_service(store: EventStore | None = None,
                  clock: Any = None) -> ResponsibilityChainService:
    # 注意：不能用 `store or ...`——空的 JsonlEventStore 因 __len__==0 为假值，
    # 会被错误替换成内存存储导致事件不落盘。
    if store is None:
        store = InMemoryEventStore()
    return ResponsibilityChainService(store, clock or _clock)


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def make_handler(service: ResponsibilityChainService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ObligationChain/0.2"

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._route(None)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            if not raw:
                body: dict[str, Any] = {}
            else:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._write(400, {"error": f"请求体不是合法 JSON: {exc}"})
                    return
                if not isinstance(parsed, dict):
                    self._write(400, {"error": "请求体必须是 JSON 对象"})
                    return
                body = parsed
            self._route(body)

        def _route(self, body: dict[str, Any] | None) -> None:
            path = urlparse(self.path).path
            try:
                status, payload = dispatch(service, self.command, path, body)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self._write(500, {"error": f"内部错误: {exc}"})
                return
            self._write(status, payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            # 联调时保持安静，避免污染容器日志。
            return

    return Handler


def serve(host: str, port: int, store: EventStore | None = None) -> ThreadingHTTPServer:
    service = build_service(store)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    return httpd


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import os

    parser = argparse.ArgumentParser(description="国际联展交付责任链服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--journal", default=os.environ.get("JOURNAL", ""),
                        help="JSONL 事件日志路径；留空则仅使用内存存储")
    args = parser.parse_args()

    event_store = JsonlEventStore(args.journal) if args.journal else InMemoryEventStore()
    server = serve(args.host, args.port, event_store)
    print(f"责任链服务监听 http://{args.host}:{args.port}"
          f"（日志: {args.journal or '内存'}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
