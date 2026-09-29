"""拒绝回执留痕与修正重放：可查、可复现、逐条定位、不泄敏、幂等、并发只生效一次。"""

import json
import logging
import threading

import pytest

from interop.errors import InteropError, RejectReason
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.receipts")

SENSITIVE_BIRTH = "1980-01-01"
SENSITIVE_NAME = "张三"
SENSITIVE_PHONE = "13800000000"


def patient(rid="p1", version="fhir-r4", **over):
    body = {"standard_version": version, "resourceType": "Patient", "id": rid,
            "name": [{"family": SENSITIVE_NAME}], "gender": "male",
            "birthDate": SENSITIVE_BIRTH,
            "telecom": [{"system": "phone", "value": SENSITIVE_PHONE}]}
    body.update(over)
    return body


def observation(rid, subject="Patient/p1", version="fhir-r4", **over):
    body = {"standard_version": version, "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
            "subject": {"reference": subject},
            "effectiveDateTime": "2026-09-22T10:00:00"}
    body.update(over)
    return body


def condition(rid, subject="Patient/p1", version="fhir-r4", code="I10", **over):
    body = {"standard_version": version, "resourceType": "Condition", "id": rid,
            "clinicalStatus": {"coding": [
                {"system": "http://x/clinical", "code": "active"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code}]},
            "subject": {"reference": subject}}
    body.update(over)
    return body


@pytest.fixture
def svc():
    s = InteropService()
    s.terminology.register_system(
        "http://hl7.org/fhir/sid/icd-10", "2016", {"I10": "Essential hypertension"})
    return s


def _reject(svc, body):
    with pytest.raises(InteropError) as ei:
        svc.ingest(body)
    return ei.value


# ---- 回执可查、结论完整、可复现 ----

def test_rejection_returns_persisted_queryable_receipt(svc):
    err = _reject(svc, observation("o1", "Patient/ghost"))
    rid = err.receipt_id
    log_case(log, "receipt-query", {"receipt_id": rid, "reason": err.reason.value},
             "判定依据：被拒写入返回 receipt_id，可按 id 查到完整回执")
    assert rid
    receipt = svc.get_receipt(rid)
    assert receipt["reason"] == "broken_reference"
    assert receipt["field"] == "reference"
    assert receipt["resource_type"] == "Observation"
    assert receipt["resource_id"] == "o1"
    assert receipt["batch_index"] is None
    assert receipt["status"] == "open"
    assert receipt["basis"]  # 判定依据非空
    assert receipt["input_sample"]["subject"]["reference"] == "Patient/ghost"


def test_resubmission_same_input_same_receipt_and_verdict(svc):
    body = observation("o1", "Patient/ghost")
    r1 = _reject(svc, body).receipt_id
    r2 = _reject(svc, body).receipt_id
    r3 = _reject(svc, body).receipt_id
    receipts = svc.list_receipts()
    log_case(log, "receipt-repro", {"ids": [r1, r2, r3], "count": len(receipts)},
             "判定依据：同一输入反复提交始终是同一条回执，不堆积、结论不漂移")
    assert r1 == r2 == r3
    assert len(receipts) == 1
    fetched = svc.get_receipt(r1)
    assert fetched["reason"] == "broken_reference"


def test_different_inputs_get_distinct_receipts(svc):
    r1 = _reject(svc, observation("o1", "Patient/ghost")).receipt_id
    r2 = _reject(svc, observation("o2", "Patient/ghost")).receipt_id
    assert r1 != r2
    assert {r["receipt_id"] for r in svc.list_receipts()} == {r1, r2}


def test_rejected_data_never_lands_in_main_store(svc):
    svc.ingest(patient("p1"))
    _reject(svc, observation("o1", "Patient/ghost"))
    with pytest.raises(InteropError) as ei:
        svc.store.get("Observation", "o1")
    assert ei.value.reason is RejectReason.RESOURCE_NOT_FOUND
    # 被拒的 Condition 也不得污染同资源后续版本
    _reject(svc, condition("c1", "Patient/ghost"))
    with pytest.raises(InteropError):
        svc.store.get("Condition", "c1")


# ---- 批量：失败逐条留痕、可单独查询 ----

def test_batch_failures_each_have_own_queryable_receipt(svc):
    svc.ingest(patient("p1"))
    result = svc.ingest_batch([
        observation("o1"),                                   # 0 成功
        observation("o2", "Patient/ghost"),                  # 1 broken_reference
        {**observation("o3"), "code": {"coding": [
            {"system": "http://loinc.org", "code": "00000-0"}]}},    # 2 unknown_code
        {**observation("o4"), "bogusField": 1},             # 3 unknown_field
    ])
    log_case(log, "batch-receipts", result,
             "判定依据：成功照常入库；每个失败下标都带独立 receipt_id 可单独查询")
    assert [s["index"] for s in result["succeeded"]] == [0]
    failed = {f["index"]: f for f in result["failed"]}
    assert set(failed) == {1, 2, 3}
    for index, reason in [(1, "broken_reference"), (2, "unknown_code"),
                          (3, "unknown_field")]:
        entry = failed[index]
        assert entry["reason"] == reason
        receipt = svc.get_receipt(entry["receipt_id"])
        assert receipt["batch_index"] == index
        assert receipt["reason"] == reason
    assert svc.store.get("Observation", "o1").version == 1


# ---- 回执不泄漏敏感原文 ----

def test_receipt_does_not_leak_sensitive_values(svc):
    err = _reject(svc, patient("p1", gender="M"))  # 非法性别被拒
    receipt = svc.get_receipt(err.receipt_id)
    serialized = json.dumps(receipt, ensure_ascii=False)
    log_case(log, "receipt-deid",
             {"name": receipt["input_sample"]["name"],
              "birthDate": receipt["input_sample"]["birthDate"]},
             "判定依据：快照中 name/birthDate/telecom 全部为 REDACTED 占位符")
    assert SENSITIVE_NAME not in serialized
    assert SENSITIVE_BIRTH not in serialized
    assert SENSITIVE_PHONE not in serialized
    assert receipt["input_sample"]["birthDate"].startswith("REDACTED:")
    assert receipt["input_sample"]["name"].startswith("REDACTED:")
    assert receipt["input_sample"]["telecom"].startswith("REDACTED:")
    # detail 文本同样不含敏感值
    assert "M" in receipt["detail"]  # 非法字段值（非敏感）保留以便定位
    assert SENSITIVE_NAME not in receipt["detail"]


# ---- 修正重放：走完整校验、幂等、状态可区分 ----

def test_corrected_replay_applies_once_and_resolves(svc):
    svc.ingest(patient("p1"))
    # 首次：未知代码被拒
    err = _reject(svc, condition("c1", code="NOPE"))
    rid = err.receipt_id
    assert svc.get_receipt(rid)["status"] == "open"

    # 修正后针对回执重放：走完整校验链路
    result = svc.replay_rejected(rid, condition("c1", code="I10"))
    log_case(log, "replay-ok", result,
             "判定依据：修正记录通过完整校验后入库，回执置 resolved，只生效一次")
    assert result["outcome"] == "applied"
    assert result["key"] == "Condition/c1"
    assert svc.get_receipt(rid)["status"] == "resolved"
    assert svc.store.get("Condition", "c1").version == 1
    assert svc.list_receipts("resolved")[0]["receipt_id"] == rid
    assert svc.list_receipts("open") == []


def test_replay_runs_full_validation_and_can_be_rejected_again(svc):
    svc.ingest(patient("p1"))
    rid = _reject(svc, condition("c1", code="NOPE")).receipt_id
    # 重放内容仍不合法（这次改成断裂引用）：不放水，原回执保持 open
    with pytest.raises(InteropError) as ei:
        svc.replay_rejected(rid, condition("c1", subject="Patient/ghost", code="I10"))
    log_case(log, "replay-rejected", ei.value.to_dict(),
             "判定依据：重放走同样校验链路，仍被拒 broken_reference，原回执仍 open")
    assert ei.value.reason is RejectReason.BROKEN_REFERENCE
    assert svc.get_receipt(rid)["status"] == "open"
    with pytest.raises(InteropError):
        svc.store.get("Condition", "c1")
    # 为本次新拒绝另留一条回执
    assert len(svc.list_receipts()) == 2

    # 改对之后仍可凭原回执重放成功
    svc.ingest(patient("p2"))
    out = svc.replay_rejected(rid, condition("c1", subject="Patient/p1", code="I10"))
    assert out["outcome"] == "applied"


def test_replay_resolved_receipt_does_not_duplicate(svc):
    svc.ingest(patient("p1"))
    rid = _reject(svc, condition("c1", code="NOPE")).receipt_id
    svc.replay_rejected(rid, condition("c1", code="I10"))

    # 已解决回执再重放：可区分结果，不重复入库、版本不前进
    with pytest.raises(InteropError) as ei:
        svc.replay_rejected(rid, condition("c1", code="I10"))
    log_case(log, "replay-resolved", ei.value.to_dict(),
             "判定依据：resolved 回执重放 -> receipt_already_resolved，无第二份数据")
    assert ei.value.reason is RejectReason.RECEIPT_ALREADY_RESOLVED
    assert svc.store.get("Condition", "c1").version == 1


def test_concurrent_replay_only_one_applies(svc):
    svc.ingest(patient("p1"))
    rid = _reject(svc, condition("c1", code="NOPE")).receipt_id
    n_threads = 8
    outcomes: list[object] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        try:
            svc.replay_rejected(rid, condition("c1", code="I10"))
            outcomes.append("applied")
        except InteropError as e:
            outcomes.append(e.reason)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    losers = {o for o in outcomes if o != "applied"}
    log_case(log, "replay-concurrent", {"outcomes": outcomes},
             "判定依据：恰好 1 个 applied，其余为 receipt_in_progress 或 "
             "receipt_already_resolved（皆为可区分结果），数据只有一份、版本为 1")
    assert outcomes.count("applied") == 1
    assert losers <= {RejectReason.RECEIPT_IN_PROGRESS,
                      RejectReason.RECEIPT_ALREADY_RESOLVED}
    assert svc.store.get("Condition", "c1").version == 1
    assert svc.get_receipt(rid)["status"] == "resolved"
    # 未抢到的调用方稍后重试得到"已解决"，不会再写一份
    with pytest.raises(InteropError) as ei:
        svc.replay_rejected(rid, condition("c1", code="I10"))
    assert ei.value.reason is RejectReason.RECEIPT_ALREADY_RESOLVED


def test_unknown_receipt_is_distinguishable(svc):
    with pytest.raises(InteropError) as ei:
        svc.get_receipt("rr-doesnotexist")
    assert ei.value.reason is RejectReason.RECEIPT_NOT_FOUND


def test_in_progress_claim_is_distinguishable_and_releasable(svc):
    svc.ingest(patient("p1"))
    rid = _reject(svc, condition("c1", code="NOPE")).receipt_id
    # 第一个调用方领取后尚未完成，第二个调用方拿到可区分结果
    claimed = svc.rejections.claim(rid)
    with pytest.raises(InteropError) as ei:
        svc.replay_rejected(rid, condition("c1", code="I10"))
    log_case(log, "replay-in-progress", ei.value.to_dict(),
             "判定依据：回执被他人领取中 -> receipt_in_progress，本次不生效")
    assert ei.value.reason is RejectReason.RECEIPT_IN_PROGRESS
    # 领取方放弃后可继续重放，不会死锁
    svc.rejections.release(claimed)
    assert svc.replay_rejected(rid, condition("c1", code="I10"))["outcome"] == "applied"
