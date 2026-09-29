"""解析层：畸形输入必须被正确归类，原因可区分。"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.parsing import parse_resource
from tests.conftest import log_case

log = logging.getLogger("test.parsing")

VALID_PATIENT = {
    "standard_version": "fhir-r4", "resourceType": "Patient", "id": "p1",
    "name": [{"family": "张", "given": ["三"]}], "gender": "male",
    "birthDate": "1980-01-01",
}


@pytest.mark.parametrize("sample,reason", [
    ("{not json", RejectReason.MALFORMED_JSON),
    (b"\xff\xfe{}", RejectReason.MALFORMED_JSON),
    ([1, 2], RejectReason.MALFORMED_JSON),
    ({"resourceType": "Patient", "id": "p1"}, RejectReason.MISSING_REQUIRED_FIELD),
    ({**VALID_PATIENT, "standard_version": "fhir-r2"}, RejectReason.UNSUPPORTED_STANDARD_VERSION),
    ({**VALID_PATIENT, "resourceType": "Claim"}, RejectReason.UNSUPPORTED_RESOURCE_TYPE),
    ({**VALID_PATIENT, "id": "bad id!"}, RejectReason.INVALID_FIELD_TYPE),
    ({**VALID_PATIENT, "name": []}, RejectReason.MISSING_REQUIRED_FIELD),
    ({**VALID_PATIENT, "gender": "M"}, RejectReason.INVALID_FIELD_TYPE),
    ({**VALID_PATIENT, "birthDate": "1980/01/01"}, RejectReason.INVALID_FIELD_TYPE),
    ({**VALID_PATIENT, "telecom": "13800000000"}, RejectReason.INVALID_FIELD_TYPE),
])
def test_patient_rejections_are_classified(sample, reason):
    log_case(log, "patient-reject", sample, f"应拒绝为 {reason.value}")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is reason
    log.info("实际拒绝原因=%s detail=%s", ei.value.reason.value, ei.value.detail)


@pytest.mark.parametrize("sample,reason", [
    # 缺 code.coding
    ({"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
      "status": "final", "code": {"coding": []},
      "subject": {"reference": "Patient/p1"},
      "effectiveDateTime": "2026-09-22T10:00:00"}, RejectReason.ILLEGAL_CODING),
    # coding 缺 system
    ({"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
      "status": "final", "code": {"coding": [{"code": "8480-6"}]},
      "subject": {"reference": "Patient/p1"},
      "effectiveDateTime": "2026-09-22T10:00:00"}, RejectReason.ILLEGAL_CODING),
    # R4 缺 effectiveDateTime（结构不完整）
    ({"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
      "status": "final",
      "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
      "subject": {"reference": "Patient/p1"}}, RejectReason.MISSING_REQUIRED_FIELD),
    # R5 用了 R4/STU3 才有的字段（版本字段错位，与未知字段原因可区分）
    ({"standard_version": "fhir-r5", "resourceType": "Observation", "id": "o1",
      "status": "final",
      "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
      "subject": {"reference": "Patient/p1"},
      "effectiveDateTime": "2026-09-22T10:00:00"}, RejectReason.FIELD_NOT_DEFINED_FOR_VERSION),
    # R4 不允许 R5 才有的 status
    ({"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
      "status": "corrected",
      "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
      "subject": {"reference": "Patient/p1"},
      "effectiveDateTime": "2026-09-22T10:00:00"}, RejectReason.INVALID_FIELD_TYPE),
    # 引用格式非法
    ({"standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
      "status": "final",
      "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
      "subject": {"reference": "p1"},
      "effectiveDateTime": "2026-09-22T10:00:00"}, RejectReason.INVALID_FIELD_TYPE),
])
def test_observation_rejections_are_classified(sample, reason):
    log_case(log, "observation-reject", sample, f"应拒绝为 {reason.value}")
    with pytest.raises(InteropError) as ei:
        parse_resource(sample)
    assert ei.value.reason is reason


def test_r4_and_r5_normalize_to_same_shape():
    r4 = parse_resource({
        "standard_version": "fhir-r4", "resourceType": "Observation", "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": "2026-09-22T10:00:00"})
    r5 = parse_resource({
        "standard_version": "fhir-r5", "resourceType": "Observation", "id": "o2",
        "status": "corrected",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
        "subject": {"reference": "Patient/p1"},
        "effectivePeriod": {"start": "2026-09-22T10:00:00", "end": "2026-09-22T10:05:00"}})
    log_case(log, "normalize", {"r4": r4.payload["effective"], "r5": r5.payload["effective"]},
             "两版本 effective 归一为 {type,start,end} 同构结构")
    assert set(r4.payload["effective"]) == set(r5.payload["effective"]) == {"type", "start", "end"}
    assert r4.references == r5.references == ["Patient/p1"]
