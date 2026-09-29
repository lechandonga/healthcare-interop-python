"""拒绝回执：可查、可复现、逐条可定位、不泄漏敏感值；修正重放幂等且并发只生效一次。"""

import logging
import threading

import pytest

from interop.errors import InteropError, RejectReason
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.receipts")


def patient(rid="p1", version="fhir-r4"):
    return {"standard_version": version, "resourceType": "Patient", "id": rid,
            "name": [{"family": "张", "given": ["三"]}], "gender": "male",
            "birthDate": "1980-01-01", "telecom": [{"system": "phone", "value": "13800000000"}]}


def condition(rid="c1", version="fhir-stu3", **extra):
    body = {"standard_version": version, "resourceType": "Condition", "id": rid,
            "clinicalStatus": "active",
            "verificationStatus": "confirmed",
            "code": {"coding": [{"system": "http://local.example/diagnosis", "code": "I10"}]},
            "subject": {"reference": "Patient/p1"}}
    body.update(extra)
    return body


def observation(rid, subject="Patient/p1", code="8480-6"):
    return {"standard_version": "fhir-r4", "resourceType": "Observation", "id": rid,
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
            "subject": {"reference": subject},
            "effectiveDateTime": "2026-09-22T10:00:00"}


@pytest.fixture
def svc():
    s = InteropService()
    s.ingest(patient(version="fhir-stu3"))
    return s


def _reject(svc, payload):
    with pytest.raises(InteropError) as ei:
        svc.ingest(payload)
    return ei.value


def test_receipt_is_queryable_with_resource_index_reason_field_basis(svc):
    bad = condition(recordedDate="2026-03-01")
    err = _reject(svc, bad)
    receipt = svc.get_receipt(err.receipt_id)
    log_case(log, "receipt-fields", receipt,
             "回执含资源坐标/原因/字段/判定依据；被拒数据未入库")
    assert receipt["resource"] == "Condition/c1"
    assert receipt["reason"] == "field_not_defined_for_version"
    assert receipt["field"] == "recordedDate"
    assert receipt["basis"]  # 判定依据非空
    assert receipt["status"] == "open"
    with pytest.raises(InteropError) as ei:
        svc.store.get("Condition", "c1")
    assert ei.value.reason is RejectReason.RESOURCE_NOT_FOUND


def test_same_input_resubmitted_returns_same_receipt_and_conclusion(svc):
    bad = condition(recordedDate="2026-03-01")
    e1 = _reject(svc, bad)
    e2 = _reject(svc, bad)
    r1 = svc.get_receipt(e1.receipt_id)
    r2 = svc.get_receipt(e2.receipt_id)
    log_case(log, "receipt-stable",
             {"id1": e1.receipt_id, "id2": e2.receipt_id},
             "同一输入反复提交只产生同一条回执，结论一致")
    assert e1.receipt_id == e2.receipt_id
    assert (r1["reason"], r1["field"], r1["detail"]) == (r2["reason"], r2["field"], r2["detail"])
    assert len(svc.list_receipts()["receipts"]) == 1


def test_batch_failures_each_have_separate_queryable_receipts(svc):
    result = svc.ingest_batch([
        observation("o1"),                          # 成功
        observation("o2", subject="Patient/ghost"),  # broken_reference
        observation("o3", code="00000-0"),           # unknown_code
        condition(rid="c2", recordedDate="x"),       # 版本错位字段
        observation("o4"),                          # 成功
    ])
    failed = {f["index"]: f for f in result["failed"]}
    log_case(log, "batch-receipts", result["failed"],
             "批量中每条失败都带独立 receipt_id，可逐条查询；成功条目照常入库")
    assert [s["index"] for s in result["succeeded"]] == [0, 4]
    assert set(failed) == {1, 2, 3}
    receipt_ids = [failed[i]["receipt_id"] for i in (1, 2, 3)]
    assert len(set(receipt_ids)) == 3
    for rid in receipt_ids:
        receipt = svc.get_receipt(rid)
        assert receipt["batch_index"] is not None
    assert svc.store.get("Observation", "o1").version == 1
    assert svc.store.get("Observation", "o4").version == 1


def test_rejected_data_does_not_pollute_existing_version(svc):
    svc.ingest(condition(rid="c1"))
    assert svc.store.get("Condition", "c1").version == 1
    # 对 c1 的非法更新/创建被拒后，已有版本不受污染
    bad_update = condition(rid="c1", recordedDate="2026-03-01")
    _reject(svc, bad_update)
    record = svc.store.get("Condition", "c1")
    log_case(log, "no-pollution",
             {"version": record.version, "recordedDate": record.resource.payload["recordedDate"]},
             "被拒写入不留痕到主数据，已有版本号与内容不变")
    assert record.version == 1
    assert record.resource.payload["recordedDate"] is None


def test_receipt_never_leaks_sensitive_values(svc):
    # 畸形 Patient：detail 会回显输入形态，回执仍不得含姓名/出生日期/电话原文
    bad = {**patient("p9"), "name": "张三", "ssn": "leak-field"}
    err = _reject(svc, bad)
    receipt = svc.get_receipt(err.receipt_id)
    rendered = str(receipt)
    log_case(log, "receipt-scrub",
             {"reason": receipt["reason"], "contains_name": "张" in rendered,
              "contains_phone": "13800000000" in rendered},
             "回执文本中敏感原值必须全部遮蔽为 REDACTED 占位符")
    assert "张" not in rendered
    assert "1980-01-01" not in rendered
    assert "13800000000" not in rendered


def test_corrected_replay_runs_full_chain_and_resolves(svc):
    bad = condition(recordedDate="2026-03-01")
    err = _reject(svc, bad)
    receipt_id = err.receipt_id

    # 第一次修正仍不合格：走完整校验链路，被再次拒绝并产生新回执，原回执保持 open
    still_bad = {**bad, "recordedDate": "2026-04-01"}
    with pytest.raises(InteropError) as ei:
        svc.replay(receipt_id, still_bad)
    assert ei.value.reason is RejectReason.FIELD_NOT_DEFINED_FOR_VERSION
    assert ei.value.receipt_id != receipt_id
    assert svc.get_receipt(receipt_id)["status"] == "open"

    fixed = {k: v for k, v in bad.items() if k != "recordedDate"}
    result = svc.replay(receipt_id, fixed)
    log_case(log, "replay-ok", result,
             "修正后重放走同一校验链路，成功入库，回执变 resolved")
    assert result["outcome"] == "resolved"
    assert result["key"] == "Condition/c1"
    assert result["receipt"]["status"] == "resolved"
    assert svc.get_receipt(receipt_id)["status"] == "resolved"
    assert svc.store.get("Condition", "c1").version == 1


def test_resolved_receipt_replay_does_not_write_again(svc):
    bad = condition(recordedDate="2026-03-01")
    err = _reject(svc, bad)
    fixed = {k: v for k, v in bad.items() if k != "recordedDate"}
    svc.replay(err.receipt_id, fixed)
    with pytest.raises(InteropError) as ei:
        svc.replay(err.receipt_id, dict(fixed))
    log_case(log, "replay-twice", ei.value.to_dict(),
             "已解决回执再重放得到可区分结果 RECEIPT_ALREADY_RESOLVED，不重复入库")
    assert ei.value.reason is RejectReason.RECEIPT_ALREADY_RESOLVED
    assert svc.store.get("Condition", "c1").version == 1


def test_replay_target_mismatch_is_rejected(svc):
    bad = condition(rid="c1", recordedDate="2026-03-01")
    err = _reject(svc, bad)
    wrong = {k: v for k, v in bad.items() if k != "recordedDate"}
    wrong["id"] = "c-other"
    with pytest.raises(InteropError) as ei:
        svc.replay(err.receipt_id, wrong)
    assert ei.value.reason is RejectReason.RECEIPT_TARGET_MISMATCH
    assert svc.get_receipt(err.receipt_id)["status"] == "open"


def test_open_and_resolved_receipts_are_listed_separately(svc):
    e1 = _reject(svc, condition(rid="c1", recordedDate="2026-03-01"))
    e2 = _reject(svc, condition(rid="c2", recordedDate="2026-03-01"))
    fixed = {k: v for k, v in condition(rid="c1", recordedDate="2026-03-01").items()
             if k != "recordedDate"}
    svc.replay(e1.receipt_id, fixed)
    open_list = svc.list_receipts(status="open")["receipts"]
    resolved_list = svc.list_receipts(status="resolved")["receipts"]
    log_case(log, "receipt-list",
             {"open": [r["receipt_id"] for r in open_list],
              "resolved": [r["receipt_id"] for r in resolved_list]},
             "待处理列表可按 open/resolved 区分")
    assert [r["receipt_id"] for r in open_list] == [e2.receipt_id]
    assert [r["receipt_id"] for r in resolved_list] == [e1.receipt_id]


def test_concurrent_replays_exactly_one_takes_effect(svc):
    bad = condition(rid="c9", recordedDate="2026-03-01")
    err = _reject(svc, bad)
    receipt_id = err.receipt_id
    fixed = {k: v for k, v in bad.items() if k != "recordedDate"}
    n_threads = 8
    outcomes: list[object] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        try:
            svc.replay(receipt_id, fixed)
            outcomes.append("resolved")
        except InteropError as e:
            outcomes.append(e.reason)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    losers = [o for o in outcomes if o in (RejectReason.RECEIPT_RESOLVED_ELSEWHERE,
                                           RejectReason.RECEIPT_ALREADY_RESOLVED)]
    log_case(log, "concurrent-replay",
             {"threads": n_threads, "outcomes": outcomes},
             "恰好一个 resolved，其余拿到可区分的已解决/被他方解决结果，无重复写入")
    assert outcomes.count("resolved") == 1
    assert len(losers) == n_threads - 1
    assert svc.store.get("Condition", "c9").version == 1


def test_receipt_endpoints_over_http(client):
    bad = condition(recordedDate="2026-03-01")
    r = client.post("/resources", json=bad)
    assert r.status_code == 422
    body = r.json()["error"]
    receipt_id = body["receipt_id"]
    assert body["reason"] == "field_not_defined_for_version"

    r = client.get(f"/receipts/{receipt_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "open"

    fixed = {k: v for k, v in bad.items() if k != "recordedDate"}
    r = client.post(f"/receipts/{receipt_id}/replay", json=fixed)
    assert r.status_code == 200
    assert r.json()["outcome"] == "resolved"

    r = client.post(f"/receipts/{receipt_id}/replay", json=fixed)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "receipt_already_resolved"


@pytest.fixture
def client(svc):
    from fastapi.testclient import TestClient

    from interop import api
    api.service = svc
    return TestClient(api.app, raise_server_exceptions=False)
