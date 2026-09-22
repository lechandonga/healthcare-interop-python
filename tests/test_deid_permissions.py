"""敏感字段去标识化与受控还原：不同权限返回可区分，未授权不泄漏。"""
from __future__ import annotations

import logging

from tests.conftest import (
    ADMIN,
    CLINICIAN,
    DEID,
    REIDENTIFY,
    make_patient,
)

logger = logging.getLogger("healthcare_interop.test")

PATIENT_BODY = make_patient(
    gender="female",
    birthDate="1990-05-17",
    name=[{"family": "Zhang", "given": ["San"]}],
    telecom=[{"system": "phone", "value": "13800000000"}],
    address=[{"city": "Shanghai"}],
    identifier=[{"system": "http://local.example.org/id/ssn", "value": "310101199005171234"}],
)


def _create(client):
    r = client.post("/fhir/Patient", json=PATIENT_BODY, headers=ADMIN)
    assert r.status_code == 201, r.text


def test_no_token_rejected(client):
    _create(client)
    r = client.get("/fhir/Patient/p1")
    assert r.status_code == 401
    assert r.json()["error"]["reason"] == "UNAUTHORIZED"
    assert "Zhang" not in r.text and "310101" not in r.text


def test_deid_view_masks_all_sensitive_fields(client):
    _create(client)
    r = client.get("/fhir/Patient/p1", headers=DEID)
    assert r.status_code == 200
    body = r.json()
    assert body["view"] == "de-identified"
    data = body["data"]
    assert data["name"] == "<redacted:name>"
    assert data["birthDate"] == "<redacted:birthDate>"
    assert data["telecom"] == "<redacted:telecom>"
    assert data["address"] == "<redacted:address>"
    assert data["identifier"] == "<redacted:identifier>"
    # 非敏感字段保留
    assert data["gender"] == "female"
    # 原始值绝不出现在响应任何位置
    for secret in ("Zhang", "San", "13800000000", "Shanghai",
                   "310101199005171234", "1990-05-17"):
        assert secret not in r.text, secret
    logger.info("脱敏响应样本=%s", data)


def test_clinician_gets_deidentified_without_reidentify_scope(client):
    _create(client)
    r = client.get("/fhir/Patient/p1", headers=CLINICIAN)
    assert r.json()["view"] == "de-identified"
    assert r.json()["data"]["birthDate"] == "<redacted:birthDate>"


def test_reidentify_scope_returns_original(client):
    _create(client)
    r = client.get("/fhir/Patient/p1", headers=REIDENTIFY)
    assert r.status_code == 200
    body = r.json()
    assert body["view"] == "identified"
    assert body["data"]["birthDate"] == "1990-05-17"
    assert body["data"]["name"][0]["family"] == "Zhang"
    logger.info("受控还原视图=%s", body["view"])


def test_admin_sees_identified(client):
    _create(client)
    r = client.get("/fhir/Patient/p1", headers=ADMIN)
    assert r.json()["view"] == "identified"


def test_deid_cannot_write(client):
    r = client.post("/fhir/Patient", json=make_patient("px"), headers=DEID)
    assert r.status_code == 403
    assert r.json()["error"]["reason"] == "FORBIDDEN"
    # 被拒绝的资源不可查
    assert client.get("/fhir/Patient/px", headers=ADMIN).status_code == 404


def test_views_are_distinguishable(client):
    _create(client)
    deid = client.get("/fhir/Patient/p1", headers=DEID).json()
    identified = client.get("/fhir/Patient/p1", headers=REIDENTIFY).json()
    assert deid["view"] != identified["view"]
    assert deid["data"]["birthDate"] != identified["data"]["birthDate"]
    # 两者资源版本一致：差异只来自权限视图
    assert deid["meta"]["version"] == identified["meta"]["version"]


def test_error_messages_do_not_leak_sensitive_values(client):
    """制造一个冲突错误，错误体中不得回显原始敏感字段。"""
    _create(client)
    # 重复创建，错误回显 ID（非敏感）但不应包含 name/ssn
    r = client.post("/fhir/Patient", json=PATIENT_BODY, headers=ADMIN)
    assert r.status_code == 409
    assert "Zhang" not in r.text
    assert "310101199005171234" not in r.text
