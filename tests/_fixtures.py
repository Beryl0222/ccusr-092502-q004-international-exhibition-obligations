"""测试共用的引导夹具。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api import build_service
from src.storage import InMemoryEventStore

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class Scenario:
    """可变时钟 + 已登记 V1 与三项闸门承诺的标准场景。"""

    def __init__(self) -> None:
        self.store = InMemoryEventStore()
        self.service = build_service(self.store)
        self.t = T0
        self.service.clock = self.now

    def now(self) -> datetime:
        return self.t

    def advance(self, **kwargs: int) -> datetime:
        self.t += timedelta(**kwargs)
        return self.t

    def iso(self, dt: datetime | None = None) -> str:
        return (dt or self.t).isoformat()

    def bootstrap_version(self, version_ref: str = "V1", *,
                          insurance_alternative: bool = True) -> None:
        self.service.record_version(version_ref, title=f"备忘录 {version_ref}",
                                    at=self.t)
        self.service.define_obligation(
            version_ref, "OBL-COL", "文博机构馆藏部", self.t + timedelta(days=10),
            title="状况报告", role="COLLECTION", at=self.t)
        alt = [{"code": "ALT-BINDER", "description": "临时承保 + 7 日补正"}] \
            if insurance_alternative else None
        self.service.define_obligation(
            version_ref, "OBL-INS", "联盟保险经纪人", self.t + timedelta(days=12),
            title="门到门保险", role="INSURANCE", alternatives=alt, at=self.t)
        self.service.define_obligation(
            version_ref, "OBL-TRN", "国际承运商", self.t + timedelta(days=14),
            title="运输安排", role="TRANSPORT", at=self.t)

    def list_crate(self, code: str = "CR-1", version_ref: str = "V1"):
        return self.service.list_crate(
            code, version_ref,
            {"COLLECTION": "OBL-COL", "INSURANCE": "OBL-INS",
             "TRANSPORT": "OBL-TRN"},
            description=f"箱件 {code}", at=self.t)

    def accept_all(self) -> None:
        for code in ("OBL-COL", "OBL-INS", "OBL-TRN"):
            self.service.accept_obligation(code, at=self.t)

    def fulfill(self, code: str, evidence_ref: str, submitter: str,
                reviewer: str = "复核人 陈洁", *, alternative: str | None = None):
        self.service.submit_evidence(
            code, evidence_ref, submitter, alternative_code=alternative, at=self.t)
        self.service.review_evidence(
            code, evidence_ref, reviewer, True, at=self.t)

    def fulfill_gates(self) -> None:
        self.fulfill("OBL-COL", "EV-COL", "馆藏员 王敏")
        self.fulfill("OBL-INS", "EV-INS", "保险经纪 周祺")
        self.fulfill("OBL-TRN", "EV-TRN", "运输代理 赵铎")

    def sign_all(self, crate: str = "CR-1") -> None:
        self.service.sign_gate(crate, "COLLECTION", "馆藏主管", at=self.t)
        self.service.sign_gate(crate, "INSURANCE", "保险主管", at=self.t)
        self.service.sign_gate(crate, "TRANSPORT", "运输主管", at=self.t)

    def release_ready_crate(self, crate: str = "CR-1"):
        self.list_crate(crate)
        self.accept_all()
        self.fulfill_gates()
        self.sign_all(crate)
        return self.service.release_crate(crate, at=self.t)
