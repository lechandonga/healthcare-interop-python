"""术语码表本地映射、冲突与版本兼容（服务/单元级）。"""
from __future__ import annotations

import logging

import pytest

from app.errors import RejectReason, RejectionError
from app.models import Coding
from app.terminology import MappingConflict, MappingEntry, TerminologyService

logger = logging.getLogger("healthcare_interop.test")


@pytest.fixture
def term():
    t = TerminologyService()
    t.load_defaults()
    return t


def test_local_to_canonical_mapping(term):
    sample = Coding("http://local.example.org/codes/observation", "BP")
    out = term.map_code(sample, "4.0")
    assert out.system == "http://loinc.org"
    assert out.code == "85354-9"
    logger.info("映射样本=%r -> 判定=%s", sample, out)


def test_conflicting_mapping_refused(term):
    sample = Coding("http://local.example.org/codes/conflicting", "X1")
    with pytest.raises(RejectionError) as ei:
        term.map_code(sample, "4.0")
    assert ei.value.reason == RejectReason.MAPPING_CONFLICT
    logger.info("冲突样本=%r 判定依据=%s", sample, ei.value.details)


def test_duplicate_identical_mapping_is_idempotent(term):
    """相同映射重复登记幂等；不同映射才构成冲突。"""
    term.add_mapping(MappingEntry(
        "http://local.example.org/codes/observation", "BP",
        Coding("http://loinc.org", "85354-9")))
    with pytest.raises(MappingConflict):
        term.add_mapping(MappingEntry(
            "http://local.example.org/codes/observation", "BP",
            Coding("http://loinc.org", "2339-0")))


@pytest.mark.parametrize("version", ["3.0", "4.0", "5.0"])
def test_loinc_available_all_versions(term, version):
    out = term.validate_code(Coding("http://loinc.org", "85354-9"), version)
    assert out.code == "85354-9"


def test_code_version_range_enforced(term):
    sample = Coding("http://snomed.info/sct-stu3", "38341003")
    term.validate_code(sample, "3.0")  # 不抛
    with pytest.raises(RejectionError) as ei:
        term.validate_code(sample, "5.0")
    assert ei.value.reason == RejectReason.TERMINOLOGY_VERSION_INCOMPATIBLE
    logger.info("版本样本=%r@5.0 判定依据=%s", sample, ei.value.details)


def test_empty_coding_illegal(term):
    with pytest.raises(RejectionError) as ei:
        term.validate_code(Coding("", "1"), "4.0")
    assert ei.value.reason == RejectReason.ILLEGAL_CODE
