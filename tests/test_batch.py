"""批量写入：部分失败隔离、原因可区分可复现、部分成功可查询。"""
from __future__ import annotations

import logging

from tests.conftest import ADMIN, make_headers, make_patient

logger = logging.getLogger("healthcare_interop.test")


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [{"resource": r} for r in resources],
    }


def _obs(oid: str, subject: str = "Patient/p1") -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": subject},
    }


def test_batch_mixed_success_and_failure(client):
    # 前置：p1 存在
    assert client.post("/fhir/Patient", json=make_patient(), headers=ADMIN).status_code == 201

    bundle = _bundle(
        make_patient("b1"),                                   # 成功
        _obs("bo1", subject="Patient/ghost"),                # 引用目标缺失
        make_patient("p1"),                                   # 已存在
        make_patient("b2", gender="zzz"),                     # 非法码
        {"resourceType": "Observation", "id": "bo2"},         # 缺必填
        make_patient("b3", unknownField=1),                   # 未知字段
        make_patient("b4", gender="male"),                    # 成功
    )
    r = client.post("/fhir/", json=bundle, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    logger.info("批量样本=%r 判定=%s", bundle, [e["error"] for e in body["entry"]])

    assert body["successCount"] == 2
    assert body["failureCount"] == 5
    expected = [
        None,
        "REFERENCE_TARGET_NOT_FOUND",
        "ALREADY_EXISTS",
        "ILLEGAL_CODE",
        "MISSING_FIELD",
        "UNKNOWN_FIELD",
        None,
    ]
    actual = [e["error"]["reason"] if e["error"] else None for e in body["entry"]]
    assert actual == expected

    # 每个失败都有可定位字段/依据
    assert body["entry"][1]["error"]["details"]["missing_reference"] == "Patient/ghost"
    assert body["entry"][3]["error"]["field"] == "gender"
    assert body["entry"][5]["error"]["field"] == "unknownField"


def test_partial_success_is_queryable(client):
    assert client.post("/fhir/Patient", json=make_patient(), headers=ADMIN).status_code == 201
    r = client.post(
        "/fhir/",
        json=_bundle(make_patient("ok1"), make_patient("ok2", gender="zzz")),
        headers=ADMIN,
    )
    body = r.json()
    assert body["successCount"] == 1
    # 成功条目可独立查询
    got = client.get("/fhir/Patient/ok1", headers=ADMIN)
    assert got.status_code == 200
    # 失败条目无残留
    assert client.get("/fhir/Patient/ok2", headers=ADMIN).status_code == 404


def test_failure_does_not_pollute_existing_state(client):
    assert client.post("/fhir/Patient", json=make_patient(active=True),
                       headers=ADMIN).status_code == 201
    original_etag = client.get("/fhir/Patient/p1",
                               headers=ADMIN).json()["meta"]["version"]
    # 批处理全部失败（含尝试创建 p1）
    r = client.post(
        "/fhir/",
        json=_bundle(
            make_patient("p1"),
            make_patient("bad", gender="zzz"),
            _obs("bx", subject="Patient/ghost"),
        ),
        headers=ADMIN,
    )
    assert r.json()["successCount"] == 0
    # p1 仍是版本 1、数据未变
    after = client.get("/fhir/Patient/p1", headers=ADMIN).json()
    assert after["meta"]["version"] == original_etag == 1
    assert after["data"]["active"] is True


def test_batch_rejection_is_reproducible(client):
    assert client.post("/fhir/Patient", json=make_patient(), headers=ADMIN).status_code == 201
    bundle = _bundle(make_patient("r1", gender="zzz"),
                     _obs("r2", subject="Patient/ghost"))
    first = client.post("/fhir/", json=bundle, headers=ADMIN).json()
    second = client.post("/fhir/", json=bundle, headers=ADMIN).json()
    # 同样输入 -> 同样的逐条目错误码（可复现）
    a = [(e["ok"], e["error"] and e["error"]["reason"]) for e in first["entry"]]
    b = [(e["ok"], e["error"] and e["error"]["reason"]) for e in second["entry"]]
    assert a == b
    logger.info("可复现判定=%s", a)


def test_batch_envelope_validation(client):
    for body, reason in [
        ({"resourceType": "Bundle"}, "INCOMPATIBLE_FIELD"),
        ({"resourceType": "Bundle", "type": "batch"}, "STRUCTURE_INCOMPLETE"),
        ({"resourceType": "Bundle", "type": "transaction",
          "entry": [{"resource": make_patient("z")}]}, "INCOMPATIBLE_FIELD"),
        (["not-an-object"], "NOT_A_RESOURCE_OBJECT"),
    ]:
        r = client.post("/fhir/", json=body, headers=ADMIN)
        assert r.json()["error"]["reason"] == reason, (body, r.text)


def test_batch_malformed_json(client):
    r = client.post("/fhir/", content=b"{oops",
                    headers={**ADMIN, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["reason"] == "MALFORMED_JSON"


def test_batch_requires_write_scope(client):
    r = client.post(
        "/fhir/",
        json=_bundle(make_patient("z")),
        headers=make_headers(token="token-deid"),
    )
    assert r.status_code == 403
