"""并发写入/更新：结果必须一致，无半更新、无覆盖、无重复写入。"""
from __future__ import annotations

import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app.dependencies import reset_service_for_tests
from app.models import NormalizedResource, ResourceKind

logger = logging.getLogger("healthcare_interop.test")


def test_concurrent_updates_only_one_succeeds(service):
    """10 个线程基于同一初始版本并发更新：恰好 1 个成功，其余 VERSION_CONFLICT。"""
    svc = service
    svc.create_resource(
        {"resourceType": "Patient", "id": "p1", "gender": "male"},
        "4.0",
        _admin(),
    )
    outcomes: list[str] = []
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait()  # 尽量放大竞争
        try:
            svc.update_resource(
                "p1",
                {"resourceType": "Patient", "id": "p1",
                 "gender": "female", "active": i % 2 == 0},
                "4.0",
                expected_version=1,
                principal=_admin(),
            )
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info("并发更新样本(线程数=10, 基线版本=1) 判定=%s", outcomes)
    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("RejectionError") == 9, outcomes
    stored = svc.store.get("p1")
    assert stored.version == 2  # 只递增一次，无重复更新


def test_concurrent_creates_never_duplicate(service):
    """并发创建同一 ID：恰好 1 个成功，其余 ALREADY_EXISTS，无重复写入。"""
    svc = service
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            svc.create_resource(
                {"resourceType": "Patient", "id": "dup", "gender": "male"},
                "4.0",
                _admin(),
            )
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc.__class__.__name__)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("RejectionError") == 7
    stored = svc.store.get("dup")
    assert stored.version == 1


def test_no_partial_update_under_contention(service):
    """竞争结束后最终状态必须是某个完整提交，而非字段混合的半更新。"""
    svc = service
    svc.create_resource(
        {"resourceType": "Patient", "id": "p2", "gender": "male",
         "active": True},
        "4.0", _admin(),
    )

    stop = threading.Event()
    errors: list[str] = []

    def writer(gender: str, active: bool, base_versions: list[int]) -> None:
        # 每个 writer 先读最新版本再更新，形成串行的成功链
        while not stop.is_set():
            cur = svc.store.get("p2").version
            try:
                result = svc.update_resource(
                    "p2",
                    {"resourceType": "Patient", "id": "p2",
                     "gender": gender, "active": active},
                    "4.0", cur, _admin(),
                )
                base_versions.append(result["meta"]["version"])
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(exc.__class__.__name__)

    versions_a: list[int] = []
    versions_b: list[int] = []
    t1 = threading.Thread(target=writer, args=("female", False, versions_a))
    t2 = threading.Thread(target=writer, args=("other", True, versions_b))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)
    stop.set()

    final = svc.read_resource("p2", _admin())["data"]
    logger.info("竞争写后最终样本=%s 冲突次数=%d", final, len(errors))
    # 完整性：两个字段必须来自同一次提交的配对值
    assert (final["gender"], final["active"]) in {
        ("female", False), ("other", True)
    }, final
    assert svc.store.get("p2").version >= 2


def test_http_concurrent_put_one_wins():
    """HTTP 层并发 PUT（相同 If-Match）：仅 1 个 200，其余 409。"""
    from main import app

    svc = reset_service_for_tests()
    svc.create_resource(
        {"resourceType": "Patient", "id": "p1", "gender": "male"},
        "4.0", _admin(),
    )
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-admin",
        "X-FHIR-Version": "4.0",
        "If-Match": 'W/"1"',
    }
    statuses: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def call() -> None:
        barrier.wait()
        r = client.put(
            "/fhir/Patient/p1",
            json={"resourceType": "Patient", "id": "p1", "gender": "female"},
            headers=headers,
        )
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info("HTTP 并发 PUT 样本(If-Match=W/1) 判定状态码=%s", statuses)
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 5, statuses


def test_version_chain_is_monotonic(service):
    """串行更新链：版本严格单调，每次读取都是完整快照。"""
    svc = service
    svc.create_resource(make_patient_body(), "4.0", _admin())
    for n in range(1, 21):
        result = svc.update_resource(
            "p1", make_patient_body(active=(n % 2 == 0)),
            "4.0", n, _admin(),
        )
        assert result["meta"]["version"] == n + 1
    assert svc.store.get("p1").version == 21


def _admin():
    from app.security import authenticate
    return authenticate("token-admin")


def make_patient_body(active: bool | None = None) -> dict:
    body = {"resourceType": "Patient", "id": "p1", "gender": "male"}
    if active is not None:
        body["active"] = active
    return body
