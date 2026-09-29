"""国际联展交付责任链核心领域。

设计要点：
- 所有业务事实以追加事件表达，事件一经记录不得原地改写；
- 跨时区消息只按事件携带的业务时间裁决，到达顺序不决定内容真假；
- 承诺带责任方、期限、前置证据与可接受替代方案；
- 箱件离馆须经馆藏、保险、运输三职责分别确认，证据提交人不得复核自己的证据；
- 任一承诺过期只暂停与其关联的箱件和公布版本，不波及其他箱件；
- 取消、冲突与交接全程留痕，实体位置、保险责任与公布记录在导出中仍可追溯。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from .envelope import validate_event

# ---------------------------------------------------------------- 事件目录

AGREEMENT_VERSION_RECORDED = "AGREEMENT_VERSION_RECORDED"
AGREEMENT_VERSION_SUPERSEDED = "AGREEMENT_VERSION_SUPERSEDED"
AGREEMENT_VERSION_CANCELLED = "AGREEMENT_VERSION_CANCELLED"

OBLIGATION_DEFINED = "OBLIGATION_DEFINED"
OBLIGATION_ACCEPTED = "OBLIGATION_ACCEPTED"
OBLIGATION_EXPIRED = "OBLIGATION_EXPIRED"

EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"
EVIDENCE_REVIEWED = "EVIDENCE_REVIEWED"

CRATE_LISTED = "CRATE_LISTED"
CRATE_LINKED = "CRATE_LINKED"
CRATE_GATE_SIGNED = "CRATE_GATE_SIGNED"
CRATE_RELEASED = "CRATE_RELEASED"
CRATE_HANDED_OVER = "CRATE_HANDED_OVER"
CRATE_SUSPENDED = "CRATE_SUSPENDED"
CRATE_RESUMED = "CRATE_RESUMED"

ACK_RECEIVED = "ACK_RECEIVED"
ACK_CONFLICT_FLAGGED = "ACK_CONFLICT_FLAGGED"
ACK_RESOLVED = "ACK_RESOLVED"

PUBLICATION_APPROVED = "PUBLICATION_APPROVED"
PUBLICATION_SUSPENDED = "PUBLICATION_SUSPENDED"
PUBLICATION_RESUMED = "PUBLICATION_RESUMED"

CLOSURE_RECONCILED = "CLOSURE_RECONCILED"

ALLOWED_EVENTS: set[str] = {
    AGREEMENT_VERSION_RECORDED,
    AGREEMENT_VERSION_SUPERSEDED,
    AGREEMENT_VERSION_CANCELLED,
    OBLIGATION_DEFINED,
    OBLIGATION_ACCEPTED,
    OBLIGATION_EXPIRED,
    EVIDENCE_SUBMITTED,
    EVIDENCE_REVIEWED,
    CRATE_LISTED,
    CRATE_LINKED,
    CRATE_GATE_SIGNED,
    CRATE_RELEASED,
    CRATE_HANDED_OVER,
    CRATE_SUSPENDED,
    CRATE_RESUMED,
    ACK_RECEIVED,
    ACK_CONFLICT_FLAGGED,
    ACK_RESOLVED,
    PUBLICATION_APPROVED,
    PUBLICATION_SUSPENDED,
    PUBLICATION_RESUMED,
    CLOSURE_RECONCILED,
}

COLLECTION = "COLLECTION"
INSURANCE = "INSURANCE"
TRANSPORT = "TRANSPORT"
RELEASE_ROLES = (COLLECTION, INSURANCE, TRANSPORT)
ROLE_LABELS = {
    COLLECTION: "馆藏",
    INSURANCE: "保险",
    TRANSPORT: "运输",
}

VERSION_RECORDED = "RECORDED"
VERSION_SUPERSEDED = "SUPERSEDED"
VERSION_CANCELLED = "CANCELLED"

ACK_RECEIVED_STATE = "RECEIVED"
ACK_CONFLICT_STATE = "CONFLICT"
ACK_RESOLVED_STATE = "RESOLVED"

SUSPEND_OBLIGATION_EXPIRED = "OBLIGATION_EXPIRED"
SUSPEND_VERSION_CANCELLED = "VERSION_CANCELLED"


class DomainError(ValueError):
    """业务规则被违反；调用方收到后应以 4xx 回应，不得吞掉。"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise DomainError(f"时间格式无效: {value}") from exc
    if parsed.tzinfo is None:
        raise DomainError("时间必须携带时区")
    return parsed


def canonical_content(content: Any) -> str:
    """外部回执正文的规范化形式，用于按内容判重与冲突识别。"""
    import json

    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 只读投影


@dataclass
class VersionView:
    ref: str
    title: str
    recorded_at: datetime
    status: str = VERSION_RECORDED
    superseded_by: str | None = None
    superseded_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None

    @property
    def effective(self) -> bool:
        return self.status == VERSION_RECORDED


@dataclass
class EvidenceView:
    evidence_ref: str
    obligation_code: str
    submitter: str
    kind: str
    submitted_at: datetime
    alternative_code: str | None
    state: str = "SUBMITTED"  # SUBMITTED / ACCEPTED / REJECTED
    reviewer: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None
    accepted: bool = False


@dataclass
class ObligationView:
    code: str
    version_ref: str
    title: str
    responsible_party: str
    role: str | None
    deadline: datetime
    prerequisite_codes: list[str]
    alternatives: list[dict[str, str]]
    linked_crates: list[str] = field(default_factory=list)
    accepted_at: datetime | None = None
    confirming_ack_ref: str | None = None
    expired_at: datetime | None = None
    fulfilled_at: datetime | None = None
    fulfilled_via_alternative: str | None = None
    evidence: OrderedDict[str, EvidenceView] = field(default_factory=OrderedDict)

    @property
    def state(self) -> str:
        if self.fulfilled_at is not None:
            return (
                "FULFILLED_ALTERNATIVE"
                if self.fulfilled_via_alternative
                else "FULFILLED"
            )
        if self.expired_at is not None:
            return "EXPIRED"
        if self.accepted_at is not None:
            return "ACCEPTED"
        return "DEFINED"

    @property
    def is_fulfilled(self) -> bool:
        return self.fulfilled_at is not None


@dataclass
class GateSignView:
    role: str
    signer: str
    obligation_code: str
    signed_at: datetime


@dataclass
class HandoverView:
    receiver: str
    location: str
    insurance_party: str | None
    final: bool
    at: datetime


@dataclass
class CrateView:
    code: str
    version_ref: str
    description: str
    gates: dict[str, str]  # 职责 -> 承诺编号
    origin_custodian: str
    origin_location: str
    listed_at: datetime
    linked_obligations: set[str] = field(default_factory=set)
    signs: dict[str, GateSignView] = field(default_factory=dict)
    released_at: datetime | None = None
    handovers: list[HandoverView] = field(default_factory=list)
    # 暂停以 (原因, 关联键) 为主键，关联键为承诺编号或版本号；
    # 同箱件上多个承诺过期时各自独立记账，互不覆盖。
    suspension_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    resumes: set[tuple[str, str]] = field(default_factory=set)

    @property
    def active_suspensions(self) -> dict[tuple[str, str], str]:
        return {
            key: detail
            for key, detail in self.suspension_reasons.items()
            if key not in self.resumes
        }

    @property
    def suspended(self) -> bool:
        return bool(self.active_suspensions)

    @property
    def all_gates_signed(self) -> bool:
        return all(role in self.signs for role in RELEASE_ROLES)

    @property
    def status(self) -> str:
        if self.handovers and self.handovers[-1].final:
            return "DELIVERED"
        # 版本取消且从未放行：实体不出馆，箱件整体作废，但全部记录保留可追溯。
        cancel_version = (SUSPEND_VERSION_CANCELLED, self.version_ref)
        if (
            self.released_at is None
            and cancel_version in self.suspension_reasons
            and cancel_version not in self.resumes
        ):
            return "CANCELLED"
        if self.suspended:
            return "SUSPENDED"
        if self.released_at is not None:
            return "IN_TRANSIT" if self.handovers else "RELEASED"
        if self.all_gates_signed:
            return "READY"
        return "LISTED"

    @property
    def current_custodian(self) -> str:
        if self.handovers:
            return self.handovers[-1].receiver
        return self.origin_custodian

    @property
    def current_location(self) -> str:
        if self.handovers:
            return self.handovers[-1].location
        return self.origin_location


@dataclass
class AckView:
    aggregate_id: str
    external_ref: str
    first_sender: str
    first_content: Any
    first_content_hash: str
    first_received_at: datetime
    arrival_at: datetime
    declared_version_ref: str | None
    contenders: list[dict[str, Any]] = field(default_factory=list)
    conflict: bool = False
    resolution: str | None = None  # FIRST / LATEST / MANUAL
    resolved_content: Any = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    note: str | None = None

    @property
    def state(self) -> str:
        if self.resolved_at is not None:
            return ACK_RESOLVED_STATE
        if self.conflict:
            return ACK_CONFLICT_STATE
        return ACK_RECEIVED_STATE

    @property
    def effective_content(self) -> Any:
        if self.resolved_at is not None:
            return self.resolved_content
        return self.first_content


@dataclass
class PublicationView:
    aggregate_id: str
    version_ref: str
    material_ref: str
    channel: str
    obligation_codes: list[str]
    approved_at: datetime
    suspension_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    resumes: set[tuple[str, str]] = field(default_factory=set)

    @property
    def active_suspensions(self) -> dict[tuple[str, str], str]:
        return {
            key: detail
            for key, detail in self.suspension_reasons.items()
            if key not in self.resumes
        }

    @property
    def suspended(self) -> bool:
        return bool(self.active_suspensions)


@dataclass
class Projection:
    versions: OrderedDict[str, VersionView] = field(default_factory=OrderedDict)
    obligations: OrderedDict[str, ObligationView] = field(default_factory=OrderedDict)
    crates: OrderedDict[str, CrateView] = field(default_factory=OrderedDict)
    acks_by_ref: OrderedDict[str, AckView] = field(default_factory=OrderedDict)
    publications: list[PublicationView] = field(default_factory=list)
    closures: list[dict[str, Any]] = field(default_factory=list)


def build_projection(events: Iterable[dict[str, Any]]) -> Projection:
    """从事件流纯函数式重建当前投影，事件本身永不被修改。"""
    p = Projection()

    for e in events:
        etype = e["event_type"]
        payload = e["payload"]
        at = parse_time(e["occurred_at"])

        if etype == AGREEMENT_VERSION_RECORDED:
            p.versions[payload["version_ref"]] = VersionView(
                ref=payload["version_ref"],
                title=payload.get("title", ""),
                recorded_at=at,
            )
        elif etype == AGREEMENT_VERSION_SUPERSEDED:
            v = p.versions[payload["version_ref"]]
            v.status = VERSION_SUPERSEDED
            v.superseded_by = payload["superseded_by"]
            v.superseded_at = at
        elif etype == AGREEMENT_VERSION_CANCELLED:
            v = p.versions[payload["version_ref"]]
            v.status = VERSION_CANCELLED
            v.cancelled_at = at
            v.cancel_reason = payload.get("reason")

        elif etype == OBLIGATION_DEFINED:
            p.obligations[payload["code"]] = ObligationView(
                code=payload["code"],
                version_ref=payload["version_ref"],
                title=payload.get("title", ""),
                responsible_party=payload["responsible_party"],
                role=payload.get("role"),
                deadline=parse_time(payload["deadline"]),
                prerequisite_codes=list(payload.get("prerequisite_codes", [])),
                alternatives=list(payload.get("alternatives", [])),
                linked_crates=list(payload.get("linked_crates", [])),
            )
        elif etype == OBLIGATION_ACCEPTED:
            o = p.obligations[payload["code"]]
            o.accepted_at = at
            o.confirming_ack_ref = payload.get("confirming_ack_ref")
        elif etype == OBLIGATION_EXPIRED:
            p.obligations[payload["code"]].expired_at = at

        elif etype == EVIDENCE_SUBMITTED:
            o = p.obligations[payload["obligation_code"]]
            o.evidence[payload["evidence_ref"]] = EvidenceView(
                evidence_ref=payload["evidence_ref"],
                obligation_code=o.code,
                submitter=payload["submitter"],
                kind=payload.get("kind", ""),
                submitted_at=at,
                alternative_code=payload.get("alternative_code"),
            )
        elif etype == EVIDENCE_REVIEWED:
            o = p.obligations[payload["obligation_code"]]
            ev = o.evidence[payload["evidence_ref"]]
            ev.state = "ACCEPTED" if payload["accepted"] else "REJECTED"
            ev.reviewer = payload["reviewer"]
            ev.review_note = payload.get("note")
            ev.reviewed_at = at
            ev.accepted = bool(payload["accepted"])
            if payload["accepted"] and payload.get("satisfies"):
                o.fulfilled_at = at
                o.fulfilled_via_alternative = payload.get("via_alternative")

        elif etype == CRATE_LISTED:
            p.crates[payload["code"]] = CrateView(
                code=payload["code"],
                version_ref=payload["version_ref"],
                description=payload.get("description", ""),
                gates=dict(payload["gates"]),
                origin_custodian=payload.get("origin_custodian", "借出机构馆藏处"),
                origin_location=payload.get("origin_location", "借出机构库房"),
                listed_at=at,
                linked_obligations=set(dict(payload["gates"]).values()),
            )
        elif etype == CRATE_LINKED:
            p.crates[payload["crate_code"]].linked_obligations.add(
                payload["obligation_code"]
            )
        elif etype == CRATE_GATE_SIGNED:
            c = p.crates[payload["crate_code"]]
            c.signs[payload["role"]] = GateSignView(
                role=payload["role"],
                signer=payload["signer"],
                obligation_code=payload["obligation_code"],
                signed_at=at,
            )
        elif etype == CRATE_RELEASED:
            p.crates[payload["crate_code"]].released_at = at
        elif etype == CRATE_HANDED_OVER:
            c = p.crates[payload["crate_code"]]
            c.handovers.append(
                HandoverView(
                    receiver=payload["receiver"],
                    location=payload["location"],
                    insurance_party=payload.get("insurance_party"),
                    final=bool(payload.get("final", False)),
                    at=at,
                )
            )
        elif etype == CRATE_SUSPENDED:
            c = p.crates[payload["crate_code"]]
            c.suspension_reasons[
                (payload["reason"], payload.get("reason_key", ""))
            ] = payload.get("detail", "")
        elif etype == CRATE_RESUMED:
            c = p.crates[payload["crate_code"]]
            c.resumes.add((payload["reason"], payload.get("reason_key", "")))

        elif etype == ACK_RECEIVED:
            p.acks_by_ref[payload["external_ref"]] = AckView(
                aggregate_id=e["aggregate_id"],
                external_ref=payload["external_ref"],
                first_sender=payload["sender"],
                first_content=payload["content"],
                first_content_hash=payload["content_hash"],
                first_received_at=parse_time(payload["received_at"]),
                arrival_at=parse_time(payload["arrival_at"]),
                declared_version_ref=payload.get("declared_version_ref"),
            )
        elif etype == ACK_CONFLICT_FLAGGED:
            a = p.acks_by_ref[payload["external_ref"]]
            a.conflict = True
            a.contenders.append(
                {
                    "sender": payload["sender"],
                    "content": payload["content"],
                    "content_hash": payload["content_hash"],
                    "received_at": payload["received_at"],
                    "arrival_at": payload["arrival_at"],
                }
            )
        elif etype == ACK_RESOLVED:
            a = p.acks_by_ref[payload["external_ref"]]
            a.resolution = payload["resolution"]
            a.resolved_content = payload["content"]
            a.resolved_by = payload["coordinator"]
            a.resolved_at = at
            a.note = payload.get("note")

        elif etype == PUBLICATION_APPROVED:
            p.publications.append(
                PublicationView(
                    aggregate_id=e["aggregate_id"],
                    version_ref=payload["version_ref"],
                    material_ref=payload["material_ref"],
                    channel=payload.get("channel", ""),
                    obligation_codes=list(payload.get("obligation_codes", [])),
                    approved_at=at,
                )
            )
        elif etype == PUBLICATION_SUSPENDED:
            pub = next(
                x for x in p.publications if x.aggregate_id == e["aggregate_id"]
            )
            pub.suspension_reasons[
                (payload["reason"], payload.get("reason_key", ""))
            ] = payload.get("detail", "")
        elif etype == PUBLICATION_RESUMED:
            pub = next(
                x for x in p.publications if x.aggregate_id == e["aggregate_id"]
            )
            pub.resumes.add((payload["reason"], payload.get("reason_key", "")))

        elif etype == CLOSURE_RECONCILED:
            p.closures.append({"version_ref": payload["version_ref"], "at": at})

    return p


# ---------------------------------------------------------------- 领域服务


class ResponsibilityChainService:
    """在 append-only 事件存储上执行责任链命令。"""

    def __init__(self, store: Any, clock: Any = now_utc) -> None:
        self.store = store
        self.clock = clock

    # ---- 内部工具 ----

    def _projection(self) -> Projection:
        return build_projection(self.store.iter_events())

    def _append(self, event_type: str, aggregate_id: str, payload: dict[str, Any],
                at: datetime | None = None) -> dict[str, Any]:
        event = {
            "event_id": f"evt-{uuid4().hex}",
            "event_type": event_type,
            "occurred_at": (at or self.clock()).isoformat(),
            "aggregate_id": aggregate_id,
            "version": self.store.next_version(aggregate_id),
            "payload": payload,
        }
        errors = validate_event(event, ALLOWED_EVENTS)
        if errors:
            raise DomainError("; ".join(errors))
        self.store.append(event)
        return event

    def _get_version(self, p: Projection, ref: str) -> VersionView:
        v = p.versions.get(ref)
        if v is None:
            raise DomainError(f"协议版本不存在: {ref}")
        return v

    def _require_effective_version(self, p: Projection, ref: str) -> VersionView:
        v = self._get_version(p, ref)
        if v.status == VERSION_SUPERSEDED:
            raise DomainError(f"协议版本 {ref} 已被 {v.superseded_by} 取代，不得再产生承诺")
        if v.status == VERSION_CANCELLED:
            raise DomainError(f"协议版本 {ref} 已取消")
        return v

    def _get_obligation(self, p: Projection, code: str) -> ObligationView:
        o = p.obligations.get(code)
        if o is None:
            raise DomainError(f"承诺不存在: {code}")
        return o

    def _sweep_expirations(self, now: datetime) -> Projection:
        """过期只追加事件并暂停关联对象；投影重建后返回。"""
        p = self._projection()
        for o in list(p.obligations.values()):
            if o.state != "ACCEPTED":
                continue
            if o.deadline >= now:
                continue
            self._append(
                OBLIGATION_EXPIRED,
                f"obligation-{o.code}",
                {"code": o.code, "version_ref": o.version_ref, "deadline": o.deadline.isoformat()},
                at=now,
            )
            for crate_code in self._crates_linked_to(p, o.code):
                self._suspend_crate(
                    crate_code, SUSPEND_OBLIGATION_EXPIRED, o.code, now
                )
            for pub in self._publications_linked_to(p, o.code):
                self._suspend_publication(
                    pub.aggregate_id, SUSPEND_OBLIGATION_EXPIRED, o.code, now
                )
        return self._projection()

    def _crates_linked_to(self, p: Projection, obligation_code: str) -> list[str]:
        return [
            c.code
            for c in p.crates.values()
            if obligation_code in c.linked_obligations
            and c.status not in ("DELIVERED", "CANCELLED")
        ]

    def _publications_linked_to(
        self, p: Projection, obligation_code: str
    ) -> list[PublicationView]:
        return [
            pub
            for pub in p.publications
            if obligation_code in pub.obligation_codes and not _publication_dead(pub, p)
        ]

    def _suspend_crate(self, crate_code: str, reason: str, reason_key: str,
                       at: datetime, detail: str = "") -> None:
        p = self._projection()
        c = p.crates.get(crate_code)
        if c is None:
            return
        key = (reason, reason_key)
        if key in c.suspension_reasons:
            return
        self._append(
            CRATE_SUSPENDED,
            f"crate-{crate_code}",
            {"crate_code": crate_code, "reason": reason,
             "reason_key": reason_key, "detail": detail},
            at=at,
        )

    def _suspend_publication(self, aggregate_id: str, reason: str, reason_key: str,
                             at: datetime, detail: str = "") -> None:
        p = self._projection()
        pub = next((x for x in p.publications if x.aggregate_id == aggregate_id), None)
        if pub is None or (reason, reason_key) in pub.suspension_reasons:
            return
        self._append(
            PUBLICATION_SUSPENDED,
            aggregate_id,
            {"reason": reason, "reason_key": reason_key, "detail": detail},
            at=at,
        )

    def _resume_for_obligation(self, obligation_code: str, at: datetime) -> None:
        p = self._projection()
        key = (SUSPEND_OBLIGATION_EXPIRED, obligation_code)
        for c in p.crates.values():
            if key in c.active_suspensions:
                self._append(
                    CRATE_RESUMED,
                    f"crate-{c.code}",
                    {
                        "crate_code": c.code,
                        "reason": key[0],
                        "reason_key": key[1],
                    },
                    at=at,
                )
        for pub in p.publications:
            if key in pub.active_suspensions:
                self._append(
                    PUBLICATION_RESUMED,
                    pub.aggregate_id,
                    {"reason": key[0], "reason_key": key[1]},
                    at=at,
                )

    # ---- 协议版本 ----

    def record_version(self, version_ref: str, title: str = "",
                       supersedes: str | None = None,
                       at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._projection()
        if version_ref in p.versions:
            raise DomainError(f"协议版本已存在: {version_ref}")
        event = self._append(
            AGREEMENT_VERSION_RECORDED,
            f"agreement_version-{version_ref}",
            {"version_ref": version_ref, "title": title},
            at=at,
        )
        # 登记新版本并不自动复活任何旧版本；显式取代只让旧版本失效。
        if supersedes is not None:
            old = self._get_version(self._projection(), supersedes)
            if old.status != VERSION_RECORDED:
                raise DomainError(
                    f"被取代版本 {supersedes} 当前状态 {old.status}，不可再被取代"
                )
            self._append(
                AGREEMENT_VERSION_SUPERSEDED,
                f"agreement_version-{supersedes}",
                {"version_ref": supersedes, "superseded_by": version_ref},
                at=at,
            )
        return event

    def cancel_version(self, version_ref: str, reason: str,
                       at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        v = self._require_effective_version(p, version_ref)
        event = self._append(
            AGREEMENT_VERSION_CANCELLED,
            f"agreement_version-{version_ref}",
            {"version_ref": version_ref, "reason": reason},
            at=at,
        )
        # 取消不删除任何记录：在途/在库箱件与公布版本只追加暂停，交接与位置历史保留。
        for c in p.crates.values():
            if c.version_ref != version_ref:
                continue
            if c.status in ("DELIVERED", "CANCELLED"):
                continue
            self._suspend_crate(c.code, SUSPEND_VERSION_CANCELLED, version_ref, at)
        for pub in p.publications:
            if pub.version_ref == version_ref:
                self._suspend_publication(
                    pub.aggregate_id, SUSPEND_VERSION_CANCELLED, version_ref, at
                )
        return event

    # ---- 承诺 ----

    def define_obligation(self, version_ref: str, code: str, responsible_party: str,
                          deadline: str | datetime, *, title: str = "",
                          role: str | None = None,
                          prerequisite_codes: list[str] | None = None,
                          alternatives: list[dict[str, str]] | None = None,
                          linked_crates: list[str] | None = None,
                          at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._projection()
        self._require_effective_version(p, version_ref)
        if code in p.obligations:
            raise DomainError(f"承诺编号已存在: {code}")
        if role is not None and role not in RELEASE_ROLES:
            raise DomainError(f"未知职责: {role}")
        deadline_dt = parse_time(deadline)
        for alt in alternatives or []:
            if "code" not in alt or "description" not in alt:
                raise DomainError("替代方案必须包含 code 与 description")
        return self._append(
            OBLIGATION_DEFINED,
            f"obligation-{code}",
            {
                "version_ref": version_ref,
                "code": code,
                "title": title,
                "responsible_party": responsible_party,
                "role": role,
                "deadline": deadline_dt.isoformat(),
                "prerequisite_codes": list(prerequisite_codes or []),
                "alternatives": list(alternatives or []),
                "linked_crates": list(linked_crates or []),
            },
            at=at,
        )

    def accept_obligation(self, code: str, *, confirming_ack_ref: str | None = None,
                          at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        o = self._get_obligation(p, code)
        self._require_effective_version(p, o.version_ref)
        if o.accepted_at is not None:
            raise DomainError(f"承诺 {code} 已被接受")
        if o.deadline < at:
            raise DomainError(f"承诺 {code} 已过期限，不得按原条款接受")
        for prereq_code in o.prerequisite_codes:
            prereq = self._get_obligation(p, prereq_code)
            if prereq.version_ref != o.version_ref:
                raise DomainError(
                    f"前置承诺 {prereq_code} 属于版本 {prereq.version_ref}，"
                    f"不能作为版本 {o.version_ref} 的前置"
                )
            if not prereq.is_fulfilled:
                raise DomainError(
                    f"前置证据未满足: {prereq_code} 当前状态 {prereq.state}"
                )
        if confirming_ack_ref is not None:
            ack = p.acks_by_ref.get(confirming_ack_ref)
            if ack is None:
                raise DomainError(f"回执不存在: {confirming_ack_ref}")
            if ack.state == ACK_CONFLICT_STATE:
                raise DomainError("回执内容冲突待人工协调，不能作为承诺接受依据")
            if (
                ack.declared_version_ref is not None
                and ack.declared_version_ref != o.version_ref
            ):
                raise DomainError(
                    f"回执针对版本 {ack.declared_version_ref}，"
                    f"不能用于承诺所在版本 {o.version_ref}；迟到确认不得复活失效版本"
                )
        return self._append(
            OBLIGATION_ACCEPTED,
            f"obligation-{code}",
            {"code": code, "confirming_ack_ref": confirming_ack_ref},
            at=at,
        )

    def submit_evidence(self, obligation_code: str, evidence_ref: str,
                        submitter: str, *, kind: str = "",
                        alternative_code: str | None = None,
                        at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        o = self._get_obligation(p, obligation_code)
        self._require_effective_version(p, o.version_ref)
        if o.state == "DEFINED":
            raise DomainError(f"承诺 {obligation_code} 尚未被对方接受，不能提交证据")
        if o.is_fulfilled:
            raise DomainError(f"承诺 {obligation_code} 已满足")
        if any(ev.evidence_ref == evidence_ref for ev in o.evidence.values()):
            raise DomainError(f"证据编号已存在: {evidence_ref}")
        if not submitter or not submitter.strip():
            raise DomainError("提交人不能为空")
        if o.state == "EXPIRED":
            alt_codes = {a["code"] for a in o.alternatives}
            if alternative_code not in alt_codes:
                raise DomainError(
                    f"承诺 {obligation_code} 已过期；仅可按登记的替代方案履行: "
                    f"{sorted(alt_codes)}"
                )
        elif alternative_code is not None:
            raise DomainError("承诺未过期时不得直接使用替代方案")
        return self._append(
            EVIDENCE_SUBMITTED,
            f"obligation-{obligation_code}",
            {
                "obligation_code": obligation_code,
                "evidence_ref": evidence_ref,
                "submitter": submitter,
                "kind": kind,
                "alternative_code": alternative_code,
            },
            at=at,
        )

    def review_evidence(self, obligation_code: str, evidence_ref: str,
                        reviewer: str, accepted: bool, *, note: str | None = None,
                        at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        o = self._get_obligation(p, obligation_code)
        ev = o.evidence.get(evidence_ref)
        if ev is None:
            raise DomainError(f"证据不存在: {evidence_ref}")
        if ev.state != "SUBMITTED":
            raise DomainError(f"证据 {evidence_ref} 已复核")
        if not reviewer or not reviewer.strip():
            raise DomainError("复核人不能为空")
        # 职责分离：提交人不得复核自己提交的证据。
        if reviewer.strip() == ev.submitter.strip():
            raise DomainError("提交人不得复核自己的证据")
        via_alternative = ev.alternative_code if ev.alternative_code else None
        event = self._append(
            EVIDENCE_REVIEWED,
            f"obligation-{obligation_code}",
            {
                "obligation_code": obligation_code,
                "evidence_ref": evidence_ref,
                "reviewer": reviewer,
                "accepted": accepted,
                "satisfies": accepted,
                "via_alternative": via_alternative,
                "note": note,
            },
            at=at,
        )
        if accepted:
            # 承诺满足（含按替代方案满足）后，仅解除与其关联的暂停。
            self._resume_for_obligation(obligation_code, at)
        return event

    # ---- 箱件与放行 ----

    def list_crate(self, code: str, version_ref: str, gates: dict[str, str], *,
                   description: str = "", origin_custodian: str | None = None,
                   origin_location: str | None = None,
                   at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._projection()
        self._require_effective_version(p, version_ref)
        if code in p.crates:
            raise DomainError(f"箱件已存在: {code}")
        if set(gates) != set(RELEASE_ROLES):
            raise DomainError(
                f"箱件必须登记馆藏/保险/运输三个职责承诺，缺少: "
                f"{sorted(set(RELEASE_ROLES) - set(gates))}"
            )
        for role, obl_code in gates.items():
            o = self._get_obligation(p, obl_code)
            if o.version_ref != version_ref:
                raise DomainError(f"承诺 {obl_code} 不属于版本 {version_ref}")
            if o.role != role:
                raise DomainError(
                    f"承诺 {obl_code} 的职责是 {o.role}，不能作为 {role} 闸门"
                )
        payload = {
            "code": code,
            "version_ref": version_ref,
            "description": description,
            "gates": dict(gates),
        }
        if origin_custodian is not None:
            payload["origin_custodian"] = origin_custodian
        if origin_location is not None:
            payload["origin_location"] = origin_location
        event = self._append(CRATE_LISTED, f"crate-{code}", payload, at=at)
        for obl_code in gates.values():
            self._append(
                CRATE_LINKED,
                f"crate-{code}",
                {"crate_code": code, "obligation_code": obl_code},
                at=at,
            )
        return event

    def link_crate(self, crate_code: str, obligation_code: str,
                   at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._projection()
        c = p.crates.get(crate_code)
        if c is None:
            raise DomainError(f"箱件不存在: {crate_code}")
        self._get_obligation(p, obligation_code)
        if obligation_code in c.linked_obligations:
            raise DomainError("箱件已关联该承诺")
        return self._append(
            CRATE_LINKED,
            f"crate-{crate_code}",
            {"crate_code": crate_code, "obligation_code": obligation_code},
            at=at,
        )

    def sign_gate(self, crate_code: str, role: str, signer: str, *,
                  at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        if role not in RELEASE_ROLES:
            raise DomainError(f"未知职责: {role}")
        c = p.crates.get(crate_code)
        if c is None:
            raise DomainError(f"箱件不存在: {crate_code}")
        self._require_effective_version(p, c.version_ref)
        if c.released_at is not None:
            raise DomainError(f"箱件 {crate_code} 已放行，职责确认不可再变更")
        if role in c.signs:
            raise DomainError(
                f"箱件 {crate_code} 的{ROLE_LABELS[role]}职责已由 "
                f"{c.signs[role].signer} 确认，不可重复签署"
            )
        obligation_code = c.gates[role]
        o = self._get_obligation(p, obligation_code)
        if not o.is_fulfilled:
            raise DomainError(
                f"{ROLE_LABELS[role]}职责承诺 {obligation_code} 尚未满足"
                f"（当前状态 {o.state}），不能签署放行确认"
            )
        if not signer or not signer.strip():
            raise DomainError("签署人不能为空")
        return self._append(
            CRATE_GATE_SIGNED,
            f"crate-{crate_code}",
            {
                "crate_code": crate_code,
                "role": role,
                "signer": signer,
                "obligation_code": obligation_code,
            },
            at=at,
        )

    def release_crate(self, crate_code: str, *,
                      at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        c = p.crates.get(crate_code)
        if c is None:
            raise DomainError(f"箱件不存在: {crate_code}")
        if c.released_at is not None:
            raise DomainError(f"箱件 {crate_code} 已放行")
        self._require_effective_version(p, c.version_ref)
        blockers = self._crate_blockers(p, c)
        if blockers:
            raise DomainError(f"箱件 {crate_code} 不可放行: {'；'.join(blockers)}")
        return self._append(
            CRATE_RELEASED,
            f"crate-{crate_code}",
            {"crate_code": crate_code},
            at=at,
        )

    def hand_over(self, crate_code: str, receiver: str, location: str, *,
                  insurance_party: str | None = None, final: bool = False,
                  at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        c = p.crates.get(crate_code)
        if c is None:
            raise DomainError(f"箱件不存在: {crate_code}")
        if c.released_at is None:
            raise DomainError(f"箱件 {crate_code} 尚未放行，不能交接")
        # 暂停只阻止新放行与发布，不阻止在途箱件的实际交接登记——
        # 取消/过期后实体位置与保险责任仍须如实记录、可追溯。
        if not receiver or not receiver.strip():
            raise DomainError("接收方不能为空")
        if not location or not location.strip():
            raise DomainError("交接位置不能为空")
        return self._append(
            CRATE_HANDED_OVER,
            f"crate-{crate_code}",
            {
                "crate_code": crate_code,
                "receiver": receiver,
                "location": location,
                "insurance_party": insurance_party,
                "final": bool(final),
            },
            at=at,
        )

    def sweep_expirations(self, at: datetime | None = None) -> int:
        """对外暴露的到期巡检；返回本次新过期的承诺数。"""
        at = at or self.clock()
        before = {o.code for o in self._projection().obligations.values()
                  if o.state == "EXPIRED"}
        self._sweep_expirations(at)
        after = {o.code for o in self._projection().obligations.values()
                 if o.state == "EXPIRED"}
        return len(after - before)

    # ---- 外部回执 ----

    def receive_ack(self, external_ref: str, sender: str, content: Any, *,
                    declared_version_ref: str | None = None,
                    received_at: str | datetime | None = None,
                    arrival_at: datetime | None = None) -> dict[str, Any]:
        """登记外部回执。

        - 同一 external_ref + 相同内容：幂等去重，不追加事件；
        - 同一 external_ref + 内容冲突：转人工协调，首收内容保留为准候选，
          到达顺序只记录，不决定真假；
        - 回执永不修改协议版本状态，迟到确认不可能复活失效版本。
        """
        arrival_at = arrival_at or self.clock()
        received_dt = parse_time(received_at) if received_at else arrival_at
        p = self._projection()
        existing = p.acks_by_ref.get(external_ref)
        content_hash = _hash_content(content)

        if existing is not None:
            if content_hash == existing.first_content_hash and all(
                content_hash != x["content_hash"] for x in existing.contenders
            ):
                # 重复回执（内容与首收一致）：按外部编号去重，幂等返回首收事件。
                return {
                    "deduped": True,
                    "external_ref": external_ref,
                    "aggregate_id": existing.aggregate_id,
                    "state": existing.state,
                }
            if any(content_hash == x["content_hash"] for x in existing.contenders):
                return {
                    "deduped": True,
                    "external_ref": external_ref,
                    "aggregate_id": existing.aggregate_id,
                    "state": existing.state,
                }
            self._append(
                ACK_CONFLICT_FLAGGED,
                existing.aggregate_id,
                {
                    "external_ref": external_ref,
                    "sender": sender,
                    "content": content,
                    "content_hash": content_hash,
                    "received_at": received_dt.isoformat(),
                    "arrival_at": arrival_at.isoformat(),
                },
                at=arrival_at,
            )
            return {
                "deduped": False,
                "external_ref": external_ref,
                "aggregate_id": existing.aggregate_id,
                "state": ACK_CONFLICT_STATE,
            }

        event = self._append(
            ACK_RECEIVED,
            f"external_ack-{external_ref}",
            {
                "external_ref": external_ref,
                "sender": sender,
                "content": content,
                "content_hash": content_hash,
                "received_at": received_dt.isoformat(),
                "arrival_at": arrival_at.isoformat(),
                "declared_version_ref": declared_version_ref,
            },
            at=arrival_at,
        )
        return {
            "deduped": False,
            "external_ref": external_ref,
            "aggregate_id": event["aggregate_id"],
            "state": ACK_RECEIVED_STATE,
        }

    def resolve_ack(self, external_ref: str, resolution: str, coordinator: str, *,
                    content: Any = None, note: str | None = None,
                    at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._projection()
        ack = p.acks_by_ref.get(external_ref)
        if ack is None:
            raise DomainError(f"回执不存在: {external_ref}")
        if ack.state != ACK_CONFLICT_STATE:
            raise DomainError(f"回执 {external_ref} 无待协调冲突（状态 {ack.state}）")
        if resolution == "FIRST":
            chosen = ack.first_content
        elif resolution == "LATEST":
            chosen = ack.contenders[-1]["content"]
        elif resolution == "MANUAL":
            if content is None:
                raise DomainError("人工裁决必须提供 content")
            chosen = content
        else:
            raise DomainError("resolution 必须是 FIRST / LATEST / MANUAL")
        return self._append(
            ACK_RESOLVED,
            ack.aggregate_id,
            {
                "external_ref": external_ref,
                "resolution": resolution,
                "content": chosen,
                "content_hash": _hash_content(chosen),
                "coordinator": coordinator,
                "note": note,
            },
            at=at,
        )

    # ---- 公布 ----

    def approve_publication(self, version_ref: str, material_ref: str, *,
                            channel: str = "",
                            obligation_codes: list[str] | None = None,
                            at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        self._require_effective_version(p, version_ref)
        for code in obligation_codes or []:
            self._get_obligation(p, code)
        return self._append(
            PUBLICATION_APPROVED,
            f"publication-{material_ref}",
            {
                "version_ref": version_ref,
                "material_ref": material_ref,
                "channel": channel,
                "obligation_codes": list(obligation_codes or []),
            },
            at=at,
        )

    # ---- 结项核对 ----

    def reconcile_closure(self, version_ref: str, *,
                          at: datetime | None = None) -> dict[str, Any]:
        at = at or self.clock()
        p = self._sweep_expirations(at)
        self._get_version(p, version_ref)
        open_conflicts = [
            a.external_ref for a in p.acks_by_ref.values() if a.state == ACK_CONFLICT_STATE
        ]
        summary = self.export_chain()
        crates = [c for c in summary["crates"] if c["version_ref"] == version_ref]
        event = self._append(
            CLOSURE_RECONCILED,
            f"agreement_version-{version_ref}",
            {
                "version_ref": version_ref,
                "open_conflicts": open_conflicts,
                "crate_count": len(crates),
                "delivered_count": sum(1 for c in crates if c["status"] == "DELIVERED"),
                "custody_snapshot": [
                    {"crate": c["code"], "custodian": c["current_custodian"],
                     "location": c["current_location"], "status": c["status"]}
                    for c in crates
                ],
            },
            at=at,
        )
        return event

    # ---- 导出：责任链 ----

    def _crate_blockers(self, p: Projection, c: CrateView) -> list[str]:
        blockers: list[str] = []
        v = p.versions.get(c.version_ref)
        if v is not None and not v.effective:
            label = "已取代" if v.status == VERSION_SUPERSEDED else "已取消"
            blockers.append(f"协议版本{label}")
        for role in RELEASE_ROLES:
            sign = c.signs.get(role)
            if sign is None:
                obl_code = c.gates[role]
                o = p.obligations.get(obl_code)
                detail = {
                    "DEFINED": "承诺尚未被对方接受",
                    "ACCEPTED": "证据尚未通过复核",
                    "EXPIRED": "承诺已过期，须按替代方案履行",
                    "FULFILLED": "已满足待签署",
                    "FULFILLED_ALTERNATIVE": "已按替代方案满足待签署",
                }.get(o.state if o else "MISSING", "承诺缺失")
                blockers.append(
                    f"缺少{ROLE_LABELS[role]}职责确认（承诺 {obl_code}：{detail}）"
                )
        for key in c.active_suspensions:
            reason, ref = key
            if reason == SUSPEND_OBLIGATION_EXPIRED:
                blockers.append(f"关联承诺 {ref} 过期，箱件暂停")
            elif reason == SUSPEND_VERSION_CANCELLED:
                blockers.append("协议版本已取消，箱件暂停")
            else:
                blockers.append(f"箱件暂停：{reason}")
        return blockers

    def export_chain(self) -> dict[str, Any]:
        """导出责任链：每个箱件为何可移交、当前由谁保管、卡点是什么。"""
        p = self._projection()
        crates_out: list[dict[str, Any]] = []
        for c in p.crates.values():
            v = p.versions.get(c.version_ref)
            gates_out = {}
            for role in RELEASE_ROLES:
                obl_code = c.gates[role]
                o = p.obligations[obl_code]
                sign = c.signs.get(role)
                ev_accepted = next(
                    (ev for ev in o.evidence.values() if ev.accepted), None
                )
                gates_out[role] = {
                    "label": ROLE_LABELS[role],
                    "obligation_code": obl_code,
                    "obligation_state": o.state,
                    "responsible_party": o.responsible_party,
                    "evidence_ref": ev_accepted.evidence_ref if ev_accepted else None,
                    "evidence_reviewer": ev_accepted.reviewer if ev_accepted else None,
                    "signed": sign is not None,
                    "signer": sign.signer if sign else None,
                    "signed_at": sign.signed_at.isoformat() if sign else None,
                }
            blockers = self._crate_blockers(p, c)
            # 保险责任：以最近一次交接记载为准；未交接则取保险闸门承诺的责任方。
            insurance_party = None
            if c.handovers and c.handovers[-1].insurance_party:
                insurance_party = c.handovers[-1].insurance_party
            else:
                insurance_obl = p.obligations.get(c.gates[INSURANCE])
                insurance_party = (
                    insurance_obl.responsible_party if insurance_obl else None
                )
            crates_out.append(
                {
                    "code": c.code,
                    "description": c.description,
                    "version_ref": c.version_ref,
                    "version_status": v.status if v else None,
                    "status": c.status,
                    "releasable": c.status in ("READY",) or (
                        c.released_at is None and not blockers and c.all_gates_signed
                        and v is not None and v.effective
                    ),
                    "blocking_reasons": blockers if c.released_at is None else [],
                    "suspensions": [
                        {"reason": reason, "reason_key": ref}
                        for reason, ref in c.active_suspensions
                    ],
                    "gates": gates_out,
                    "released_at": c.released_at.isoformat() if c.released_at else None,
                    "current_custodian": c.current_custodian,
                    "current_location": c.current_location,
                    "insurance_party": insurance_party,
                    "custody_chain": [
                        {"custodian": c.origin_custodian,
                         "location": c.origin_location,
                         "at": c.listed_at.isoformat(),
                         "kind": "ORIGIN"}
                    ]
                    + [
                        {
                            "custodian": h.receiver,
                            "location": h.location,
                            "at": h.at.isoformat(),
                            "insurance_party": h.insurance_party,
                            "final": h.final,
                            "kind": "HANDOVER",
                        }
                        for h in c.handovers
                    ],
                }
            )

        obligations_out = [
            {
                "code": o.code,
                "version_ref": o.version_ref,
                "title": o.title,
                "responsible_party": o.responsible_party,
                "role": o.role,
                "deadline": o.deadline.isoformat(),
                "state": o.state,
                "prerequisite_codes": o.prerequisite_codes,
                "alternatives": o.alternatives,
                "linked_crates": [
                    c.code for c in p.crates.values()
                    if o.code in c.linked_obligations
                ],
                "accepted_evidence": [
                    ev.evidence_ref
                    for ev in o.evidence.values() if ev.accepted
                ],
            }
            for o in p.obligations.values()
        ]

        publications_out = [
            {
                "material_ref": pub.material_ref,
                "version_ref": pub.version_ref,
                "channel": pub.channel,
                "approved_at": pub.approved_at.isoformat(),
                "suspended": pub.suspended,
                "suspensions": [
                    {"reason": reason, "reason_key": ref}
                    for reason, ref in pub.active_suspensions
                ],
            }
            for pub in p.publications
        ]

        acks_out = [
            {
                "external_ref": a.external_ref,
                "state": a.state,
                "first_sender": a.first_sender,
                "first_received_at": a.first_received_at.isoformat(),
                "declared_version_ref": a.declared_version_ref,
                "effective_content": a.effective_content,
                "contender_count": len(a.contenders),
                "resolution": a.resolution,
                "resolved_by": a.resolved_by,
            }
            for a in p.acks_by_ref.values()
        ]

        return {
            "generated_at": self.clock().isoformat(),
            "versions": [
                {
                    "ref": v.ref,
                    "title": v.title,
                    "status": v.status,
                    "recorded_at": v.recorded_at.isoformat(),
                    "superseded_by": v.superseded_by,
                    "cancelled_at": v.cancelled_at.isoformat() if v.cancelled_at else None,
                    "cancel_reason": v.cancel_reason,
                }
                for v in p.versions.values()
            ],
            "obligations": obligations_out,
            "crates": crates_out,
            "publications": publications_out,
            "acks": acks_out,
            "manual_queue": [a.external_ref for a in p.acks_by_ref.values()
                            if a.state == ACK_CONFLICT_STATE],
        }


def _publication_dead(pub: PublicationView, p: Projection) -> bool:
    v = p.versions.get(pub.version_ref)
    return v is not None and v.status == VERSION_CANCELLED


def _hash_content(content: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_content(content).encode("utf-8")).hexdigest()
