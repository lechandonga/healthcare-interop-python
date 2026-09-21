"""术语层：未知代码、映射冲突、版本不兼容必须显式标记。"""

import logging

import pytest

from interop.errors import InteropError, RejectReason
from interop.models import Coding
from interop.terminology import TerminologyService
from tests.conftest import log_case

log = logging.getLogger("test.terminology")


@pytest.fixture
def term():
    return TerminologyService()


def test_validate_known_code(term):
    sample = Coding("http://loinc.org", "8480-6")
    log_case(log, "validate-ok", sample, "LOINC 本地码表含 8480-6，应通过并补全 display")
    result = term.validate(sample)
    assert result.code == "8480-6" and result.display


def test_validate_unknown_code(term):
    sample = Coding("http://loinc.org", "99999-9")
    log_case(log, "validate-unknown", sample, "本地码表无此代码，应拒绝 UNKNOWN_CODE")
    with pytest.raises(InteropError) as ei:
        term.validate(sample)
    assert ei.value.reason is RejectReason.UNKNOWN_CODE


def test_validate_version_incompatible(term):
    sample = Coding("http://loinc.org|2.70", "8480-6")
    log_case(log, "validate-version", sample,
             "请求版本 2.70 != 本地 2.74，应拒绝 CODE_SYSTEM_VERSION_INCOMPATIBLE")
    with pytest.raises(InteropError) as ei:
        term.validate(sample)
    assert ei.value.reason is RejectReason.CODE_SYSTEM_VERSION_INCOMPATIBLE


def test_map_local_to_loinc(term):
    sample = Coding("http://local.example/clinical", "SBP")
    log_case(log, "map-ok", sample, "院内码 SBP 一一映射到 LOINC 8480-6")
    result = term.map_code(sample, "http://loinc.org")
    assert result.code == "8480-6"


def test_map_conflict_is_flagged_not_silently_picked(term):
    # 人为制造一对二映射冲突
    term.register_mapping("http://local.example/clinical", "1.0", "SBP",
                          "http://loinc.org", "11111-1")
    sample = Coding("http://local.example/clinical", "SBP")
    log_case(log, "map-conflict", sample,
             "SBP 映射到 8480-6 与 11111-1 两个目标，应拒绝 MAPPING_CONFLICT 而非取其一")
    with pytest.raises(InteropError) as ei:
        term.map_code(sample, "http://loinc.org")
    assert ei.value.reason is RejectReason.MAPPING_CONFLICT
    assert "8480-6" in ei.value.detail and "11111-1" in ei.value.detail


def test_map_no_mapping(term):
    sample = Coding("http://loinc.org", "8480-6")
    log_case(log, "map-none", sample, "LOINC->院内码 无映射，应拒绝 UNKNOWN_CODE")
    with pytest.raises(InteropError) as ei:
        term.map_code(sample, "http://local.example/clinical")
    assert ei.value.reason is RejectReason.UNKNOWN_CODE
