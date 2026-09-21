"""去标识化与受控还原：未授权访问被拒绝且不泄漏原值，不同权限结果可区分。"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.deidentify")

PATIENT = {"standard_version": "fhir-r4", "resourceType": "Patient", "id": "p1",
           "name": [{"family": "张", "given": ["三"]}], "gender": "male",
           "birthDate": "1980-01-01", "telecom": [{"system": "phone", "value": "13800000000"}]}


@pytest.fixture
def svc():
    s = InteropService()
    s.ingest(PATIENT)
    return s


def test_default_view_is_deidentified(svc):
    view = svc.get("Patient", "p1")
    log_case(log, "default-view", view["resource"]["sensitive_fields"],
             "无令牌访问应返回去标识化视图，敏感字段为哈希占位符")
    assert view["view"] == "deidentified"
    for value in view["resource"]["sensitive_fields"].values():
        assert value.startswith("REDACTED:")
    # 不泄漏原始数据
    assert "张" not in str(view) and "1980-01-01" not in str(view) \
        and "13800000000" not in str(view)


def test_authorized_reidentification(svc):
    token = svc.issue_token("Patient", "p1", role="clinical")
    view = svc.get("Patient", "p1", token=token)
    log_case(log, "authorized", view["resource"]["sensitive_fields"],
             "clinical 角色令牌应还原出原始敏感字段")
    assert view["view"] == "reidentified"
    assert view["resource"]["sensitive_fields"]["birthDate"] == "1980-01-01"
    assert view["resource"]["sensitive_fields"]["name"][0]["family"] == "张"


def test_invalid_token_rejected(svc):
    log_case(log, "invalid-token", {"token": "forged"}, "伪造令牌应拒绝 INVALID_TOKEN")
    with pytest.raises(InteropError) as ei:
        svc.get("Patient", "p1", token="forged")
    assert ei.value.reason is RejectReason.INVALID_TOKEN


def test_insufficient_role_rejected_and_distinguishable(svc):
    token = svc.issue_token("Patient", "p1", role="research")
    log_case(log, "wrong-role", {"role": "research"},
             "research 角色应拒绝 UNAUTHORIZED_REIDENTIFICATION，与 INVALID_TOKEN 可区分")
    with pytest.raises(InteropError) as ei:
        svc.get("Patient", "p1", token=token)
    assert ei.value.reason is RejectReason.UNAUTHORIZED_REIDENTIFICATION
    assert ei.value.reason is not RejectReason.INVALID_TOKEN


def test_token_scoped_to_single_resource(svc):
    svc.ingest({**PATIENT, "id": "p2"})
    token = svc.issue_token("Patient", "p1", role="clinical")
    log_case(log, "cross-resource", {"token_for": "p1", "access": "p2"},
             "p1 的令牌不得用于还原 p2，应拒绝 UNAUTHORIZED_REIDENTIFICATION")
    with pytest.raises(InteropError) as ei:
        svc.get("Patient", "p2", token=token)
    assert ei.value.reason is RejectReason.UNAUTHORIZED_REIDENTIFICATION


def test_revoked_token_rejected(svc):
    token = svc.issue_token("Patient", "p1", role="clinical")
    svc.deidentification.revoke_token(token)
    log_case(log, "revoked", {"token": "已撤销"}, "撤销后的令牌应拒绝 INVALID_TOKEN")
    with pytest.raises(InteropError) as ei:
        svc.get("Patient", "p1", token=token)
    assert ei.value.reason is RejectReason.INVALID_TOKEN
