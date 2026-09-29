"""字段严格性：版本错位字段与未知字段分别拒绝、原因可区分；STU3/Condition 接入。

三个版本（stu3/r4/r5）一视同仁，不因新版本或新资源放宽标准。
"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.parsing import parse_resource
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.fields")


def patient(rid="p1", version="fhir-r4", **extra):
    body = {"standard_version": version, "resourceType": "Patient", "id": rid,
            "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}
    body.update(extra)
    return body


def observation(rid="o1", version="fhir-r4", **extra):
    body = {"standard_version": version, "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "subject": {"reference": "Patient/p1"}}
    if version == "fhir-r5":
        body["effectivePeriod"] = {"start": "2026-09-22T10:00:00",
                                   "end": "2026-09-22T10:05:00"}
    else:
        body["effectiveDateTime"] = "2026-09-22T10:00:00"
    body.update(extra)
    return body


def condition(rid="c1", version="fhir-r4", **extra):
    if version == "fhir-stu3":
        clinical = "active"
        verification = "confirmed"
    else:
        clinical = {"coding": [
            {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
             "code": "active"}]}
        verification = None
    body = {"standard_version": version, "resourceType": "Condition", "id": rid,
            "clinicalStatus": clinical,
            "code": {"coding": [{"system": "http://local.example/diagnosis", "code": "I10"}]},
            "subject": {"reference": "Patient/p1"}}
    if verification is not None:
        body["verificationStatus"] = verification
    body.update(extra)
    return body


@pytest.mark.parametrize("version,sample,reason,field", [
    # 旧版本同样严格：R4 Patient 带 R5 才有的 contact
    ("fhir-r4", patient(contact=[{"name": "x"}]),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "contact"),
    # R4 Patient 带服务任何版本都未定义的字段
    ("fhir-r4", patient(ssn="123"),
     RejectReason.UNKNOWN_FIELD, "ssn"),
    # STU3 Patient 携带 R4 才有的 maritalStatus
    ("fhir-stu3", patient(version="fhir-stu3", maritalStatus={"text": "M"}),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "maritalStatus"),
    # STU3 携带完全未知字段（新版本也不能当没看见）
    ("fhir-stu3", patient(version="fhir-stu3", ghost=1),
     RejectReason.UNKNOWN_FIELD, "ghost"),
    # R5 携带 R4/STU3 才有的字段
    ("fhir-r5", observation(version="fhir-r5",
                            effectiveDateTime="2026-09-22T10:00:00"),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "effectiveDateTime"),
    # R4 Observation 携带 R5 才有的 instantiates
    ("fhir-r4", observation(instantiates="x"),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "instantiates"),
    # STU3 Observation 携带 R4 才有的完全未知 R5 字段 instantiates
    ("fhir-stu3", observation(version="fhir-stu3", instantiates="x"),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "instantiates"),
    # Observation 带任何版本都未定义的字段
    ("fhir-r4", observation(extraField=[{"v": 1}]),
     RejectReason.UNKNOWN_FIELD, "extraField"),
    # STU3 Condition 带 R4 才有的 recordedDate
    ("fhir-stu3", condition(version="fhir-stu3", recordedDate="2026-03-01"),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "recordedDate"),
    # STU3 Condition 带 R4 才有的 onsetPeriod
    ("fhir-stu3", condition(version="fhir-stu3",
                            onsetPeriod={"start": "2026-01-01T00:00:00",
                                         "end": "2026-02-01T00:00:00"}),
     RejectReason.FIELD_NOT_DEFINED_FOR_VERSION, "onsetPeriod"),
    # R4 Condition 携带完全未定义字段
    ("fhir-r4", condition(severityScore=9),
     RejectReason.UNKNOWN_FIELD, "severityScore"),
])
def test_cross_version_and_unknown_fields_are_distinguishable(version, sample, reason, field):
    log_case(log, f"strict-field/{version}/{field}", sample,
             f"应拒绝为 {reason.value} 且定位字段 {field}")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is reason
    assert ei.value.field == field


def test_two_field_reasons_are_distinct_categories():
    """同一资源上版本错位与未知字段必须是两种可区分原因。"""
    log_case(log, "reason-distinct",
             {"version_mismatch": "recordedDate@stu3", "unknown": "severityScore@r4"},
             "FIELD_NOT_DEFINED_FOR_VERSION 与 UNKNOWN_FIELD 枚举互不相等")
    assert RejectReason.FIELD_NOT_DEFINED_FOR_VERSION is not RejectReason.UNKNOWN_FIELD


def test_stu3_r4_r5_condition_normalize_to_same_shape():
    """三个版本的 Condition 归一为同一内部形态，下游不感知版本差异。"""
    stu3 = parse_resource(condition(rid="c1", version="fhir-stu3",
                                    onsetDateTime="2026-01-01T00:00:00"))
    r4 = parse_resource(condition(rid="c2", version="fhir-r4",
                                  onsetPeriod={"start": "2026-01-01T00:00:00",
                                               "end": "2026-02-01T00:00:00"},
                                  recordedDate="2026-03-01"))
    r5 = parse_resource(condition(rid="c3", version="fhir-r5"))
    log_case(log, "condition-normalize",
             {"keys_stu3": sorted(stu3.payload), "keys_r4": sorted(r4.payload),
              "keys_r5": sorted(r5.payload)},
             "三个版本 payload 同构：clinicalStatus/verificationStatus/code/onset/recordedDate")
    for resource in (stu3, r4, r5):
        assert set(resource.payload) == {
            "clinicalStatus", "verificationStatus", "code", "onset", "recordedDate"}
        assert resource.payload["clinicalStatus"] == "active"
        assert resource.references == ["Patient/p1"]
    assert stu3.payload["onset"]["type"] == "dateTime"
    assert r4.payload["onset"]["type"] == "Period"


@pytest.mark.parametrize("version,status,reason", [
    ("fhir-stu3", "active", None),
    ("fhir-stu3", {"coding": [{"code": "active"}]}, RejectReason.INVALID_FIELD_TYPE),
    ("fhir-r4", {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                             "code": "active"}]}, None),
    ("fhir-r4", "active", RejectReason.INVALID_FIELD_TYPE),
    ("fhir-r4", {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                             "code": "bogus"}]}, RejectReason.INVALID_FIELD_TYPE),
])
def test_condition_clinical_status_typed_per_version(version, status, reason):
    sample = condition(rid="cx", version=version, clinicalStatus=status)
    log_case(log, "clinical-status", sample,
             f"STU3 为 code 字符串、R4/R5 为 CodeableConcept；预期={reason}")
    if reason is None:
        resource = parse_resource(sample)
        assert resource.payload["clinicalStatus"] == "active"
    else:
        with pytest.raises(InteropError) as ei:
            parse_resource(sample)
        assert ei.value.reason is reason


def test_condition_subject_must_point_to_patient():
    sample = {"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
              "status": "final",
              "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
              "effectiveDateTime": "2026-09-22T10:00:00",
              "subject": {"reference": "Condition/c0"}}
    log_case(log, "cond-subject", sample,
             "Condition.subject 指向非 Patient 应拒绝 INVALID_FIELD_TYPE")
    with pytest.raises(InteropError) as ei:
        parse_resource(condition(subject={"reference": "Observation/o1"}))
    assert ei.value.reason is RejectReason.INVALID_FIELD_TYPE


def test_stu3_reference_direction_rules():
    """版本序 stu3 < r4 < r5：高引低放行，低引高拒绝。"""
    svc = InteropService()
    svc.ingest(patient("p1", version="fhir-r4"))
    # STU3 -> R4：低引高，拒绝
    sample_stu3 = condition(version="fhir-stu3", subject={"reference": "Patient/p1"})
    log_case(log, "stu3->r4", sample_stu3, "STU3 引用 R4 患者应拒绝 REFERENCE_VERSION_MISMATCH")
    with pytest.raises(InteropError) as ei:
        svc.ingest(sample_stu3)
    assert ei.value.reason is RejectReason.REFERENCE_VERSION_MISMATCH

    svc.ingest(patient("p2", version="fhir-stu3"))
    # R4 -> STU3：高引低，放行
    sample_r4 = condition(rid="c2", subject={"reference": "Patient/p2"})
    log_case(log, "r4->stu3", sample_r4, "R4 Condition 引用 STU3 患者应放行并入库")
    _, version = svc.ingest(sample_r4)
    assert version == 1


def test_condition_unknown_code_and_missing_fields_rejected():
    """新资源不放宽标准：未知代码、缺必填、编码结构非法照旧拒绝。"""
    svc = InteropService()
    svc.ingest(patient())
    bad_code = condition(code={"coding": [{"system": "http://local.example/diagnosis",
                                           "code": "Z99"}]})
    log_case(log, "cond-unknown-code", bad_code, "诊断码不在本地码表应拒绝 UNKNOWN_CODE")
    with pytest.raises(InteropError) as ei:
        svc.ingest(bad_code)
    assert ei.value.reason is RejectReason.UNKNOWN_CODE

    missing = condition()
    missing.pop("code")
    with pytest.raises(InteropError) as ei:
        parse_resource(missing)
    assert ei.value.reason is RejectReason.MISSING_REQUIRED_FIELD

    illegal = condition(code={"coding": [{"code": "I10"}]})
    with pytest.raises(InteropError) as ei:
        parse_resource(illegal)
    assert ei.value.reason is RejectReason.ILLEGAL_CODING
