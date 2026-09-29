"""应用服务：所有业务规则与命令处理。

关键不变量：
- 事实只追加；命令在事件存储的同一把锁内完成“读投影→校验→落事件”。
- 三职责（馆藏/保险/运输）分别确认，确认方不得为同一方；证据提交人不得自核。
- 承诺过期只暂停其关联箱件与公布版本，不波及无关箱件。
- 外部回执按外部编号去重；内容冲突转人工，到达顺序不决定真假。
- 已被取代/取消的协议版本不会被迟到确认复活。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from src import domain
from src.domain import DomainError, State, fold
from src.eventstore import EventStore

ROLE_CONFIRM_EVENTS = domain.ROLE_EVENT


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(content: Any) -> str:
    body = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(f"{field} 不是有效时间") from exc
    if parsed.tzinfo is None:
        raise DomainError(f"{field} 必须包含时区")
    return parsed


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ObligationService:
    def __init__(self, store: EventStore, clock: Callable[[], str] = utcnow_iso) -> None:
        self.store = store
        self.state: State = fold(store.all_events())
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _emit(self, aggregate_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": _new_id("evt"),
            "event_type": event_type,
            "occurred_at": self.clock(),
            "aggregate_id": aggregate_id,
            "version": self.store.next_version(aggregate_id),
            "payload": payload,
        }
        stored = self.store.append(event)
        domain.apply_one(self.state, stored)
        return stored

    def _now(self) -> datetime:
        return _parse_ts(self.clock(), "系统时间")

    def _get_agreement(self, agreement_id: str):
        ag = self.state.agreements.get(agreement_id)
        if ag is None:
            raise DomainError(f"协议版本不存在: {agreement_id}")
        return ag

    def _require_active_agreement(self, agreement_id: str):
        ag = self._get_agreement(agreement_id)
        if ag.state == "SUPERSEDED":
            raise DomainError(
                f"协议版本 {ag.ref} 已被 {ag.superseded_by} 取代，迟到确认不得使其重新生效"
            )
        if ag.state == "CANCELLED":
            raise DomainError(f"协议版本 {ag.ref} 已取消，不得再变更")
        if ag.state != "ACTIVE":
            raise DomainError(f"协议版本 {ag.ref} 当前状态为 {ag.state}，尚未生效")
        return ag

    def _get_obligation(self, obligation_id: str):
        ob = self.state.obligations.get(obligation_id)
        if ob is None:
            raise DomainError(f"承诺不存在: {obligation_id}")
        return ob

    def _get_crate(self, crate_id: str):
        crate = self.state.crates.get(crate_id)
        if crate is None:
            raise DomainError(f"箱件不存在: {crate_id}")
        return crate

    def _get_publication(self, publication_id: str):
        pub = self.state.publications.get(publication_id)
        if pub is None:
            raise DomainError(f"公布版本不存在: {publication_id}")
        return pub

    # ---------------------------------------------------------- agreement versions
    def draft_agreement(self, body: dict[str, Any]) -> dict[str, Any]:
        for field_ in ("agreement_ref", "title"):
            if not body.get(field_):
                raise DomainError(f"缺少字段: {field_}")
        if self.state.agreement_by_ref(body["agreement_ref"]) is not None:
            raise DomainError(f"协议编号已存在: {body['agreement_ref']}")
        agreement_id = body.get("id") or _new_id("agreement_version")
        return self._emit(agreement_id, "AGREEMENT_DRAFTED", {
            "agreement_ref": body["agreement_ref"],
            "title": body["title"],
            "parties": body.get("parties", []),
        })

    def activate_agreement(self, agreement_id: str) -> dict[str, Any]:
        ag = self._get_agreement(agreement_id)
        if ag.state == "ACTIVE":
            raise DomainError("协议已生效")
        if ag.state in ("SUPERSEDED", "CANCELLED"):
            raise DomainError(f"协议已{('取消' if ag.state == 'CANCELLED' else '被取代')}，不得生效")
        return self._emit(agreement_id, "AGREEMENT_ACTIVATED", {})

    def supersede_agreement(self, agreement_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ag = self._require_active_agreement(agreement_id)
        replaced_by = body.get("replaced_by") or body.get("replaced_by_ref")
        if not replaced_by:
            raise DomainError("缺少字段: replaced_by")
        return self._emit(agreement_id, "AGREEMENT_SUPERSEDED", {"replaced_by": replaced_by})

    def cancel_agreement(self, agreement_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ag = self._get_agreement(agreement_id)
        if ag.state == "CANCELLED":
            raise DomainError("协议已取消")
        if ag.state == "SUPERSEDED":
            raise DomainError("已被取代的版本直接保留历史，不另行取消")
        return self._emit(agreement_id, "AGREEMENT_CANCELLED", {
            "reason": body.get("reason", ""),
        })

    # ------------------------------------------------------------------ obligations
    def accept_obligation(self, body: dict[str, Any]) -> dict[str, Any]:
        agreement_id = body.get("agreement_version_id")
        self._require_active_agreement(agreement_id)
        for field_ in ("code", "description", "responsible_party_id", "deadline",
                       "prerequisite_evidence_kinds"):
            if field_ not in body:
                raise DomainError(f"缺少字段: {field_}")
        _parse_ts(body["deadline"], "deadline")
        kinds = body["prerequisite_evidence_kinds"]
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
            raise DomainError("prerequisite_evidence_kinds 必须是非空字符串数组")
        if self.state.obligation_by_code(body["code"]) is not None:
            raise DomainError(f"承诺编号已存在: {body['code']}")
        obligation_id = body.get("id") or _new_id("obligation")
        return self._emit(obligation_id, "OBLIGATION_ACCEPTED", {
            "code": body["code"],
            "agreement_version_id": agreement_id,
            "description": body["description"],
            "responsible_party_id": body["responsible_party_id"],
            "deadline": body["deadline"],
            "prerequisite_evidence_kinds": kinds,
            "acceptable_alternatives": body.get("acceptable_alternatives", {}),
            "crate_codes": body.get("crate_codes", []),
            "publication_codes": body.get("publication_codes", []),
        })

    def sign_evidence(self, obligation_id: str, body: dict[str, Any], *, alternative: bool) -> dict[str, Any]:
        ob = self._get_obligation(obligation_id)
        self._require_active_agreement(ob.agreement_id)
        submitted_by = body.get("submitted_by", "")
        sign_off_by = body.get("sign_off_by", "")
        if not submitted_by or not sign_off_by:
            raise DomainError("证据必须记录提交人与复核人")
        if submitted_by == sign_off_by:
            raise DomainError("提交人不得复核自己的证据")
        evidence_ref = body.get("evidence_ref", "")
        if not evidence_ref:
            raise DomainError("缺少字段: evidence_ref")

        if alternative:
            satisfies_kind = body.get("satisfies_kind", "")
            alt_kind = body.get("alternative_evidence_kind", "")
            if satisfies_kind not in ob.prerequisite_evidence_kinds:
                raise DomainError(f"{satisfies_kind} 不是承诺 {ob.code} 要求的前置证据")
            allowed = ob.acceptable_alternatives.get(satisfies_kind)
            if not allowed:
                raise DomainError(f"承诺 {ob.code} 未声明 {satisfies_kind} 的可接受替代方案")
            if alt_kind != allowed:
                raise DomainError(
                    f"替代证据类型不被接受：{satisfies_kind} 只接受 {allowed}，收到 {alt_kind}"
                )
            if satisfies_kind in ob.covered_kinds():
                raise DomainError(f"{satisfies_kind} 已被证据覆盖，无需再替代")
            payload = {
                "evidence_kind": alt_kind,
                "satisfies_kind": satisfies_kind,
                "evidence_ref": evidence_ref,
                "submitted_by": submitted_by,
                "sign_off_by": sign_off_by,
                "note": body.get("note", f"以替代证据 {alt_kind} 满足 {satisfies_kind}"),
            }
            event_type = "ALTERNATIVE_EVIDENCE_APPLIED"
        else:
            kind = body.get("evidence_kind", "")
            if kind not in ob.prerequisite_evidence_kinds:
                raise DomainError(f"{kind} 不是承诺 {ob.code} 要求的前置证据")
            if kind in ob.covered_kinds():
                raise DomainError(f"{kind} 已被证据覆盖")
            payload = {
                "evidence_kind": kind,
                "satisfies_kind": kind,
                "evidence_ref": evidence_ref,
                "submitted_by": submitted_by,
                "sign_off_by": sign_off_by,
                "note": body.get("note", ""),
            }
            event_type = "EVIDENCE_SIGNED"

        events = [self._emit(ob.id, event_type, payload)]
        events.extend(self._auto_resume_publications(ob))
        return events[-1]

    def _auto_resume_publications(self, ob) -> list[dict[str, Any]]:
        """承诺在暂停后补全（含替代方案）：关联公布版本解除暂停，等待人工批准。

        箱件的暂停由投影即时解除（无副作用事件）；公布版本需要一条审计事件
        记录“由哪条承诺补全而解除暂停”。
        """
        out: list[dict[str, Any]] = []
        refreshed = self.state.obligation_by_code(ob.code)
        if self.state.obligation_state(refreshed) not in domain.OPEN_OBLIGATION_STATES:
            return out
        for pub in self.state.publications.values():
            if pub.state != "HELD" or ob.code not in pub.obligation_codes:
                continue
            if all(
                self.state.obligation_state(g) in domain.OPEN_OBLIGATION_STATES
                for g in self.state.gates_for_publication(pub)
                if self.state.agreements[g.agreement_id].state == "ACTIVE"
            ):
                out.append(self._emit(pub.id, "PUBLICATION_HOLD_LIFTED", {
                    "reason": f"门控承诺 {ob.code} 已由证据补全，暂停解除，回到暂停前状态待人工批准",
                    "obligation_code": ob.code,
                }))
        return out

    def sweep_expired(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """把当前时刻已过截止期且前置证据仍未齐全的承诺标记过期。

        暂停范围严格限定为该承诺关联的箱件（由投影派生 HOLD）和公布版本
        （落 PUBLICATION_HELD 事件）。无关箱件与公布版本不受影响。
        """
        reference = _parse_ts(now, "now") if now else self._now()
        emitted: list[dict[str, Any]] = []
        for ob in list(self.state.obligations.values()):
            ag = self.state.agreements.get(ob.agreement_id)
            if ag is None or ag.state != "ACTIVE" or ob.expired:
                continue
            if not ob.missing_kinds():
                continue
            if _parse_ts(ob.deadline, "deadline") <= reference:
                emitted.append(self._emit(ob.id, "OBLIGATION_EXPIRED", {
                    "deadline": ob.deadline,
                    "missing_evidence_kinds": ob.missing_kinds(),
                }))
                for pub in self.state.publications.values():
                    # 起草中、已批准未发布的版本都暂停；已发布记录不可撤下，
                    # 只能事后以新版本取代（见 cancel/supersede 规则）。
                    if pub.state not in ("DRAFT", "APPROVED_FOR_RELEASE"):
                        continue
                    if ob.code in pub.obligation_codes:
                        emitted.append(self._emit(pub.id, "PUBLICATION_HELD", {
                            "reason": f"门控承诺 {ob.code} 已过期，暂停发布审批",
                            "obligation_code": ob.code,
                        }))
        return emitted

    # -------------------------------------------------------------- loan catalogue
    def list_object(self, body: dict[str, Any]) -> dict[str, Any]:
        agreement_id = body.get("agreement_version_id", "")
        self._get_agreement(agreement_id)
        for field_ in ("code", "title", "kind", "owner_party_id"):
            if not body.get(field_):
                raise DomainError(f"缺少字段: {field_}")
        if body["kind"] not in ("PHYSICAL", "DIGITAL_REPRODUCTION"):
            raise DomainError("kind 必须是 PHYSICAL 或 DIGITAL_REPRODUCTION")
        if self.state.object_by_code(body["code"]) is not None:
            raise DomainError(f"物件编号已存在: {body['code']}")
        object_id = body.get("id") or _new_id("loan_object")
        return self._emit(object_id, "OBJECT_LISTED", {
            "code": body["code"],
            "title": body["title"],
            "kind": body["kind"],
            "agreement_version_id": agreement_id,
            "owner_party_id": body["owner_party_id"],
            "rights_license_ref": body.get("rights_license_ref", ""),
        })

    # ----------------------------------------------------------------------- crates
    def assemble_crate(self, body: dict[str, Any]) -> dict[str, Any]:
        for field_ in ("code", "origin_location", "origin_party_id"):
            if not body.get(field_):
                raise DomainError(f"缺少字段: {field_}")
        if self.state.crate_by_code(body["code"]) is not None:
            raise DomainError(f"箱件编号已存在: {body['code']}")
        not_applicable = body.get("roles_not_applicable", {}) or {}
        for role in not_applicable:
            if role not in domain.ROLES:
                raise DomainError(f"未知职责: {role}")
            if not str(not_applicable[role]).strip():
                raise DomainError(f"职责 {role} 标记不适用时必须给出原因")
        crate_id = body.get("id") or _new_id("crate_transfer")
        return self._emit(crate_id, "CRATE_ASSEMBLED", {
            "code": body["code"],
            "origin_location": body["origin_location"],
            "origin_party_id": body["origin_party_id"],
            "roles_not_applicable": not_applicable,
        })

    def pack_object(self, crate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        crate = self._get_crate(crate_id)
        if crate.state != "ASSEMBLED":
            raise DomainError("箱件已移交或取消，不能再装入物件")
        obj_code = body.get("object_code", "")
        obj = self.state.object_by_code(obj_code)
        if obj is None:
            raise DomainError(f"物件不存在: {obj_code}")
        if obj.crate_id and obj.crate_id != crate_id:
            raise DomainError(f"物件 {obj_code} 已装入其他箱件")
        if obj.kind == "DIGITAL_REPRODUCTION":
            # 数字复制品可随介质运输；物理性两岗可在箱件上声明不适用，但保险职责仍须确认。
            pass
        return self._emit(obj.id, "OBJECT_PACKED_INTO_CRATE", {"crate_id": crate_id})

    def confirm_crate_role(self, crate_id: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        crate = self._get_crate(crate_id)
        if role not in domain.ROLES:
            raise DomainError(f"未知职责: {role}")
        if role in crate.roles_not_applicable:
            raise DomainError(
                f"{role} 对本箱件不适用：{crate.roles_not_applicable[role]}（责任链中留痕，无需确认）"
            )
        if crate.state != "ASSEMBLED":
            raise DomainError("箱件已移交或取消，不能再确认")
        if role in crate.confirmations:
            raise DomainError(f"{role} 已确认，不得重复确认")
        confirmer = body.get("confirmer_party_id", "")
        if not confirmer:
            raise DomainError("缺少字段: confirmer_party_id")
        for other_role, record in crate.confirmations.items():
            if record["by"] == confirmer:
                raise DomainError(
                    f"{confirmer} 已承担 {other_role} 确认；三种职责须由不同责任方分别确认"
                )
        return self._emit(crate_id, ROLE_CONFIRM_EVENTS[role], {
            "confirmer_party_id": confirmer,
            "evidence_ref": body.get("evidence_ref", ""),
            "note": body.get("note", ""),
        })

    def release_crate(self, crate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        crate = self._get_crate(crate_id)
        if crate.state == "CANCELLED":
            raise DomainError("箱件已取消，不能移交")
        if crate.state != "ASSEMBLED":
            raise DomainError("箱件已移交")
        missing_roles = self.state.missing_roles(crate)
        if missing_roles:
            raise DomainError(f"三职责确认未齐，缺：{', '.join(missing_roles)}")
        holds = self.state.crate_hold_reasons(crate)
        if holds:
            raise DomainError("箱件处于暂停状态：" + "；".join(holds))
        carrier = body.get("carrier_party_id", "")
        if not carrier:
            raise DomainError("缺少字段: carrier_party_id")
        return self._emit(crate_id, "CRATE_RELEASED", {
            "carrier_party_id": carrier,
            "custody_note": body.get("custody_note", "三职责确认完成，移交承运人"),
        })

    def deliver_crate(self, crate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        crate = self._get_crate(crate_id)
        if crate.state != "RELEASED_TO_CARRIER":
            raise DomainError("只有在途箱件可以签收交付")
        if not body.get("location") or not body.get("received_by_party_id"):
            raise DomainError("交付必须记录目的地位置与签收方")
        return self._emit(crate_id, "CRATE_DELIVERED", {
            "location": body["location"],
            "received_by_party_id": body["received_by_party_id"],
        })

    def return_crate(self, crate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        crate = self._get_crate(crate_id)
        if crate.state not in ("RELEASED_TO_CARRIER", "DELIVERED"):
            raise DomainError("只有已移交箱件可以退回")
        if not body.get("location") or not body.get("received_by_party_id"):
            raise DomainError("退回必须记录位置与接收方")
        return self._emit(crate_id, "CRATE_RETURNED", {
            "location": body["location"],
            "received_by_party_id": body["received_by_party_id"],
        })

    def cancel_crate(self, crate_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """取消移交：实体最后位置、保管人、保险责任方全部随事件留痕。"""
        crate = self._get_crate(crate_id)
        if crate.state == "CANCELLED":
            raise DomainError("箱件已取消")
        for field_ in ("final_location", "custodian_party_id", "insurance_responsible_party_id", "reason"):
            if not body.get(field_):
                raise DomainError(f"取消必须留痕字段: {field_}")
        return self._emit(crate_id, "CRATE_CANCELLED", {
            "final_location": body["final_location"],
            "custodian_party_id": body["custodian_party_id"],
            "insurance_responsible_party_id": body["insurance_responsible_party_id"],
            "reason": body["reason"],
        })

    # ------------------------------------------------------------- specialist flow
    def issue_invitation(self, body: dict[str, Any]) -> dict[str, Any]:
        for field_ in ("code", "specialist_name", "meeting_ref", "issued_by"):
            if not body.get(field_):
                raise DomainError(f"缺少字段: {field_}")
        if self.state.invitation_by_code(body["code"]) is not None:
            raise DomainError(f"邀请编号已存在: {body['code']}")
        invitation_id = body.get("id") or _new_id("specialist_invitation")
        payload = {
            "code": body["code"],
            "specialist_name": body["specialist_name"],
            "meeting_ref": body["meeting_ref"],
            "issued_by": body["issued_by"],
        }
        if body.get("deadline"):
            _parse_ts(body["deadline"], "deadline")
            payload["deadline"] = body["deadline"]
        return self._emit(invitation_id, "INVITATION_ISSUED", payload)

    def acknowledge_invitation(self, invitation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        inv = self.state.invitations.get(invitation_id)
        if inv is None:
            raise DomainError(f"邀请不存在: {invitation_id}")
        if inv.state == "CANCELLED":
            raise DomainError("邀请已取消")
        if body.get("response") not in ("ACCEPTED", "DECLINED"):
            raise DomainError("response 必须是 ACCEPTED 或 DECLINED")
        return self._emit(invitation_id, "INVITATION_ACKNOWLEDGED", {
            "response": body["response"],
            "note": body.get("note", ""),
        })

    def reissue_invitation(self, invitation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        inv = self.state.invitations.get(invitation_id)
        if inv is None:
            raise DomainError(f"邀请不存在: {invitation_id}")
        if inv.state == "CANCELLED":
            raise DomainError("邀请已取消，请重新发起")
        return self._emit(invitation_id, "INVITATION_REISSUED", {
            "note": body.get("note", "专家时间冲突后重新邀请"),
        })

    def cancel_invitation(self, invitation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        inv = self.state.invitations.get(invitation_id)
        if inv is None:
            raise DomainError(f"邀请不存在: {invitation_id}")
        if inv.state == "CANCELLED":
            raise DomainError("邀请已取消")
        return self._emit(invitation_id, "INVITATION_CANCELLED", {
            "reason": body.get("reason", ""),
        })

    # ------------------------------------------------------------- publications
    def draft_publication(self, body: dict[str, Any]) -> dict[str, Any]:
        agreement_id = body.get("agreement_version_id", "")
        self._get_agreement(agreement_id)
        for field_ in ("code", "title", "material_ref"):
            if not body.get(field_):
                raise DomainError(f"缺少字段: {field_}")
        if self.state.publication_by_code(body["code"]) is not None:
            raise DomainError(f"公布编号已存在: {body['code']}")
        for code in body.get("obligation_codes", []):
            if self.state.obligation_by_code(code) is None:
                raise DomainError(f"门控承诺不存在: {code}")
        publication_id = body.get("id") or _new_id("publication")
        return self._emit(publication_id, "PUBLICATION_DRAFTED", {
            "code": body["code"],
            "title": body["title"],
            "material_ref": body["material_ref"],
            "agreement_version_id": agreement_id,
            "obligation_codes": body.get("obligation_codes", []),
        })

    def _publication_gate_status(self, pub):
        gates = [
            g for g in self.state.gates_for_publication(pub)
            if self.state.agreements[g.agreement_id].state == "ACTIVE"
        ]
        blocked: list[str] = []
        for g in gates:
            status = self.state.obligation_state(g)
            if status not in domain.OPEN_OBLIGATION_STATES:
                blocked.append(f"{g.code}:{status}")
        return gates, blocked

    def approve_publication(self, publication_id: str, body: dict[str, Any]) -> dict[str, Any]:
        pub = self._get_publication(publication_id)
        if pub.state in ("PUBLISHED", "SUPERSEDED", "CANCELLED"):
            raise DomainError(f"公布版本当前状态 {pub.state}，不可批准")
        _, blocked = self._publication_gate_status(pub)
        if blocked:
            raise DomainError("门控承诺未全部满足/已过期，暂停批准：" + ", ".join(blocked))
        return self._emit(publication_id, "PUBLICATION_APPROVED_FOR_RELEASE", {
            "approved_by": body.get("approved_by", ""),
        })

    def publish_publication(self, publication_id: str, body: dict[str, Any]) -> dict[str, Any]:
        pub = self._get_publication(publication_id)
        if pub.state != "APPROVED_FOR_RELEASE":
            raise DomainError("公布版本须先经批准（且门控承诺全部满足）")
        if not body.get("published_ref"):
            raise DomainError("缺少字段: published_ref")
        _, blocked = self._publication_gate_status(pub)
        if blocked:
            raise DomainError("发布前复检发现门控失效：" + ", ".join(blocked))
        return self._emit(publication_id, "PUBLICATION_PUBLISHED", {
            "published_ref": body["published_ref"],
        })

    def supersede_publication(self, publication_id: str, body: dict[str, Any]) -> dict[str, Any]:
        pub = self._get_publication(publication_id)
        if pub.state not in ("PUBLISHED", "APPROVED_FOR_RELEASE", "HELD"):
            raise DomainError("只有已批准/已发布/暂停中的版本可被新版本取代")
        if not body.get("replaced_by"):
            raise DomainError("缺少字段: replaced_by")
        return self._emit(publication_id, "PUBLICATION_SUPERSEDED", {
            "replaced_by": body["replaced_by"],
        })

    def cancel_publication(self, publication_id: str, body: dict[str, Any]) -> dict[str, Any]:
        pub = self._get_publication(publication_id)
        if pub.state == "CANCELLED":
            raise DomainError("公布版本已取消")
        if pub.state == "PUBLISHED":
            raise DomainError("已发布记录不得删除或取消；如需更正请登记新版本取代，原发布记录继续可追溯")
        return self._emit(publication_id, "PUBLICATION_CANCELLED", {
            "reason": body.get("reason", ""),
        })

    # ------------------------------------------------------------ external acks
    def receive_ack(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """接收外部回执。返回本次追加的事件（可能多条）。

        - 按 ack_ref 去重：内容完全一致 -> 丢弃重复（仍留只读记录）。
        - 内容不一致 -> 全部转人工协调案件，系统不按到达顺序裁定真假。
        - 迟到的协议确认（指向已失效版本）只登记，不产生任何生效效果。
        """
        ack_ref = body.get("ack_ref", "")
        sender = body.get("sender_party_id", "")
        if not ack_ref or not sender:
            raise DomainError("缺少字段: ack_ref / sender_party_id")
        content = body.get("content", {})
        content_hash = canonical_hash(content)
        provided_hash = body.get("content_hash")
        if provided_hash and provided_hash != content_hash:
            raise DomainError("content_hash 与内容的规范哈希不一致")

        regarding = body.get("regarding", {}) or {}
        revival_note = self._check_revival(regarding)

        kept = self.state.acks_by_ref(ack_ref)
        kept = [a for a in kept if a.state in ("RECEIVED", "IN_CONFLICT", "RESOLVED")]
        open_case = next(
            (c for c in self.state.cases.values()
             if c.ack_ref == ack_ref and c.state == "OPEN"),
            None,
        )
        resolved_case = next(
            (c for c in self.state.cases.values()
             if c.ack_ref == ack_ref and c.state == "RESOLVED"),
            None,
        )
        matches_resolution = (
            resolved_case is not None and content_hash == resolved_case.chosen_content_hash
        )
        matching = [a for a in kept if a.content_hash == content_hash]
        ack_id = _new_id("external_ack")
        base_payload = {
            "ack_ref": ack_ref,
            "sender_party_id": sender,
            "content_hash": content_hash,
            "content": content,
            "regarding": regarding,
            "late_and_not_effective": bool(revival_note),
            "revival_note": revival_note or "",
        }

        # 完全一致的重复回执（任何一种）：
        #  - 该编号下所有登记内容一致（常规重发）；
        #  - 已在开放协调案件中、本次内容只是某个已登记变体的再次到达；
        #  - 案件已裁决，本次内容与裁决选定版本一致。
        # 以上都不改变任何事实，仅留只读丢弃记录；与裁决矛盾的新内容才另开新案。
        if kept and (
            len(matching) == len(kept)
            or (open_case is not None and matching)
            or matches_resolution
        ):
            discarded = self._emit(ack_id, "ACK_DUPLICATE_DISCARDED", base_payload)
            return [discarded]

        received = self._emit(ack_id, "ACK_RECEIVED", base_payload)
        if not kept:
            return [received]

        # 内容冲突：同一编号下所有不同版本都进协调案件，首个版本不自动为真，
        # 裁决后的矛盾重发则另开新案，不由系统按到达顺序消解。
        affected = sorted({a.id for a in kept} | {ack_id})
        case_id = open_case.id if open_case else _new_id("coordination_case")
        escalated = self._emit(case_id, "ACK_CONFLICT_ESCALATED", {
            "ack_ref": ack_ref,
            "case_id": case_id,
            "affected_ack_ids": affected,
            "ack_ids": affected,
            "reason": (
                f"同一外部编号 {ack_ref} 累计 {len(affected)} 份内容不一致的回执，"
                "到达顺序不决定真假，转人工协调"
            ),
        })
        return [received, escalated]

    def _check_revival(self, regarding: dict[str, Any]) -> str:
        kind = regarding.get("kind", "")
        if kind != "agreement_confirmation":
            return ""
        ag = None
        if regarding.get("agreement_version_id"):
            ag = self.state.agreements.get(regarding["agreement_version_id"])
        elif regarding.get("agreement_ref"):
            ag = self.state.agreement_by_ref(regarding["agreement_ref"])
        if ag is None:
            return ""
        if ag.state == "SUPERSEDED":
            return f"协议 {ag.ref} 已被 {ag.superseded_by} 取代，迟到确认仅登记不生效"
        if ag.state == "CANCELLED":
            return f"协议 {ag.ref} 已取消，迟到确认仅登记不生效"
        return ""

    def resolve_case(self, case_id: str, body: dict[str, Any]) -> dict[str, Any]:
        case = self.state.cases.get(case_id)
        if case is None:
            raise DomainError(f"协调案件不存在: {case_id}")
        if case.state == "RESOLVED":
            raise DomainError("案件已裁决；更正请再次发起协调")
        chosen = body.get("chosen_ack_id", "")
        if chosen not in case.ack_ids:
            raise DomainError("chosen_ack_id 必须是案件登记的回执之一")
        if not body.get("resolution") or not body.get("resolved_by"):
            raise DomainError("裁决必须写明 resolution 与 resolved_by")
        chosen_ack = self.state.acks[chosen]
        return self._emit(case_id, "COORDINATION_RESOLVED", {
            "resolution": body["resolution"],
            "chosen_ack_id": chosen,
            "chosen_content_hash": chosen_ack.content_hash,
            "resolved_by": body["resolved_by"],
            "note": body.get("note", "人工裁决；被取舍版本仍在事件流中可追溯"),
        })

    def reconcile_closure(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._emit(_new_id("closure"), "CLOSURE_RECONCILED", {
            "note": body.get("note", "闭幕对账"),
        })

    # --------------------------------------------------------------- export chain
    def responsibility_chain(self) -> dict[str, Any]:
        """导出责任链：每个箱件为何可移交/为何暂停、当前由谁保管、完整保管链。"""
        crates_out: list[dict[str, Any]] = []
        for crate in sorted(self.state.crates.values(), key=lambda c: c.code):
            gates = []
            for ob in sorted(self.state.gates_for_crate(crate), key=lambda o: o.code):
                agreement = self.state.agreements[ob.agreement_id]
                gates.append({
                    "obligation_code": ob.code,
                    "agreement_ref": agreement.ref,
                    "agreement_state": agreement.state,
                    "currently_gating": agreement.state == "ACTIVE",
                    "responsible_party_id": ob.responsible_party_id,
                    "deadline": ob.deadline,
                    "state": self.state.obligation_state(ob),
                    "required_evidence_kinds": ob.prerequisite_evidence_kinds,
                    "missing_evidence_kinds": ob.missing_kinds(),
                    "acceptable_alternatives": ob.acceptable_alternatives,
                    "signoffs": [
                        {
                            "evidence_kind": s.evidence_kind,
                            "via_alternative": s.via_alternative,
                            "evidence_ref": s.evidence_ref,
                            "submitted_by": s.submitted_by,
                            "sign_off_by": s.sign_off_by,
                            "at": s.at,
                        }
                        for s in ob.signoffs
                    ],
                })

            confirmations = {}
            for role in domain.ROLES:
                if role in crate.roles_not_applicable:
                    confirmations[role] = {
                        "applicable": False,
                        "reason": crate.roles_not_applicable[role],
                    }
                elif role in crate.confirmations:
                    record = crate.confirmations[role]
                    confirmations[role] = {
                        "applicable": True,
                        "confirmed": True,
                        "by": record["by"],
                        "at": record["at"],
                        "evidence_ref": record["evidence_ref"],
                    }
                else:
                    confirmations[role] = {"applicable": True, "confirmed": False}

            current = self.state.current_custodian(crate)
            holds = self.state.crate_hold_reasons(crate)
            status = self.state.crate_status(crate)
            if status == "READY_TO_HANDOVER":
                why = "三道职责（馆藏/保险/运输）均已由不同责任方确认，且全部关联承诺满足或已按替代方案解除，准予移交"
            elif status == "HOLD_PENDING_CONDITIONS":
                why = "暂停：" + "；".join(holds)
            elif crate.state == "RELEASED_TO_CARRIER":
                why = "已移交承运人，在途"
            elif crate.state == "DELIVERED":
                why = "已由目的地签收"
            elif crate.state == "RETURNED":
                why = "展览结束已退回"
            elif crate.state == "CANCELLED":
                why = f"已取消（{crate.cancel_reason}）；最后位置与保险责任均留痕"
            else:
                why = "集结中，尚缺职责确认：" + ", ".join(self.state.missing_roles(crate))

            crates_out.append({
                "crate_id": crate.id,
                "code": crate.code,
                "status": status,
                "why": why,
                "hold_reasons": holds,
                "objects": [
                    {"code": self.state.objects[oid].code,
                     "title": self.state.objects[oid].title,
                     "kind": self.state.objects[oid].kind}
                    for oid in crate.object_ids
                ],
                "role_confirmations": confirmations,
                "gating_obligations": gates,
                "current_custodian": (
                    None if current is None else {
                        "party_id": current.custodian_party_id,
                        "location": current.location,
                        "since": current.at,
                        "action": current.action,
                    }
                ),
                "custody_ledger": [
                    {
                        "at": e.at, "action": e.action,
                        "custodian_party_id": e.custodian_party_id,
                        "location": e.location, "note": e.note,
                    }
                    for e in crate.ledger
                ],
                "cancellation": (
                    None if crate.state != "CANCELLED" else {
                        "final_location": crate.current_location,
                        "reason": crate.cancel_reason,
                        "last_ledger_entry": {
                            "at": crate.ledger[-1].at,
                            "custodian_party_id": crate.ledger[-1].custodian_party_id,
                            "insurance_note": crate.ledger[-1].note,
                        },
                    }
                ),
            })

        publications_out = []
        for pub in sorted(self.state.publications.values(), key=lambda p: p.code):
            gates, blocked = self._publication_gate_status(pub)
            publications_out.append({
                "code": pub.code,
                "title": pub.title,
                "state": pub.state,
                "material_ref": pub.material_ref,
                "published_ref": pub.published_ref,
                "published_at": pub.published_at,
                "gating_obligations": [
                    {"code": g.code, "state": self.state.obligation_state(g)} for g in gates
                ],
                "blocked": blocked,
                "trace_note": (
                    "已发布记录即使后续取消/取代也保留于此" if pub.published_ref else ""
                ),
            })

        return {
            "generated_at": self.clock(),
            "agreements": [
                {
                    "id": a.id, "ref": a.ref, "title": a.title, "state": a.state,
                    "superseded_by": a.superseded_by, "cancelled_reason": a.cancelled_reason,
                }
                for a in sorted(self.state.agreements.values(), key=lambda a: a.created_at)
            ],
            "crates": crates_out,
            "publications": publications_out,
            "invitations": [
                {"code": i.code, "specialist_name": i.specialist_name,
                 "state": i.state, "reissue_count": i.reissue_count}
                for i in sorted(self.state.invitations.values(), key=lambda i: i.code)
            ],
            "open_coordination_cases": [
                {
                    "case_id": c.id, "ack_ref": c.ack_ref, "ack_ids": c.ack_ids,
                    "opened_at": c.opened_at,
                }
                for c in self.state.cases.values() if c.state == "OPEN"
            ],
        }
