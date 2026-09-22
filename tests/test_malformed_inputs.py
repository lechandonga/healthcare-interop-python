"""畸形输入必须被正确归类且原因可区分；失败不得残留状态。"""
from __future__ import annotations

import logging

import pytest

from tests.conftest import ADMIN, make_headers, make_patient

logger = logging.getLogger("healthcare_interop.test")


def _assert_reject(response, expected: str, *, field: str | None = None) -> dict:
    assert response.status_code in (400, 401, 403, 404, 409, 422), response.text
    err = response.json()["error"]
    # 判定依据可区分、可复现
    assert err["reason"] == expected, (err, expected)
    if field is not None:
        assert err["field"] == field, (err, field)
    return err


def test_malformed_json_body(client, caplog):
    sample = b'{"resourceType": "Patient", "id": }'
    with caplog.at_level(logging.INFO):
        r = client.post(
            "/fhir/Patient",
            content=sample,
            headers={**ADMIN, "Content-Type": "application/json"},
        )
    logger.info("样本=%r 判定=%s", sample, r.json())
    err = _assert_reject(r, "MALFORMED_JSON")
    assert "position" in err["details"]
    # 失败不得产生任何资源
    assert client.get("/fhir/Patient/x", headers=ADMIN).status_code == 404


def test_non_object_top_level(client):
    raw_cases: list[tuple[bytes, str]] = [
        (b"[]", "list"),
        (b'"string"', "str"),
        (b"123", "int"),
        (b"null", "NoneType"),
    ]
    for sample, type_name in raw_cases:
        r = client.post(
            "/fhir/Patient",
            content=sample,
            headers={**ADMIN, "Content-Type": "application/json"},
        )
        err = _assert_reject(r, "NOT_A_RESOURCE_OBJECT")
        logger.info("样本=%r 判定依据 actual_type=%s", sample, err["details"])
        assert err["details"]["actual_type"] == type_name


def test_missing_resource_type(client):
    r = client.post("/fhir/Patient", json={"id": "p1"}, headers=ADMIN)
    _assert_reject(r, "MISSING_FIELD", field="resourceType")


def test_unsupported_resource_type(client):
    r = client.post(
        "/fhir/Medication",
        json={"resourceType": "Medication", "id": "m1"},
        headers=ADMIN,
    )
    _assert_reject(r, "UNSUPPORTED_RESOURCE_TYPE", field="resourceType")


def test_missing_and_empty_required_fields(client):
    cases = [
        (make_patient(pid=None), "MISSING_FIELD", "id"),
        (make_patient(id=""), "EMPTY_FIELD", "id"),
        ({"resourceType": "Patient", "id": "p1", "gender": ""},
         "EMPTY_FIELD", "gender"),
        ({"resourceType": "Observation", "id": "o1"},
         "MISSING_FIELD", "status"),
        ({"resourceType": "Observation", "id": "o1", "status": "final"},
         "MISSING_FIELD", "code"),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"text": "仅自由文本"},
            },
            "STRUCTURE_INCOMPLETE",
            "code.coding",
        ),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": []},
            },
            "STRUCTURE_INCOMPLETE",
            "code.coding",
        ),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": ["not-an-object"]},
            },
            "STRUCTURE_INCOMPLETE",
            "code.coding[0]",
        ),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"code": "85354-9"}]},
            },
            "MISSING_FIELD",
            "code.coding[0].system",
        ),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org"}]},
            },
            "MISSING_FIELD",
            "code.coding[0].code",
        ),
        (
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"system": "http://loinc.org", "code": ""}]},
            },
            "ILLEGAL_CODE",
            "code.coding[0]",
        ),
    ]
    for sample, reason, fld in cases:
        logger.info("样本=%r 期望判定=%s 字段=%s", sample, reason, fld)
        r = client.post("/fhir/Patient" if sample["resourceType"] == "Patient"
                        else "/fhir/Observation", json=sample, headers=ADMIN)
        _assert_reject(r, reason, field=fld)


def test_empty_optional_fields_rejected(client):
    """可选字段给了空值也按 EMPTY_FIELD 拒绝，不静默当“没提供”。"""
    r = client.post(
        "/fhir/Patient",
        json=make_patient(birthDate=""),
        headers=ADMIN,
    )
    _assert_reject(r, "EMPTY_FIELD", field="birthDate")


def test_unknown_field_not_silently_ignored(client):
    sample = make_patient(extraField="x")
    r = client.post("/fhir/Patient", json=sample, headers=ADMIN)
    err = _assert_reject(r, "UNKNOWN_FIELD", field="extraField")
    logger.info("样本=%r 判定依据=%s", sample, err)


def test_invalid_field_type(client):
    sample = make_patient(gender=123)
    r = client.post("/fhir/Patient", json=sample, headers=ADMIN)
    _assert_reject(r, "INVALID_FIELD_TYPE", field="gender")


def test_illegal_gender_and_status(client):
    r = client.post("/fhir/Patient", json=make_patient(gender="helicopter"),
                    headers=ADMIN)
    _assert_reject(r, "ILLEGAL_CODE", field="gender")

    obs = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "weird",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
    }
    r = client.post("/fhir/Observation", json=obs, headers=ADMIN)
    _assert_reject(r, "ILLEGAL_CODE", field="status")


def test_unknown_code_system_and_code(client):
    obs = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": "http://unknown.example/x", "code": "1"}]},
    }
    r = client.post("/fhir/Observation", json=obs, headers=ADMIN)
    _assert_reject(r, "CODE_SYSTEM_UNKNOWN", field="code.coding[0].system")

    obs["code"]["coding"][0] = {"system": "http://loinc.org", "code": "NOPE"}
    r = client.post("/fhir/Observation", json=obs, headers=ADMIN)
    _assert_reject(r, "CODE_NOT_FOUND", field="code.coding[0].code")


def test_mapping_conflict_rejected(client):
    obs = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {
            "coding": [
                {"system": "http://local.example.org/codes/conflicting",
                 "code": "X1"}
            ]
        },
    }
    r = client.post("/fhir/Observation", json=obs, headers=ADMIN)
    err = _assert_reject(r, "MAPPING_CONFLICT", field="code.coding[0]")
    logger.info("冲突样本=%r 判定依据=%s", obs, err["details"])


def test_broken_and_missing_references(client):
    # 先建一个 Patient
    assert client.post("/fhir/Patient", json=make_patient(), headers=ADMIN).status_code == 201

    obs = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
    }
    # 坏引用格式
    r = client.post("/fhir/Observation", json={**obs, "subject": {"reference": "bad"}},
                    headers=ADMIN)
    _assert_reject(r, "REFERENCE_BROKEN", field="subject.reference")
    # 目标不存在
    r = client.post(
        "/fhir/Observation",
        json={**obs, "subject": {"reference": "Patient/ghost"}},
        headers=ADMIN,
    )
    _assert_reject(r, "REFERENCE_TARGET_NOT_FOUND", field="references")
    # 失败资源未残留
    assert client.get("/fhir/Observation/o1", headers=ADMIN).status_code == 404


def test_missing_and_unsupported_version(client):
    r = client.post(
        "/fhir/Patient",
        json=make_patient(),
        headers={"Authorization": "Bearer token-admin"},
    )
    _assert_reject(r, "MISSING_VERSION", field="fhirVersion")

    r = client.post(
        "/fhir/Patient",
        json=make_patient(),
        headers=make_headers(version="2.1"),
    )
    err = _assert_reject(r, "UNSUPPORTED_VERSION")
    assert err["details"]["supported"] == ["3.0", "4.0", "5.0"]


def test_url_body_id_mismatch_rejected(client):
    r = client.put(
        "/fhir/Patient/p9",
        json=make_patient(id="p8"),
        headers={**ADMIN, "If-Match": 'W/"1"'},
    )
    _assert_reject(r, "INCOMPATIBLE_FIELD", field="id")
