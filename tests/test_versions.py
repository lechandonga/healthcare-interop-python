"""STU3/R4/R5 三版本：Condition 接入、严格字段判定、归一形态一致、引用方向规则。"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.parsing import parse_resource
from tests.conftest import log_case

log = logging.getLogger("test.versions")


def patient(rid, version="fhir-stu3"):
    return {"standard_version": version, "resourceType": "Patient", "id": rid,
            "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}


def observation(rid, subject, version):
    body = {"standard_version": version, "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "subject": {"reference": subject}}
    if version == "fhir-r5":
        body["effectivePeriod"] = {"start": "2026-09-22T10:00:00",
                                   "end": "2026-09-22T10:05:00"}
    else:  # stu3 与 r4 同为 effectiveDateTime
        body["effectiveDateTime"] = "2026-09-22T10:00:00"
    return body


def condition(rid, subject, version, *, code="I10", status="active"):
    return {"standard_version": version, "resourceType": "Condition", "id": rid,
            "clinicalStatus": {"coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                 "code": status}]},
            "verificationStatus": {"coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                 "code": "confirmed"}]},
            "code": {"coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10", "code": code}]},
            "subject": {"reference": subject},
            "onsetDateTime": "2026-09-01T08:00:00"}


# ---- STU3 接入与归一 ----

def test_stu3_observation_parses_and_normalizes_same_as_r4():
    stu3 = parse_resource(observation("o1", "Patient/p1", "fhir-stu3"))
    r4 = parse_resource(observation("o2", "Patient/p1", "fhir-r4"))
    log_case(log, "stu3-norm", {"stu3": stu3.payload["effective"], "r4": r4.payload["effective"]},
             "判定依据：STU3 与 R4 均归一为 {type,start,end}，下游不感知版本差异")
    assert stu3.standard_version.value == "fhir-stu3"
    assert stu3.payload["effective"] == r4.payload["effective"] == {
        "type": "dateTime", "start": "2026-09-22T10:00:00", "end": "2026-09-22T10:00:00"}
    assert stu3.references == ["Patient/p1"]


def test_condition_normalizes_identically_across_three_versions():
    c3 = parse_resource(condition("c1", "Patient/p1", "fhir-stu3"))
    c4 = parse_resource(condition("c2", "Patient/p1", "fhir-r4"))
    c5 = parse_resource(condition("c3", "Patient/p1", "fhir-r5"))
    log_case(log, "condition-norm", [c3.payload, c4.payload, c5.payload],
             "判定依据：三个版本 Condition 归一后键集合完全一致")
    shapes = [set(c.payload) for c in (c3, c4, c5)]
    assert shapes[0] == shapes[1] == shapes[2]
    for c in (c3, c4, c5):
        assert c.payload["clinical_status"]["code"] == "active"
        assert c.payload["verification_status"]["code"] == "confirmed"
        assert c.payload["code"]["code"] == "I10"
        assert c.references == ["Patient/p1"]
        assert c.payload["onset"] == "2026-09-01T08:00:00"


def test_stu3_unsupported_version_still_rejected():
    sample = {**patient("p1"), "standard_version": "fhir-r2"}
    log_case(log, "bad-version", sample, "判定依据：fhir-r2 不在 stu3/r4/r5 支持范围")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is RejectReason.UNSUPPORTED_STANDARD_VERSION


# ---- 严格字段判定：跨版本字段 vs 未知字段，原因必须可区分 ----

@pytest.mark.parametrize("sample,reason", [
    # R4 携带 R5 才有的 effectivePeriod
    (observation("o1", "Patient/p1", "fhir-r4")
     | {"effectivePeriod": {"start": "2026-09-22T10:00:00", "end": "2026-09-22T10:05:00"}},
     RejectReason.VERSION_FIELD_MISMATCH),
    # STU3 携带 R5 才有的 effectivePeriod（旧版本也要严）
    (observation("o1", "Patient/p1", "fhir-stu3")
     | {"effectivePeriod": {"start": "2026-09-22T10:00:00", "end": "2026-09-22T10:05:00"}},
     RejectReason.VERSION_FIELD_MISMATCH),
    # R5 携带 STU3/R4 的 effectiveDateTime
    (observation("o1", "Patient/p1", "fhir-r5")
     | {"effectiveDateTime": "2026-09-22T10:00:00"},
     RejectReason.VERSION_FIELD_MISMATCH),
    # 任意版本都没定义过的字段
    ({**observation("o1", "Patient/p1", "fhir-r4"), "note": [{"text": "x"}]},
     RejectReason.UNKNOWN_FIELD),
    # 老版本 R4 Patient 上出现未定义字段（不只新版本严）
    ({**patient("p1", "fhir-r4"), "maritalStatus": {"text": "未婚"}},
     RejectReason.UNKNOWN_FIELD),
    # STU3 Patient 上出现未定义字段
    ({**patient("p1", "fhir-stu3"), "extraThing": 1},
     RejectReason.UNKNOWN_FIELD),
    # Condition 上出现未定义字段
    ({**condition("c1", "Patient/p1", "fhir-r4"), "abatementDateTime": "2026-09-02T08:00:00"},
     RejectReason.UNKNOWN_FIELD),
])
def test_version_field_vs_unknown_field_are_distinguishable(sample, reason):
    log_case(log, "strict-fields", sample,
             f"判定依据：跨版本字段=version_field_mismatch，未定义字段=unknown_field，"
             f"本例应判 {reason.value}")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is reason
    assert reason in (RejectReason.VERSION_FIELD_MISMATCH, RejectReason.UNKNOWN_FIELD)
    assert ei.value.reason is not (
        RejectReason.UNKNOWN_FIELD if reason is RejectReason.VERSION_FIELD_MISMATCH
        else RejectReason.VERSION_FIELD_MISMATCH)


# ---- Condition 按各版本规矩严格校验 ----

@pytest.mark.parametrize("mutate,reason", [
    (lambda b: b.pop("clinicalStatus"), RejectReason.MISSING_REQUIRED_FIELD),
    (lambda b: b.pop("subject"), RejectReason.MISSING_REQUIRED_FIELD),
    (lambda b: b["clinicalStatus"]["coding"][0].__setitem__("code", "not-a-status"),
     RejectReason.INVALID_FIELD_TYPE),
    # relapse 是 R4/R5 合法值，但 STU3 取值集不允许
    (lambda b: b["clinicalStatus"]["coding"][0].__setitem__("code", "relapse"),
     RejectReason.INVALID_FIELD_TYPE),
    (lambda b: b.update(code=""), RejectReason.ILLEGAL_CODING),
    (lambda b: b.__setitem__("subject", {"reference": "Organization/org1"}),
     RejectReason.INVALID_FIELD_TYPE),
])
def test_condition_validation_uses_version_rules(mutate, reason):
    sample = condition("c1", "Patient/p1", "fhir-stu3")
    mutate(sample)
    log_case(log, "condition-reject", sample,
             f"判定依据：Condition 按声明版本严格校验，本例应拒绝 {reason.value}")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is reason


def test_r5_only_status_rejected_on_stu3_and_r4():
    # unknown 只在 R5 临床状态取值集中
    for version in ("fhir-stu3", "fhir-r4"):
        sample = condition("c1", "Patient/p1", version, status="unknown")
        with pytest.raises(InteropError) as ei:
            parse_resource(sample)
        log_case(log, "status-versions", sample,
                 f"判定依据：{version} 不允许 R5 取值 unknown")
        assert ei.value.reason is RejectReason.INVALID_FIELD_TYPE
    assert parse_resource(condition("c5", "Patient/p1", "fhir-r5", status="unknown"))


# ---- 引用方向：STU3<R4<R5 同规则 ----

def test_reference_direction_across_three_versions():
    from interop.service import InteropService
    from interop.errors import RejectReason as RR
    # R4 资源可以引用 STU3 患者（高版本 -> 低版本放行）
    svc = InteropService()
    svc.terminology.register_system(
        "http://hl7.org/fhir/sid/icd-10", "2016", {"I10": "Essential hypertension"})
    svc.ingest(patient("p-old", "fhir-stu3"))
    _, ver = svc.ingest(condition("c1", "Patient/p-old", "fhir-r4"))
    assert ver == 1
    # STU3 资源引用 R4 患者必须拒绝（低版本 -> 高版本）
    svc.ingest(patient("p-new", "fhir-r4"))
    with pytest.raises(InteropError) as ei:
        svc.ingest(condition("c2", "Patient/p-new", "fhir-stu3"))
    log_case(log, "stu3->r4", {"source": "fhir-stu3", "target": "fhir-r4"},
             "判定依据：低版本引用高版本 -> reference_version_mismatch")
    assert ei.value.reason is RR.REFERENCE_VERSION_MISMATCH
