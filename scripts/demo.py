#!/usr/bin/env python3
"""端到端联调演示：经 API 层提交与签收承诺，最终导出责任链。

剧情：
1. 文博机构与海外古城联盟登记协议版本 V1，拆分借展条件、保险、运输、
   权利许可等承诺（含责任方、期限、前置证据、替代方案）。
2. 箱件 CR-01 经馆藏/保险/运输三职责分别确认后放行并交接；
   证据提交人不得复核自己的证据。
3. 外部回执按外部编号去重；内容冲突转人工协调，不以到达顺序定真假。
4. V2 取代 V1；对方时区下迟到的 V1 确认不能复活旧版本。
5. 承诺过期只暂停关联箱件 CR-02 与宣传素材，CR-01 不受影响；
   按登记的替代方案补证后恢复。
6. 取消后实体位置、保险责任与公布记录仍可追溯；导出说明每个箱件
   为何可移交、当前由谁保管。

用法： python3 scripts/demo.py [事件日志.jsonl] [导出文件.json]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import build_service, dispatch  # noqa: E402
from src.storage import JsonlEventStore  # noqa: E402

CST = timezone(timedelta(hours=8))  # 联络处所在地
LOCAL = timezone(timedelta(hours=-5))  # 对方联盟所在地（迟到确认来源）

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=CST)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def advance(self, **kwargs: int) -> datetime:
        self.t += timedelta(**kwargs)
        return self.t

    def __call__(self) -> datetime:
        return self.t


def call(service, method: str, path: str, body: dict | None = None):
    status, payload = dispatch(service, method, path, body)
    if status >= 400:
        raise SystemExit(f"API 调用失败 {status} {method} {path}: {payload}")
    print(f"  {method} {path} -> {status}")
    return payload


def main() -> None:
    journal = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "demo_journal.jsonl"
    export_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "chain_export.json"
    if journal.exists():
        journal.unlink()
    service = build_service(JsonlEventStore(journal))
    clock = Clock(T0)
    service.clock = clock

    print("== 1. 登记协议版本与承诺 ==")
    call(service, "POST", "/versions", {
        "version_ref": "V1", "title": "国际联展合作备忘录 V1",
        "at": clock.t.isoformat(),
    })
    # 借展状态报告（馆藏职责），前置：目录已确认；期限 10 天
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-CAT", "role": "COLLECTION",
        "title": "借展目录与状态报告", "responsible_party": "文博机构馆藏部",
        "deadline": (clock.t + timedelta(days=7)).isoformat(),
    })
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-COL", "role": "COLLECTION",
        "title": "借展物件状况报告", "responsible_party": "文博机构馆藏部",
        "deadline": (clock.t + timedelta(days=10)).isoformat(),
        "prerequisite_codes": ["OBL-CAT"],
    })
    # 保险承诺：过期可用“临时保单 + 7 日内补正”的替代方案
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-INS", "role": "INSURANCE",
        "title": "门到门保险生效", "responsible_party": "联盟指定保险经纪人",
        "deadline": (clock.t + timedelta(days=12)).isoformat(),
        "alternatives": [
            {"code": "ALT-BINDER", "description": "临时承保 binder + 7 日内补正正式保单"},
        ],
    })
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-TRN", "role": "TRANSPORT",
        "title": "专业艺术品运输安排", "responsible_party": "国际运输承运商",
        "deadline": (clock.t + timedelta(days=14)).isoformat(),
    })
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-RIGHTS",
        "title": "数字复制品与宣传素材权利许可", "responsible_party": "联盟宣传部",
        "deadline": (clock.t + timedelta(days=14)).isoformat(),
    })
    # 出境许可：如正式批件逾期，可凭对方海关电子担保受理替代
    call(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-CUST", "role": "TRANSPORT",
        "title": "文物出境许可批件", "responsible_party": "文博机构合规处",
        "deadline": (clock.t + timedelta(days=12)).isoformat(),
        "alternatives": [
            {"code": "ALT-EBOND", "description": "对方海关电子担保函，14 日内补正正式批件"},
        ],
    })

    print("== 2. 登记两只箱件 ==")
    for crate in ("CR-01", "CR-02"):
        call(service, "POST", "/crates", {
            "code": crate, "version_ref": "V1",
            "description": f"青铜器借展箱件 {crate}",
            "gates": {"COLLECTION": "OBL-COL", "INSURANCE": "OBL-INS",
                      "TRANSPORT": "OBL-TRN"},
        })
    call(service, "POST", "/crates/CR-02/link", {"obligation_code": "OBL-CUST"})

    print("== 3. 外部回执：对方接受承诺；同编号重复送达幂等去重 ==")
    ack_body = {
        "external_ref": "ACK-7781", "sender": "古城联盟秘书处",
        "content": {"decision": "ACCEPT", "obligation": "OBL-CAT"},
        "declared_version_ref": "V1",
        "received_at": clock.t.isoformat(),
    }
    call(service, "POST", "/acks", ack_body)
    call(service, "POST", "/acks", ack_body)  # 跨时区重发，内容一致
    call(service, "POST", "/obligations/OBL-CAT/accept",
         {"confirming_ack_ref": "ACK-7781"})

    print("== 4. 提交并复核证据（提交人与复核人必须不同） ==")
    call(service, "POST", "/obligations/OBL-CAT/evidence", {
        "evidence_ref": "EV-CAT-1", "submitter": "登记员 王敏",
        "kind": "CATALOG_REPORT",
    })
    call(service, "POST", "/obligations/OBL-CAT/evidence/review", {
        "evidence_ref": "EV-CAT-1", "reviewer": "联络员 陈洁",
        "accepted": True, "note": "目录与状态基线一致",
    })
    # 其余承诺由对方接受
    for code in ("OBL-COL", "OBL-INS", "OBL-TRN", "OBL-RIGHTS", "OBL-CUST"):
        call(service, "POST", f"/obligations/{code}/accept", {})
    call(service, "POST", "/obligations/OBL-COL/evidence", {
        "evidence_ref": "EV-COL-1", "submitter": "修复师 林澜",
        "kind": "CONDITION_REPORT",
    })
    call(service, "POST", "/obligations/OBL-COL/evidence/review", {
        "evidence_ref": "EV-COL-1", "reviewer": "联络员 陈洁",
        "accepted": True,
    })
    call(service, "POST", "/obligations/OBL-TRN/evidence", {
        "evidence_ref": "EV-TRN-1", "submitter": "运输代理 赵铎",
        "kind": "TRANSPORT_PLAN",
    })
    call(service, "POST", "/obligations/OBL-TRN/evidence/review", {
        "evidence_ref": "EV-TRN-1", "reviewer": "联络员 陈洁",
        "accepted": True,
    })

    print("== 5. 保险证据：首次被驳回，补正后通过 ==")
    call(service, "POST", "/obligations/OBL-INS/evidence", {
        "evidence_ref": "EV-INS-1", "submitter": "保险经纪 周祺",
        "kind": "POLICY_DRAFT",
    })
    status, rejected = dispatch(service, "POST",
                                "/obligations/OBL-INS/evidence/review", {
                                    "evidence_ref": "EV-INS-1",
                                    "reviewer": "联络员 陈洁",
                                    "accepted": False,
                                    "note": "保单金额不足"})
    print(f"  复核驳回 -> {status}（保单金额不足，承诺仍未满足）")
    call(service, "POST", "/obligations/OBL-INS/evidence", {
        "evidence_ref": "EV-INS-2", "submitter": "保险经纪 周祺",
        "kind": "POLICY_FINAL",
    })
    call(service, "POST", "/obligations/OBL-INS/evidence/review", {
        "evidence_ref": "EV-INS-2", "reviewer": "联络员 陈洁",
        "accepted": True,
    })

    print("== 6. 三职责分别确认，CR-01 放行并跨保险责任段交接 ==")
    for role, signer in (("COLLECTION", "馆藏主管 高岩"),
                         ("INSURANCE", "保险主管 孟真"),
                         ("TRANSPORT", "运输主管 韩启")):
        call(service, "POST", "/crates/CR-01/gates/sign",
             {"role": role, "signer": signer})
    # 少一个职责确认都不能放行（此处 CR-01 已三签，应成功；CR-02 未签应失败）
    status, blocked = dispatch(service, "POST", "/crates/CR-02/release", {})
    print(f"  CR-02 未齐三签尝试放行 -> {status} {blocked['error']}")
    call(service, "POST", "/crates/CR-01/release", {})
    call(service, "POST", "/crates/CR-01/handovers", {
        "receiver": "承运商押运组", "location": "文博机构出库月台",
        "insurance_party": "联盟指定保险经纪人",
    })
    clock.advance(days=2)
    call(service, "POST", "/crates/CR-01/handovers", {
        "receiver": "古城联盟场馆组", "location": "联盟展厅入库区",
        "insurance_party": "联盟场馆自保单元", "final": True,
        "at": clock.t.isoformat(),
    })

    print("== 7. 回执内容冲突：转人工协调，先到内容不被后到覆盖 ==")
    call(service, "POST", "/acks", {
        "external_ref": "ACK-9002", "sender": "联盟秘书 A",
        "content": {"press_release": "APPROVED", "caption": "古城联展"},
        "declared_version_ref": "V1",
        "received_at": (clock.t - timedelta(hours=3)).isoformat(),
        "arrival_at": (clock.t - timedelta(hours=3)).isoformat(),
    })
    call(service, "POST", "/acks", {
        "external_ref": "ACK-9002", "sender": "联盟秘书 B",
        "content": {"press_release": "WITHHOLD", "caption": "古城联展"},
        "received_at": (clock.t - timedelta(hours=2)).isoformat(),
        "arrival_at": clock.t.isoformat(),
    })
    call(service, "POST", "/acks/ACK-9002/resolve", {
        "resolution": "FIRST", "coordinator": "项目联络处 陈洁",
        "note": "与对方电话核实，以先签发的批准件为准",
    })
    call(service, "POST", "/obligations/OBL-RIGHTS/evidence", {
        "evidence_ref": "EV-RGT-1", "submitter": "宣传专员 孙宁",
        "kind": "LICENSE_PACK",
    })
    call(service, "POST", "/obligations/OBL-RIGHTS/evidence/review", {
        "evidence_ref": "EV-RGT-1", "reviewer": "联络员 陈洁",
        "accepted": True,
    })
    call(service, "POST", "/publications", {
        "version_ref": "V1", "material_ref": "PRESS-KIT-1",
        "channel": "双方官网与社交媒体",
        "obligation_codes": ["OBL-RIGHTS", "OBL-CUST"],
    })

    print("== 8. 承诺过期：仅暂停关联箱件 CR-02 与宣传素材，CR-01 已交付不受影响 ==")
    clock.advance(days=11)  # 第 13 天，OBL-CUST（期限 12 天）已过期
    call(service, "POST", "/maintenance/sweep-expirations", {})
    chain = service.export_chain()
    cr01 = next(c for c in chain["crates"] if c["code"] == "CR-01")
    cr02 = next(c for c in chain["crates"] if c["code"] == "CR-02")
    print(f"  CR-01 状态={cr01['status']}（已完成交接，不受 OBL-CUST 过期影响）")
    print(f"  CR-02 状态={cr02['status']}，暂停原因={cr02['suspensions']}")
    status, blocked = dispatch(service, "POST", "/crates/CR-02/release", {})
    print(f"  暂停期间尝试放行 CR-02 -> {status} {blocked['error']}")

    print("== 9. 按登记替代方案补出境担保并复核，CR-02 与宣传素材解除暂停 ==")
    call(service, "POST", "/obligations/OBL-CUST/evidence", {
        "evidence_ref": "EV-CUST-ALT", "submitter": "合规专员 何巍",
        "kind": "CUSTOMS_EBOND", "alternative_code": "ALT-EBOND",
    })
    call(service, "POST", "/obligations/OBL-CUST/evidence/review", {
        "evidence_ref": "EV-CUST-ALT", "reviewer": "联络员 陈洁",
        "accepted": True, "note": "对方海关电子担保有效，接受为替代履行",
    })
    for role, signer in (("COLLECTION", "馆藏主管 高岩"),
                         ("INSURANCE", "保险主管 孟真"),
                         ("TRANSPORT", "运输主管 韩启")):
        call(service, "POST", "/crates/CR-02/gates/sign",
             {"role": role, "signer": signer})
    call(service, "POST", "/crates/CR-02/release", {})
    call(service, "POST", "/crates/CR-02/handovers", {
        "receiver": "承运商押运组", "location": "文博机构出库月台",
        "insurance_party": "联盟指定保险经纪人",
    })
    clock.advance(days=2)
    call(service, "POST", "/crates/CR-02/handovers", {
        "receiver": "古城联盟场馆组", "location": "联盟展厅入库区",
        "insurance_party": "联盟场馆自保单元", "final": True,
        "at": clock.t.isoformat(),
    })

    print("== 10. V2 取代 V1；对方时区迟到的 V1 确认不得复活旧版本 ==")
    clock.advance(days=1)
    call(service, "POST", "/versions", {
        "version_ref": "V2", "title": "国际联展合作备忘录 V2（撤换一件展品）",
        "supersedes": "V1", "at": clock.t.isoformat(),
    })
    late = call(service, "POST", "/acks", {
        "external_ref": "ACK-LATE-V1", "sender": "古城联盟秘书处（夜班补发）",
        "content": {"decision": "ACCEPT", "obligation": "OBL-CAT", "note": "迟到 3 周"},
        "declared_version_ref": "V1",
        "received_at": datetime(2026, 9, 20, 22, 0, tzinfo=LOCAL).isoformat(),
        "arrival_at": clock.t.isoformat(),
    })
    print(f"  迟到回执照常登记（state={late['state']}），但不改变任何版本状态")
    status, stale = dispatch(service, "POST", "/obligations", {
        "version_ref": "V1", "code": "OBL-LATE",
        "responsible_party": "联盟秘书处",
        "deadline": (clock.t + timedelta(days=5)).isoformat(),
    })
    print(f"  在已失效 V1 上新设承诺 -> {status} {stale['error']}")

    print("== 11. 结项核对并导出责任链 ==")
    call(service, "POST", "/versions/V1/closure", {})
    chain = service.export_chain()
    export_path.write_text(json.dumps(chain, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    for crate in chain["crates"]:
        print(f"  {crate['code']}: 状态={crate['status']} "
              f"保管={crate['current_custodian']} @ {crate['current_location']} "
              f"保险责任={crate['insurance_party']}")
    press = chain["publications"][0]
    print(f"  {press['material_ref']}: 暂停={press['suspended']}（替代履行后已恢复）")
    print(f"  人工协调队列: {chain['manual_queue']}")
    print(f"  V1 状态: {next(v for v in chain['versions'] if v['ref'] == 'V1')['status']}")
    print(f"\n事件日志: {journal}（{len(service.store)} 条事件）")
    print(f"责任链导出: {export_path}")


if __name__ == "__main__":
    main()
