"""批量写入：单条失败不污染整批，失败原因可区分且可复现，部分成功可查询。"""

import logging

from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.batch")


def patient(rid):
    return {"standard_version": "fhir-r4", "resourceType": "Patient", "id": rid,
            "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}


def observation(rid, subject, code="8480-6"):
    return {"standard_version": "fhir-r4", "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
            "subject": {"reference": subject},
            "effectiveDateTime": "2026-09-22T10:00:00"}


def test_partial_failure_does_not_pollute_batch():
    svc = InteropService()
    svc.ingest(patient("p1"))
    batch = [
        observation("o1", "Patient/p1"),          # 成功
        observation("o2", "Patient/ghost"),       # 断裂引用
        observation("o3", "Patient/p1", code="00000-0"),  # 未知代码
        {"standard_version": "fhir-r9"},          # 版本不支持
        observation("o4", "Patient/p1"),          # 成功
    ]
    result = svc.ingest_batch(batch)
    log_case(log, "partial-batch", result,
             "判定依据：o1/o4 成功；o2=broken_reference, o3=unknown_code, idx3=unsupported_standard_version")

    assert [s["index"] for s in result["succeeded"]] == [0, 4]
    reasons = {f["index"]: f["reason"] for f in result["failed"]}
    assert reasons == {1: "broken_reference", 2: "unknown_code",
                       3: "unsupported_standard_version"}

    # 部分成功结果可查询
    assert svc.store.get("Observation", "o1").version == 1
    assert svc.store.get("Observation", "o4").version == 1
    # 失败记录无残留、不污染已有状态
    for rid in ("o2", "o3"):
        try:
            svc.store.get("Observation", rid)
            raise AssertionError(f"{rid} 不应存在")
        except Exception as e:
            assert "resource_not_found" in str(e)


def test_batch_failure_is_reproducible():
    """同一批输入重放，失败原因与下标完全一致（可复现）。"""
    def run():
        svc = InteropService()
        svc.ingest(patient("p1"))
        return svc.ingest_batch([
            observation("o1", "Patient/p1"),
            observation("o2", "Patient/ghost"),
        ])
    r1, r2 = run(), run()
    log_case(log, "reproducible", {"run1": r1["failed"], "run2": r2["failed"]},
             "判定依据：两次独立运行的 failed 列表完全一致")
    assert r1["failed"] == r2["failed"]
    assert r1["succeeded"] == r2["succeeded"]


def test_batch_idempotent_replay():
    """整批带幂等键重放：不产生重复写入，版本不前进。"""
    svc = InteropService()
    svc.ingest(patient("p1"))
    batch = [{**observation("o1", "Patient/p1"), "idempotency_key": "b1"},
             {**observation("o2", "Patient/p1"), "idempotency_key": "b2"}]
    first = svc.ingest_batch(batch)
    second = svc.ingest_batch(batch)
    log_case(log, "batch-replay", {"first": first, "second": second},
             "判定依据：两次均全部成功且版本均为 1（幂等重放返回原记录）")
    assert len(first["succeeded"]) == len(second["succeeded"]) == 2
    assert svc.store.get("Observation", "o1").version == 1
    assert svc.store.get("Observation", "o2").version == 1
