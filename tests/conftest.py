"""测试夹具：每个用例前后重置单例服务，保证状态隔离。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.dependencies import reset_service_for_tests
from main import app


@pytest.fixture
def client() -> TestClient:
    reset_service_for_tests()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service():
    return reset_service_for_tests()


@pytest.fixture(autouse=True)
def _audit_logs(caplog):
    """捕获 health interop 各 logger，便于在用例中打印样本与判定依据。"""
    for name in (
        "healthcare_interop.api",
        "healthcare_interop.batch",
        "healthcare_interop.audit",
    ):
        caplog.set_level(logging.INFO, logger=name)
    yield


ADMIN = {"Authorization": "Bearer token-admin", "X-FHIR-Version": "4.0"}
CLINICIAN = {"Authorization": "Bearer token-clinician", "X-FHIR-Version": "4.0"}
DEID = {"Authorization": "Bearer token-deid", "X-FHIR-Version": "4.0"}
REIDENTIFY = {
    "Authorization": "Bearer token-reidentify",
    "X-FHIR-Version": "4.0",
}


def make_headers(token: str = "token-admin", version: str = "4.0") -> dict:
    return {"Authorization": f"Bearer {token}", "X-FHIR-Version": version}


def make_patient(pid: str | None = "p1", **over) -> dict:
    body: dict = {"resourceType": "Patient"}
    if pid is not None:
        body["id"] = pid
    body.update(over)
    return body
