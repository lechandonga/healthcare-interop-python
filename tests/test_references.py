"""引用完整性：坏引用、悬挂引用、类型不匹配。"""
from __future__ import annotations

import logging

import pytest

from app.errors import RejectReason, RejectionError
from app.models import NormalizedResource, ResourceKind
from app.references import check_references
from app.storage import ResourceStore

logger = logging.getLogger("healthcare_interop.test")


def _obs(rid: str, refs: list[str]) -> NormalizedResource:
    return NormalizedResource(
        kind=ResourceKind.OBSERVATION, resource_id=rid,
        source_version="4.0", data={}, references=refs,
    )


def test_missing_target_rejected():
    store = ResourceStore()
    with pytest.raises(RejectionError) as ei:
        check_references(_obs("o1", ["Patient/ghost"]), store)
    assert ei.value.reason == RejectReason.REFERENCE_TARGET_NOT_FOUND


def test_existing_target_passes():
    store = ResourceStore()
    store.put_new(NormalizedResource(
        ResourceKind.PATIENT, "p1", "4.0", {}))
    check_references(_obs("o1", ["Patient/p1"]), store)


def test_type_mismatch_rejected():
    store = ResourceStore()
    store.put_new(NormalizedResource(
        ResourceKind.OBSERVATION, "x1", "4.0", {}))
    with pytest.raises(RejectionError) as ei:
        check_references(_obs("o1", ["Patient/x1"]), store)
    assert ei.value.reason == RejectReason.REFERENCE_BROKEN


@pytest.mark.parametrize("bad", ["Patient/", "/id", "nope", "Unknown/x", "a/b/c"])
def test_broken_reference_shapes(bad):
    store = ResourceStore()
    with pytest.raises(RejectionError) as ei:
        check_references(_obs("o1", [bad]), store)
    assert ei.value.reason == RejectReason.REFERENCE_BROKEN
    logger.info("坏引用样本=%r 判定=%s", bad, ei.value.reason)
