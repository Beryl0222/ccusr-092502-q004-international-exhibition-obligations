#!/usr/bin/env python3
"""联调演示：在内存服务上走完一轮借展交付，并打印责任链结论。

运行：python3 scripts/demo.py
它不依赖网络与外部库；如需对真实 HTTP 服务联调，见 README。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eventstore import EventStore  # noqa: E402
from src.service import ObligationService  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    contract = json.loads((ROOT / "contracts" / "domain.json").read_text(encoding="utf-8"))
    svc = ObligationService(EventStore(None, set(contract["events"])))

    ag = svc.draft_agreement({
        "agreement_ref": "MOU-2026-1", "title": "古城联合展览备忘录",
        "parties": [{"id": "MUSEUM", "name": "文博机构"},
                    {"id": "CITIES", "name": "海外古城联盟"}],
    })
    aid = ag["aggregate_id"]
    svc.activate_agreement(aid)

    svc.accept_obligation({
        "code": "OBL-INS", "agreement_version_id": aid,
        "description": "海外段保险生效证明", "responsible_party_id": "CITIES",
        "deadline": "2026-10-20T18:00:00+02:00",
        "prerequisite_evidence_kinds": ["INSURANCE_CERTIFICATE"],
        "acceptable_alternatives": {"INSURANCE_CERTIFICATE": "CONDITION_REPORT"},
        "crate_codes": ["CRATE-A"], "publication_codes": ["PUB-OPEN"],
    })
    svc.accept_obligation({
        "code": "OBL-RIGHTS", "agreement_version_id": aid,
        "description": "数字复制品权利许可", "responsible_party_id": "MUSEUM",
        "deadline": "2026-10-25T18:00:00+08:00",
        "prerequisite_evidence_kinds": ["RIGHTS_LICENSE", "STATUS_REPORT"],
        "crate_codes": ["CRATE-A"],
    })

    crate = svc.assemble_crate({
        "code": "CRATE-A", "origin_location": "本馆一号库房", "origin_party_id": "MUSEUM",
    })
    cid = crate["aggregate_id"]
    svc.list_object({"code": "OBJ-12", "title": "唐三彩马", "kind": "PHYSICAL",
                     "agreement_version_id": aid, "owner_party_id": "MUSEUM"})
    svc.pack_object(cid, {"object_code": "OBJ-12"})

    # 三道职责分别由不同责任方确认
    svc.confirm_crate_role(cid, "COLLECTION", {"confirmer_party_id": "MUSEUM-REGISTRY",
                                               "evidence_ref": "COL-77"})
    svc.confirm_crate_role(cid, "INSURANCE", {"confirmer_party_id": "INSCO-EU",
                                              "evidence_ref": "POL-902"})
    svc.confirm_crate_role(cid, "TRANSPORT", {"confirmer_party_id": "FREIGHTCO",
                                              "evidence_ref": "BKG-55"})

    # 前置证据：常规签收 + 替代证据（状况报告替代保险证明）
    rights = svc.state.obligation_by_code("OBL-RIGHTS")
    svc.sign_evidence(rights.id, {"evidence_kind": "RIGHTS_LICENSE", "evidence_ref": "LIC-3",
                                  "submitted_by": "MUSEUM-LEGAL", "sign_off_by": "CITIES-IP"},
                      alternative=False)
    svc.sign_evidence(rights.id, {"evidence_kind": "STATUS_REPORT", "evidence_ref": "SR-8",
                                  "submitted_by": "MUSEUM-REGISTRY", "sign_off_by": "CITIES-REG"},
                      alternative=False)
    ins = svc.state.obligation_by_code("OBL-INS")
    svc.sign_evidence(ins.id, {"satisfies_kind": "INSURANCE_CERTIFICATE",
                               "alternative_evidence_kind": "CONDITION_REPORT",
                               "evidence_ref": "CR-22", "submitted_by": "CITIES",
                               "sign_off_by": "MUSEUM-CONSERV"}, alternative=True)

    svc.release_crate(cid, {"carrier_party_id": "FREIGHTCO"})
    svc.deliver_crate(cid, {"location": "海外古城联盟主展馆 A 厅",
                            "received_by_party_id": "CITIES-VENUE"})

    chain = svc.responsibility_chain()
    view = next(c for c in chain["crates"] if c["code"] == "CRATE-A")
    print("=" * 72)
    print(f"箱件 {view['code']}（{view['objects'][0]['title']}）状态：{view['status']}")
    print(f"结论：{view['why']}")
    print(f"当前保管：{view['current_custodian']['party_id']} @ {view['current_custodian']['location']}")
    print("-" * 72)
    print("三道职责确认：")
    for role, info in view["role_confirmations"].items():
        if not info.get("applicable", True):
            print(f"  - {role}: 不适用（{info['reason']}）")
        elif info.get("confirmed"):
            print(f"  - {role}: 已确认 by {info['by']} at {info['at']}")
        else:
            print(f"  - {role}: 未确认")
    print("门控承诺：")
    for g in view["gating_obligations"]:
        alt = "（含替代证据）" if any(s["via_alternative"] for s in g["signoffs"]) else ""
        print(f"  - {g['obligation_code']} [{g['state']}]{alt} "
              f"责任方={g['responsible_party_id']} 缺证={g['missing_evidence_kinds']}")
    print("保管链：")
    for e in view["custody_ledger"]:
        print(f"  {e['at']}  {e['action']:22s} {e['custodian_party_id']:18s} {e['location']}")
    print("=" * 72)
    print(f"事件总数：{len(svc.store)}（全部只追加，可重放）")


if __name__ == "__main__":
    main()
