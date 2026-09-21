"""并发写入：同一资源并发修改结果必须一致——无半更新、无覆盖、无重复写入。"""

import logging
import threading

import pytest

from interop.errors import InteropError, RejectReason
from interop.service import InteropService
from tests.conftest import log_case

log = logging.getLogger("test.concurrency")

PATIENT = {"standard_version": "fhir-r4", "resourceType": "Patient", "id": "p1",
           "name": [{"family": "张"}], "gender": "male", "birthDate": "1980-01-01"}


def test_concurrent_updates_no_lost_update():
    """N 个线程基于同一版本并发更新：恰好 1 个成功，其余 VERSION_CONFLICT，
    最终版本 = 初始 + 成功数，无半更新。"""
    svc = InteropService()
    svc.ingest(PATIENT)
    n_threads = 16
    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        body = {**PATIENT, "gender": "female" if i % 2 else "male"}
        barrier.wait()  # 尽量同时进入临界区
        try:
            svc.update(body, expected_version=1)
            results.append(("ok", i))
        except InteropError as e:
            results.append(("err", e.reason))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "err" and r[1] is RejectReason.VERSION_CONFLICT]
    log_case(log, "concurrent-update",
             {"threads": n_threads, "ok": len(ok), "conflicts": len(conflicts)},
             "判定依据：恰好 1 个成功，其余全部 VERSION_CONFLICT，最终版本号为 2")
    assert len(ok) == 1
    assert len(ok) + len(conflicts) == n_threads
    assert svc.store.get("Patient", "p1").version == 2


def test_sequential_retry_after_conflict_succeeds():
    """冲突后重新读取版本再更新，应成功——并发结论可收敛。"""
    svc = InteropService()
    svc.ingest(PATIENT)
    svc.update({**PATIENT, "gender": "female"}, expected_version=1)
    log_case(log, "stale-version", {"expected_version": 1},
             "基于过期版本更新应拒绝 VERSION_CONFLICT")
    with pytest.raises(InteropError) as ei:
        svc.update(PATIENT, expected_version=1)
    assert ei.value.reason is RejectReason.VERSION_CONFLICT
    current = svc.store.get("Patient", "p1").version
    _, ver = svc.update(PATIENT, expected_version=current)
    assert ver == current + 1


def test_idempotent_replay_no_duplicate_write():
    """同一幂等键重复提交：返回原记录，版本不前进，不产生重复写入。"""
    svc = InteropService()
    svc.ingest(PATIENT, idempotency_key="req-1")
    svc.ingest(PATIENT, idempotency_key="req-1")  # 网络重试重放
    svc.ingest(PATIENT, idempotency_key="req-1")
    record = svc.store.get("Patient", "p1")
    log_case(log, "idempotent-replay", {"idempotency_key": "req-1", "replays": 3},
             "判定依据：版本仍为 1，幂等键只应用一次")
    assert record.version == 1
    assert record.idempotency_keys == {"req-1"}


def test_duplicate_create_without_key_rejected():
    svc = InteropService()
    svc.ingest(PATIENT)
    log_case(log, "dup-create", PATIENT, "无幂等键重复创建应拒绝 DUPLICATE_WRITE，不覆盖已有数据")
    with pytest.raises(InteropError) as ei:
        svc.ingest(PATIENT)
    assert ei.value.reason is RejectReason.DUPLICATE_WRITE
    assert svc.store.get("Patient", "p1").version == 1


def test_concurrent_creates_single_winner():
    """N 个线程并发创建同一资源：恰好 1 个成功，其余 DUPLICATE_WRITE。"""
    svc = InteropService()
    n_threads = 8
    outcomes: list[object] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        try:
            svc.ingest(PATIENT)
            outcomes.append("ok")
        except InteropError as e:
            outcomes.append(e.reason)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(log, "concurrent-create", {"threads": n_threads, "outcomes": outcomes},
             "判定依据：恰好 1 个 ok，其余 DUPLICATE_WRITE，版本为 1")
    assert outcomes.count("ok") == 1
    assert outcomes.count(RejectReason.DUPLICATE_WRITE) == n_threads - 1
    assert svc.store.get("Patient", "p1").version == 1
