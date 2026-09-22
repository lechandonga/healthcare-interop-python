"""标准版本协商与 HTTP 头解析。"""
from __future__ import annotations

from app.errors import RejectReason, RejectionError
from app.models import SUPPORTED_VERSIONS

# 接受的版本标记：Content-Type 中的 fhirVersion，或独立头 X-FHIR-Version
FHIR_VERSION_HEADER = "x-fhir-version"

# 允许的别名 -> 规范版本号
_VERSION_ALIASES: dict[str, str] = {
    "3.0": "3.0",
    "3": "3.0",
    "dstu3": "3.0",
    "stu3": "3.0",
    "fhir-3.0": "3.0",
    "4.0": "4.0",
    "4": "4.0",
    "r4": "4.0",
    "fhir-4.0": "4.0",
    "5.0": "5.0",
    "5": "5.0",
    "r5": "5.0",
    "fhir-5.0": "5.0",
}


def _normalize(raw: str) -> str:
    key = raw.strip().lower()
    canonical = _VERSION_ALIASES.get(key)
    if canonical is None:
        raise RejectionError(
            RejectReason.UNSUPPORTED_VERSION,
            f"不支持的 FHIR 标准版本: {raw!r}",
            field="fhirVersion",
            details={"supplied": raw, "supported": list(SUPPORTED_VERSIONS)},
        )
    return canonical


def resolve_fhir_version(content_type: str | None, fhir_header: str | None) -> str:
    """确定请求使用的 FHIR 标准版本。

    解析顺序：``X-FHIR-Version`` 头优先，其次 ``Content-Type`` 的
    ``fhirVersion`` 参数。

    * 完全缺失版本信息 -> ``MISSING_VERSION``（不假设默认版本）
    * 版本不在支持范围内 -> ``UNSUPPORTED_VERSION``
    """
    if fhir_header and fhir_header.strip():
        return _normalize(fhir_header)

    if content_type:
        # 例: application/fhir+json; charset=utf-8; fhirVersion=4.0
        for part in content_type.split(";"):
            item = part.strip()
            if item.lower().startswith("fhirversion="):
                value = item.split("=", 1)[1].strip().strip('"')
                if value:
                    return _normalize(value)

    raise RejectionError(
        RejectReason.MISSING_VERSION,
        "请求未声明 FHIR 标准版本：请通过 X-FHIR-Version 头或 "
        "Content-Type 的 fhirVersion 参数指定",
        field="fhirVersion",
        details={"supported": list(SUPPORTED_VERSIONS)},
    )
