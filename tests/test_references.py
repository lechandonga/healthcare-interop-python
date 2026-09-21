"""引用完整性与版本兼容：断裂引用与版本错配必须显式拒绝。"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.references")


def patient(rid, version="fhir-r4"):
    return {"standard_version": version, "resourceType": "Patient", "id": rid,
            "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}


def observation(rid, subject, version="fhir-r4"):
    body = {"standard_version": version, "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "subject": {"reference": subject}}
    if version == "fhir-r4":
        body["effectiveDateTime"] = "2026-09-22T10:00:00"
    else:
        body["effectivePeriod"] = {"start": "2026-09-22T10:00:00",
                                   "end": "2026-09-22T10:05:00"}
    return body


def test_broken_reference_rejected():
    svc = InteropService()
    sample = observation("o1", "Patient/ghost")
    log_case(log, "broken-ref", sample, "引用目标 Patient/ghost 不存在，应拒绝 BROKEN_REFERENCE")
    with pytest.raises(InteropError) as ei:
        svc.ingest(sample)
    assert ei.value.reason is RejectReason.BROKEN_REFERENCE
    # 失败不得残留
    with pytest.raises(InteropError) as ei2:
        svc.store.get("Observation", "o1")
    assert ei2.value.reason is RejectReason.RESOURCE_NOT_FOUND


def test_r4_cannot_reference_r5():
    svc = InteropService()
    svc.ingest(patient("p1", version="fhir-r5"))
    sample = observation("o1", "Patient/p1", version="fhir-r4")
    log_case(log, "r4->r5", sample, "旧版本 R4 资源引用新版本 R5 患者，应拒绝 REFERENCE_VERSION_MISMATCH")
    with pytest.raises(InteropError) as ei:
        svc.ingest(sample)
    assert ei.value.reason is RejectReason.REFERENCE_VERSION_MISMATCH


def test_r5_may_reference_r4():
    svc = InteropService()
    svc.ingest(patient("p1", version="fhir-r4"))
    sample = observation("o1", "Patient/p1", version="fhir-r5")
    log_case(log, "r5->r4", sample, "新版本 R5 引用旧版本 R4 患者，兼容矩阵允许，应成功")
    _, version = svc.ingest(sample)
    assert version == 1


def test_unknown_code_rejected_before_storage():
    svc = InteropService()
    svc.ingest(patient("p1"))
    sample = observation("o1", "Patient/p1")
    sample["code"] = {"coding": [{"system": "http://loinc.org", "code": "00000-0"}]}
    log_case(log, "unknown-code", sample, "代码不在本地码表，应拒绝 UNKNOWN_CODE 且不入库")
    with pytest.raises(InteropError) as ei:
        svc.ingest(sample)
    assert ei.value.reason is RejectReason.UNKNOWN_CODE
    with pytest.raises(InteropError):
        svc.store.get("Observation", "o1")
