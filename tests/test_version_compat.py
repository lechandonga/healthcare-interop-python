"""标准版本与字段兼容：不支持的版本/字段必须被明确拒绝。"""
from __future__ import annotations

import logging

import pytest

from tests.conftest import ADMIN, make_headers, make_patient

logger = logging.getLogger("healthcare_interop.test")


def _reason(response) -> tuple[str, dict]:
    err = response.json()["error"]
    return err["reason"], err


def _obs(**over) -> dict:
    body = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
    }
    body.update(over)
    return body


@pytest.fixture
def patient(client):
    r = client.post("/fhir/Patient", json=make_patient(), headers=ADMIN)
    assert r.status_code == 201


def test_version_aliases_supported(client):
    for alias, canonical in [
        ("3.0", "3.0"), ("STU3", "3.0"),
        ("R4", "4.0"), ("4.0", "4.0"),
        ("R5", "5.0"), ("5", "5.0"),
    ]:
        r = client.post(
            "/fhir/Patient",
            json=make_patient(pid=f"p-{alias.replace('.', '')}"),
            headers=make_headers(version=alias),
        )
        assert r.status_code == 201, (alias, r.text)
        assert r.json()["meta"]["sourceVersion"] == canonical


@pytest.mark.usefixtures("patient")
def test_observation_comment_only_stu3(client):
    sample = _obs(comment="STU3 备注")
    for ver, ok in [("3.0", True), ("4.0", False), ("5.0", False)]:
        body = dict(sample)
        if ver != "3.0":
            body["id"] = f"o-{ver.replace('.','')}"
        r = client.post("/fhir/Observation", json=body,
                        headers=make_headers(version=ver))
        logger.info("字段=comment 版本=%s 样本=%r 判定=%s", ver, body, r.status_code)
        if ok:
            assert r.status_code == 201, r.text
            assert r.json()["data"]["note"] == "STU3 备注"
        else:
            reason, err = _reason(r)
            assert reason == "INCOMPATIBLE_FIELD"
            assert err["details"]["supported_versions"] == ["3.0"]


@pytest.mark.usefixtures("patient")
def test_observation_note_r4_and_r5_only(client):
    for ver, ok in [("3.0", False), ("4.0", True), ("5.0", True)]:
        body = _obs(id=f"o-note-{ver.replace('.','')}", note="R4+ 备注")
        r = client.post("/fhir/Observation", json=body,
                        headers=make_headers(version=ver))
        if not ok:
            reason, err = _reason(r)
            assert reason == "INCOMPATIBLE_FIELD", (ver, r.text)
            assert err["details"]["supported_versions"] == ["4.0", "5.0"]
        else:
            assert r.status_code == 201, (ver, r.text)


@pytest.mark.usefixtures("patient")
def test_effective_instant_r5_only(client):
    inst = "2026-09-22T10:00:00Z"
    for ver, ok in [("3.0", False), ("4.0", False), ("5.0", True)]:
        body = _obs(id=f"o-inst-{ver.replace('.','')}", effectiveInstant=inst)
        r = client.post("/fhir/Observation", json=body,
                        headers=make_headers(version=ver))
        logger.info("字段=effectiveInstant 版本=%s -> %s", ver, r.status_code)
        if ok:
            assert r.status_code == 201
            assert r.json()["data"]["effective"] == inst
        else:
            assert _reason(r)[0] == "INCOMPATIBLE_FIELD"


def _condition(**over) -> dict:
    body = {
        "resourceType": "Condition",
        "id": "c1",
        "code": {
            "coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10"}
            ]
        },
        "subject": {"reference": "Patient/p1"},
    }
    body.update(over)
    return body


@pytest.mark.usefixtures("patient")
def test_condition_clinical_status_shape_per_version(client):
    # STU3：字符串
    r = client.post(
        "/fhir/Condition",
        json=_condition(clinicalStatus="active"),
        headers=make_headers(version="3.0"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["clinicalStatus"] == "active"

    # R4：必填，缺失 -> MISSING_FIELD
    r = client.post("/fhir/Condition", json=_condition(),
                    headers=make_headers(version="4.0"))
    assert _reason(r)[0] == "MISSING_FIELD"

    # R4：传 STU3 字符串 -> 结构语义不兼容
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="c2", clinicalStatus="active"),
        headers=make_headers(version="4.0"),
    )
    reason, err = _reason(r)
    assert reason == "INCOMPATIBLE_FIELD", r.text
    assert err["field"] == "clinicalStatus"

    # STU3：传 R4 CodeableConcept -> 不兼容
    cc = {
        "coding": [
            {"system": "http://local.example.org/codes/diagnosis", "code": "HTN"}
        ]
    }
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="c3", clinicalStatus=cc),
        headers=make_headers(version="3.0"),
    )
    assert _reason(r)[0] == "INCOMPATIBLE_FIELD"

    # R4 正确 CodeableConcept（本地码会映射到 ICD-10）
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="c4", clinicalStatus=cc),
        headers=make_headers(version="4.0"),
    )
    assert r.status_code == 201, r.text


@pytest.mark.usefixtures("patient")
def test_onset_period_r5_only(client):
    period = {"start": "2025-01-01", "end": "2025-02-01"}
    for ver, ok in [("3.0", False), ("4.0", False), ("5.0", True)]:
        r = client.post(
            "/fhir/Condition",
            json=_condition(
                id=f"c-p-{ver.replace('.','')}",
                onsetPeriod=period,
                **(
                    {"clinicalStatus": {
                        "coding": [
                            {"system": "http://local.example.org/codes/diagnosis",
                             "code": "HTN"}
                        ]}}
                    if ver in ("4.0", "5.0")
                    else {"clinicalStatus": "active"}
                ),
            ),
            headers=make_headers(version=ver),
        )
        if ok:
            assert r.status_code == 201, r.text
        else:
            assert _reason(r)[0] == "INCOMPATIBLE_FIELD"


@pytest.mark.usefixtures("patient")
def test_asserted_vs_recorded_date(client):
    # assertedDate 仅 STU3；recordedDate 仅 R4/R5
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="cA", clinicalStatus="active", assertedDate="2024-01-01"),
        headers=make_headers(version="3.0"),
    )
    assert r.status_code == 201
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="cA2", assertedDate="2024-01-01"),
        headers=make_headers(version="4.0"),
    )
    assert _reason(r)[0] == "INCOMPATIBLE_FIELD"

    cc = {"coding": [
        {"system": "http://local.example.org/codes/diagnosis", "code": "HTN"}]}
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="cR", clinicalStatus=cc, recordedDate="2024-01-01"),
        headers=make_headers(version="4.0"),
    )
    assert r.status_code == 201
    r = client.post(
        "/fhir/Condition",
        json=_condition(id="cR2", clinicalStatus="active", recordedDate="2024-01-01"),
        headers=make_headers(version="3.0"),
    )
    assert _reason(r)[0] == "INCOMPATIBLE_FIELD"


@pytest.mark.usefixtures("patient")
def test_snomed_uri_version_incompatibility(client):
    """SNOMED CT URI 随版本变化：跨版本使用必须拒绝。"""
    body = _obs(
        id="o-s1",
        code={"coding": [{"system": "http://snomed.info/sct",
                          "code": "38341003"}]},
    )
    # R4 OK
    r = client.post("/fhir/Observation", json=body,
                    headers=make_headers(version="4.0"))
    assert r.status_code == 201, r.text
    # STU3 用 R4 URI -> 术语版本不兼容
    r = client.post("/fhir/Observation", json=body,
                    headers=make_headers(version="3.0"))
    reason, err = _reason(r)
    assert reason == "TERMINOLOGY_VERSION_INCOMPATIBLE", r.text
    logger.info("SNOMED 跨版本样本=%r 判定依据=%s",
                body["code"], err["details"])

    body_stu3 = _obs(
        id="o-s2",
        code={"coding": [{"system": "http://snomed.info/sct-stu3",
                          "code": "38341003"}]},
    )
    r = client.post("/fhir/Observation", json=body_stu3,
                    headers=make_headers(version="4.0"))
    assert _reason(r)[0] == "TERMINOLOGY_VERSION_INCOMPATIBLE"


@pytest.mark.usefixtures("patient")
def test_local_mapping_normalizes_across_versions(client):
    """同一本地概念在 STU3/R4/R5 下都映射到同一规范 LOINC 码。"""
    for ver in ("3.0", "4.0", "5.0"):
        body = _obs(
            id=f"o-map-{ver.replace('.', '')}",
            code={"coding": [
                {"system": "http://local.example.org/codes/observation",
                 "code": "BP"}]},
        )
        r = client.post("/fhir/Observation", json=body,
                        headers=make_headers(version=ver))
        assert r.status_code == 201, r.text
        coding = r.json()["data"]["codings"][0]
        assert coding["system"] == "http://loinc.org"
        assert coding["code"] == "85354-9"
        assert coding["source_code"] == "BP"  # 保留来源可解释
