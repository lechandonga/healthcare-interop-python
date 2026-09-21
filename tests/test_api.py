"""API 层：错误经 HTTP 响应仍可区分，端到端流程可用。"""

import logging

import pytest
from fastapi.testclient import TestClient

from interop.api import app, service
from tests.conftest import log_case

log = logging.getLogger("test.api")


@pytest.fixture(autouse=True)
def fresh_service():
    from interop.service import InteropService
    import interop.api as api
    api.service = InteropService()
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


PATIENT = {"standard_version": "fhir-r4", "resourceType": "Patient", "id": "p1",
           "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}


def test_error_reasons_distinguishable_over_http(client):
    cases = [
        ({"standard_version": "fhir-r9"}, 400, "unsupported_standard_version"),
        ({"standard_version": "fhir-r4", "resourceType": "Patient", "id": "x"},
         422, "missing_required_field"),
    ]
    for body, status, reason in cases:
        r = client.post("/resources", json=body)
        log_case(log, "http-error", body, f"HTTP {status} reason={reason}")
        assert r.status_code == status
        assert r.json()["error"]["reason"] == reason


def test_end_to_end_flow(client):
    r = client.post("/resources", json=PATIENT)
    assert r.status_code == 201

    # 默认去标识化视图
    r = client.get("/resources/Patient/p1")
    assert r.json()["view"] == "deidentified"
    assert "1980-01-01" not in r.text

    # 未授权还原被拒
    r = client.get("/resources/Patient/p1", params={"token": "forged"})
    assert r.status_code == 401
    assert r.json()["error"]["reason"] == "invalid_token"

    # 授权还原
    token = client.post("/tokens/Patient/p1", params={"role": "clinical"}).json()["token"]
    r = client.get("/resources/Patient/p1", params={"token": token})
    assert r.json()["view"] == "reidentified"
    assert r.json()["resource"]["sensitive_fields"]["birthDate"] == "1980-01-01"

    # 乐观锁更新
    r = client.put("/resources/Patient/p1",
                   json={**PATIENT, "gender": "female"},
                   params={"expected_version": 1})
    assert r.json()["version"] == 2
    r = client.put("/resources/Patient/p1", json=PATIENT,
                   params={"expected_version": 1})
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "version_conflict"
