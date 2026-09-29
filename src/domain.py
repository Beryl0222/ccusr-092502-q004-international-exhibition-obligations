"""领域投影：把追加的事件流折叠成当前可读状态。

本模块是纯函数式折叠，不产生事件、不依赖当前时钟；所有“现在是否过期”的
判断由服务层先落 OBLIGATION_EXPIRED 事件，再由这里反映。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

ROLES = ("COLLECTION", "INSURANCE", "TRANSPORT")
ROLE_EVENT = {
    "COLLECTION": "CRATE_CONFIRMED_BY_COLLECTION",
    "INSURANCE": "CRATE_CONFIRMED_BY_INSURANCE",
    "TRANSPORT": "CRATE_CONFIRMED_BY_TRANSPORT",
}

# 可放行（条件开放）的承诺状态：正常满足、逾期补全、替代解除都算开放
OPEN_OBLIGATION_STATES = {"RELEASABLE", "RELEASED", "DISPENSED", "RECOVERED"}


class DomainError(ValueError):
    """业务规则拒绝。"""


@dataclass
class SignOff:
    evidence_kind: str
    evidence_ref: str
    submitted_by: str
    sign_off_by: str
    at: str
    satisfies_kind: str = ""
    via_alternative: bool = False
    note: str = ""


@dataclass
class Agreement:
    id: str
    ref: str
    title: str
    parties: list[dict[str, str]]
    state: str = "DRAFT"
    created_at: str = ""
    activated_at: str = ""
    superseded_by: str = ""
    cancelled_reason: str = ""


@dataclass
class Obligation:
    id: str
    code: str
    agreement_id: str
    description: str
    responsible_party_id: str
    deadline: str
    prerequisite_evidence_kinds: list[str]
    acceptable_alternatives: dict[str, str]  # 替代证据种类 -> 说明
    crate_codes: list[str]
    publication_codes: list[str]
    signoffs: list[SignOff] = field(default_factory=list)
    expired: bool = False
    expired_at: str = ""

    def covered_kinds(self) -> set[str]:
        # 替代签收按它所满足的原要求类型计入覆盖
        return {s.satisfies_kind or s.evidence_kind for s in self.signoffs}

    def missing_kinds(self) -> list[str]:
        covered = self.covered_kinds()
        return [k for k in self.prerequisite_evidence_kinds if k not in covered]

    def used_alternative(self) -> bool:
        return any(s.via_alternative for s in self.signoffs)


@dataclass
class LoanObject:
    id: str
    code: str
    title: str
    kind: str  # PHYSICAL / DIGITAL_REPRODUCTION
    agreement_id: str
    owner_party_id: str
    state: str = "LISTED"
    crate_id: str = ""
    rights_license_ref: str = ""


@dataclass
class CustodyEntry:
    at: str
    action: str
    custodian_party_id: str
    location: str
    note: str = ""


@dataclass
class Crate:
    id: str
    code: str
    origin_location: str
    origin_party_id: str
    roles_not_applicable: dict[str, str]  # role -> 不适用原因
    object_ids: list[str] = field(default_factory=list)
    confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: str = "ASSEMBLED"
    ledger: list[CustodyEntry] = field(default_factory=list)
    carrier_party_id: str = ""
    current_location: str = ""
    cancel_reason: str = ""


@dataclass
class Invitation:
    id: str
    code: str
    specialist_name: str
    meeting_ref: str
    issued_by: str
    issued_at: str
    deadline: str = ""
    state: str = "ISSUED"
    response_note: str = ""
    reissue_count: int = 0


@dataclass
class Publication:
    id: str
    code: str
    title: str
    material_ref: str
    agreement_id: str
    obligation_codes: list[str]
    state: str = "DRAFT"
    approved_by: str = ""
    published_at: str = ""
    published_ref: str = ""
    superseded_by: str = ""
    cancelled_reason: str = ""
    pre_hold_state: str = ""


@dataclass
class ExternalAck:
    id: str
    ack_ref: str
    sender_party_id: str
    content_hash: str
    content: dict[str, Any]
    regarding: dict[str, Any]
    received_seq: int
    state: str = "RECEIVED"
    case_id: str = ""
    late_and_not_effective: bool = False
    revival_note: str = ""


@dataclass
class CoordinationCase:
    id: str
    ack_ref: str
    ack_ids: list[str]
    opened_at: str
    state: str = "OPEN"
    resolution: str = ""
    chosen_ack_id: str = ""
    chosen_content_hash: str = ""
    resolved_by: str = ""
    resolved_at: str = ""
    note: str = ""


@dataclass
class State:
    agreements: dict[str, Agreement] = field(default_factory=dict)
    obligations: dict[str, Obligation] = field(default_factory=dict)
    objects: dict[str, LoanObject] = field(default_factory=dict)
    crates: dict[str, Crate] = field(default_factory=dict)
    invitations: dict[str, Invitation] = field(default_factory=dict)
    publications: dict[str, Publication] = field(default_factory=dict)
    acks: dict[str, ExternalAck] = field(default_factory=dict)
    cases: dict[str, CoordinationCase] = field(default_factory=dict)
    closure_seq: int = 0

    # ---- 外部编码索引 ----
    def agreement_by_ref(self, ref: str) -> Agreement | None:
        for a in self.agreements.values():
            if a.ref == ref:
                return a
        return None

    def crate_by_code(self, code: str) -> Crate | None:
        for c in self.crates.values():
            if c.code == code:
                return c
        return None

    def obligation_by_code(self, code: str) -> Obligation | None:
        for o in self.obligations.values():
            if o.code == code:
                return o
        return None

    def object_by_code(self, code: str) -> LoanObject | None:
        for o in self.objects.values():
            if o.code == code:
                return o
        return None

    def publication_by_code(self, code: str) -> Publication | None:
        for p in self.publications.values():
            if p.code == code:
                return p
        return None

    def invitation_by_code(self, code: str) -> Invitation | None:
        for i in self.invitations.values():
            if i.code == code:
                return i
        return None

    def acks_by_ref(self, ack_ref: str) -> list[ExternalAck]:
        return [a for a in self.acks.values() if a.ack_ref == ack_ref and a.state != "DUPLICATE_DISCARDED"]

    # ---- 承诺状态推导 ----
    def obligation_state(self, ob: Obligation) -> str:
        agreement = self.agreements.get(ob.agreement_id)
        if agreement is not None and agreement.state == "CANCELLED":
            return "CANCELLED"
        missing = ob.missing_kinds()
        if not missing:
            # 曾过期、后补全（含替代方案）：暂停解除，但保留逾期补全痕迹
            if ob.expired:
                return "RECOVERED"
            return "DISPENSED" if ob.used_alternative() else "RELEASABLE"
        if ob.expired:
            return "BLOCKED"
        return "PENDING"

    def gates_for_crate(self, crate: Crate) -> list[Obligation]:
        """当前仍有效的门控承诺：只取 ACTIVE 协议下的承诺；
        失效版本下的承诺在责任链中单独展示，不再阻塞移交。"""
        result: list[Obligation] = []
        for ob in self.obligations.values():
            if crate.code not in ob.crate_codes:
                continue
            agreement = self.agreements.get(ob.agreement_id)
            if agreement is None:
                continue
            result.append(ob)
        return result

    def active_gates_for_crate(self, crate: Crate) -> list[Obligation]:
        return [
            ob for ob in self.gates_for_crate(crate)
            if self.agreements[ob.agreement_id].state == "ACTIVE"
        ]

    def gates_for_publication(self, pub: Publication) -> list[Obligation]:
        result: list[Obligation] = []
        for code in pub.obligation_codes:
            ob = self.obligation_by_code(code)
            if ob is not None:
                result.append(ob)
        return result

    def crate_hold_reasons(self, crate: Crate) -> list[str]:
        if crate.state != "ASSEMBLED":
            return []
        reasons: list[str] = []
        for ob in self.active_gates_for_crate(crate):
            status = self.obligation_state(ob)
            if status not in OPEN_OBLIGATION_STATES:
                if status == "BLOCKED":
                    reasons.append(f"承诺 {ob.code} 已于 {ob.deadline} 过期未满足（责任方 {ob.responsible_party_id}）")
                else:
                    reasons.append(
                        f"承诺 {ob.code} 未满足，缺前置证据：{', '.join(ob.missing_kinds())}"
                    )
        return reasons

    def crate_status(self, crate: Crate) -> str:
        if crate.state != "ASSEMBLED":
            return crate.state
        if self.crate_hold_reasons(crate):
            return "HOLD_PENDING_CONDITIONS"
        if self._all_roles_set(crate):
            return "READY_TO_HANDOVER"
        return "ASSEMBLED"

    def _all_roles_set(self, crate: Crate) -> bool:
        for role in ROLES:
            if role in crate.roles_not_applicable:
                continue
            if role not in crate.confirmations:
                return False
        return True

    def missing_roles(self, crate: Crate) -> list[str]:
        return [
            r for r in ROLES
            if r not in crate.roles_not_applicable and r not in crate.confirmations
        ]

    def current_custodian(self, crate: Crate) -> CustodyEntry | None:
        return crate.ledger[-1] if crate.ledger else None


def fold(events: Iterable[dict[str, Any]]) -> State:
    state = State()
    for event in events:
        apply_one(state, event)
    return state


def apply_one(state: State, event: dict[str, Any]) -> None:
    etype = event["event_type"]
    p = event["payload"]
    at = event["occurred_at"]
    aid = event["aggregate_id"]

    if etype == "AGREEMENT_DRAFTED":
        state.agreements[aid] = Agreement(
            id=aid, ref=p["agreement_ref"], title=p["title"],
            parties=p.get("parties", []), created_at=at,
        )
    elif etype == "AGREEMENT_ACTIVATED":
        state.agreements[aid].state = "ACTIVE"
        state.agreements[aid].activated_at = at
    elif etype == "AGREEMENT_SUPERSEDED":
        state.agreements[aid].state = "SUPERSEDED"
        state.agreements[aid].superseded_by = p["replaced_by"]
    elif etype == "AGREEMENT_CANCELLED":
        state.agreements[aid].state = "CANCELLED"
        state.agreements[aid].cancelled_reason = p["reason"]

    elif etype == "OBLIGATION_ACCEPTED":
        state.obligations[aid] = Obligation(
            id=aid, code=p["code"], agreement_id=p["agreement_version_id"],
            description=p["description"], responsible_party_id=p["responsible_party_id"],
            deadline=p["deadline"], prerequisite_evidence_kinds=list(p["prerequisite_evidence_kinds"]),
            acceptable_alternatives=dict(p.get("acceptable_alternatives", {})),
            crate_codes=list(p.get("crate_codes", [])),
            publication_codes=list(p.get("publication_codes", [])),
        )
    elif etype == "EVIDENCE_SIGNED":
        ob = state.obligations[aid]
        ob.signoffs.append(SignOff(
            evidence_kind=p["evidence_kind"], evidence_ref=p["evidence_ref"],
            submitted_by=p["submitted_by"], sign_off_by=p["sign_off_by"], at=at,
            satisfies_kind=p.get("satisfies_kind", ""),
            note=p.get("note", ""),
        ))
    elif etype == "ALTERNATIVE_EVIDENCE_APPLIED":
        ob = state.obligations[aid]
        ob.signoffs.append(SignOff(
            evidence_kind=p["evidence_kind"], evidence_ref=p["evidence_ref"],
            submitted_by=p["submitted_by"], sign_off_by=p["sign_off_by"], at=at,
            satisfies_kind=p.get("satisfies_kind", p["evidence_kind"]),
            via_alternative=True, note=p.get("note", ""),
        ))
    elif etype == "OBLIGATION_EXPIRED":
        ob = state.obligations[aid]
        ob.expired = True
        ob.expired_at = at

    elif etype == "OBJECT_LISTED":
        state.objects[aid] = LoanObject(
            id=aid, code=p["code"], title=p["title"], kind=p["kind"],
            agreement_id=p["agreement_version_id"], owner_party_id=p["owner_party_id"],
            rights_license_ref=p.get("rights_license_ref", ""),
        )
    elif etype == "OBJECT_PACKED_INTO_CRATE":
        obj = state.objects[aid]
        obj.state = "PACKED"
        obj.crate_id = p["crate_id"]
        crate = state.crates[p["crate_id"]]
        if aid not in crate.object_ids:
            crate.object_ids.append(aid)

    elif etype == "CRATE_ASSEMBLED":
        crate = Crate(
            id=aid, code=p["code"], origin_location=p["origin_location"],
            origin_party_id=p["origin_party_id"],
            roles_not_applicable=dict(p.get("roles_not_applicable", {})),
            current_location=p["origin_location"],
        )
        crate.ledger.append(CustodyEntry(
            at=at, action="ASSEMBLED", custodian_party_id=p["origin_party_id"],
            location=p["origin_location"], note="箱件在起运地集结，由起运机构保管",
        ))
        state.crates[aid] = crate
    elif etype in ROLE_EVENT.values():
        crate = state.crates[aid]
        role = {v: k for k, v in ROLE_EVENT.items()}[etype]
        crate.confirmations[role] = {
            "by": p["confirmer_party_id"], "at": at,
            "evidence_ref": p.get("evidence_ref", ""), "note": p.get("note", ""),
        }
    elif etype == "CRATE_RELEASED":
        crate = state.crates[aid]
        crate.state = "RELEASED_TO_CARRIER"
        crate.carrier_party_id = p["carrier_party_id"]
        crate.current_location = "IN_TRANSIT"
        crate.ledger.append(CustodyEntry(
            at=at, action="RELEASED_TO_CARRIER", custodian_party_id=p["carrier_party_id"],
            location="IN_TRANSIT",
            note=p.get("custody_note", "三职责确认完成，移交承运人"),
        ))
    elif etype == "CRATE_DELIVERED":
        crate = state.crates[aid]
        crate.state = "DELIVERED"
        crate.current_location = p["location"]
        crate.ledger.append(CustodyEntry(
            at=at, action="DELIVERED", custodian_party_id=p["received_by_party_id"],
            location=p["location"], note="目的地签收",
        ))
        for oid in crate.object_ids:
            state.objects[oid].state = "DELIVERED"
    elif etype == "CRATE_RETURNED":
        crate = state.crates[aid]
        crate.state = "RETURNED"
        crate.current_location = p["location"]
        crate.ledger.append(CustodyEntry(
            at=at, action="RETURNED", custodian_party_id=p["received_by_party_id"],
            location=p["location"], note="展览结束退回",
        ))
        for oid in crate.object_ids:
            state.objects[oid].state = "RETURNED"
    elif etype == "CRATE_CANCELLED":
        crate = state.crates[aid]
        crate.state = "CANCELLED"
        crate.cancel_reason = p["reason"]
        crate.current_location = p["final_location"]
        crate.ledger.append(CustodyEntry(
            at=at, action="CANCELLED", custodian_party_id=p["custodian_party_id"],
            location=p["final_location"],
            note=f"取消移交：{p['reason']}；保险责任方 {p.get('insurance_responsible_party_id', '未记录')}",
        ))

    elif etype == "INVITATION_ISSUED":
        state.invitations[aid] = Invitation(
            id=aid, code=p["code"], specialist_name=p["specialist_name"],
            meeting_ref=p["meeting_ref"], issued_by=p["issued_by"], issued_at=at,
            deadline=p.get("deadline", ""),
        )
    elif etype == "INVITATION_ACKNOWLEDGED":
        inv = state.invitations[aid]
        inv.state = "ACCEPTED" if p["response"] == "ACCEPTED" else "DECLINED"
        inv.response_note = p.get("note", "")
    elif etype == "INVITATION_REISSUED":
        inv = state.invitations[aid]
        inv.state = "ISSUED"
        inv.response_note = ""
        inv.reissue_count += 1
    elif etype == "INVITATION_CANCELLED":
        state.invitations[aid].state = "CANCELLED"

    elif etype == "PUBLICATION_DRAFTED":
        state.publications[aid] = Publication(
            id=aid, code=p["code"], title=p["title"], material_ref=p["material_ref"],
            agreement_id=p["agreement_version_id"],
            obligation_codes=list(p.get("obligation_codes", [])),
        )
    elif etype == "PUBLICATION_HELD":
        pub = state.publications[aid]
        pub.pre_hold_state = pub.state
        pub.state = "HELD"
    elif etype == "PUBLICATION_HOLD_LIFTED":
        pub = state.publications[aid]
        # 回到暂停前状态（通常是 DRAFT），由人工重新走批准；不直接发布
        pub.state = pub.pre_hold_state or "DRAFT"
        pub.pre_hold_state = ""
    elif etype == "PUBLICATION_APPROVED_FOR_RELEASE":
        pub = state.publications[aid]
        pub.state = "APPROVED_FOR_RELEASE"
        pub.approved_by = p["approved_by"]
    elif etype == "PUBLICATION_PUBLISHED":
        pub = state.publications[aid]
        pub.state = "PUBLISHED"
        pub.published_at = at
        pub.published_ref = p["published_ref"]
    elif etype == "PUBLICATION_SUPERSEDED":
        pub = state.publications[aid]
        pub.state = "SUPERSEDED"
        pub.superseded_by = p["replaced_by"]
    elif etype == "PUBLICATION_CANCELLED":
        pub = state.publications[aid]
        pub.state = "CANCELLED"
        pub.cancelled_reason = p["reason"]

    elif etype == "ACK_RECEIVED":
        state.acks[aid] = ExternalAck(
            id=aid, ack_ref=p["ack_ref"], sender_party_id=p["sender_party_id"],
            content_hash=p["content_hash"], content=p.get("content", {}),
            regarding=p.get("regarding", {}), received_seq=event["seq"],
            late_and_not_effective=p.get("late_and_not_effective", False),
            revival_note=p.get("revival_note", ""),
        )
    elif etype == "ACK_DUPLICATE_DISCARDED":
        # 丢弃的重复件也保留为只读记录，但不参与任何索引与状态
        state.acks[aid] = ExternalAck(
            id=aid, ack_ref=p["ack_ref"], sender_party_id=p["sender_party_id"],
            content_hash=p["content_hash"], content=p.get("content", {}),
            regarding=p.get("regarding", {}), received_seq=event["seq"],
            state="DUPLICATE_DISCARDED",
            late_and_not_effective=p.get("late_and_not_effective", False),
            revival_note=p.get("revival_note", ""),
        )
    elif etype == "ACK_CONFLICT_ESCALATED":
        case_id = p["case_id"]
        case = state.cases.get(case_id)
        if case is None:
            case = CoordinationCase(
                id=case_id, ack_ref=p["ack_ref"],
                ack_ids=list(p.get("ack_ids", [])), opened_at=at,
            )
            state.cases[case_id] = case
        for ack_id in p["affected_ack_ids"]:
            if ack_id in state.acks:
                state.acks[ack_id].state = "IN_CONFLICT"
                state.acks[ack_id].case_id = case_id
                if ack_id not in case.ack_ids:
                    case.ack_ids.append(ack_id)
    elif etype == "COORDINATION_RESOLVED":
        case = state.cases[aid]
        case.state = "RESOLVED"
        case.resolution = p["resolution"]
        case.chosen_ack_id = p["chosen_ack_id"]
        case.chosen_content_hash = p["chosen_content_hash"]
        case.resolved_by = p["resolved_by"]
        case.resolved_at = at
        case.note = p.get("note", "")
        for ack_id in case.ack_ids:
            state.acks[ack_id].state = "RESOLVED"

    elif etype == "CLOSURE_RECONCILED":
        state.closure_seq += 1
