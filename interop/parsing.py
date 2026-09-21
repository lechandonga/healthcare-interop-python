"""多标准版本输入的解析与归一。

设计要点：
- 输入为 JSON 信封：{"standard_version": "fhir-r4"|"fhir-r5", "resourceType": ..., "id": ..., ...}
- 版本相关的字段差异在此集中处理（见 README「兼容规则」）：
  * R4 Observation 使用 effectiveDateTime；R5 使用 effectivePeriod {start, end}
  * 两者统一归一为 payload["effective"] = {"type": ..., "start": ..., "end": ...}
- 任何字段缺失 / 编码非法 / 结构不完整都抛出带 RejectReason 的 InteropError，绝不静默跳过。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource, ResourceType, StandardVersion

_GENDER_VALUES = {"male", "female", "other", "unknown"}
_OBS_STATUS = {
    StandardVersion.FHIR_R4: {"registered", "preliminary", "final", "amended"},
    # R5 在本服务支持子集中新增 corrected / cancelled
    StandardVersion.FHIR_R5: {"registered", "preliminary", "final", "amended", "corrected", "cancelled"},
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_REF_RE = re.compile(r"^(Patient|Observation)/[A-Za-z0-9_-]+$")

# 敏感字段：进入存储前会被抽取，用于去标识化
SENSITIVE_PATIENT_FIELDS = ("name", "telecom", "birthDate")


def _fail(reason: RejectReason, detail: str, field: str | None = None) -> None:
    raise InteropError(reason, detail, field=field)


def _require(obj: dict, field: str, ctx: str) -> Any:
    if field not in obj or obj[field] is None:
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx} 缺少必填字段 {field!r}", field)
    return obj[field]


def _require_type(obj: dict, field: str, types: tuple, ctx: str) -> Any:
    value = _require(obj, field, ctx)
    if not isinstance(value, types) or isinstance(value, bool) and bool not in types:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx} 字段 {field!r} 类型非法: 期望 {types}, 实际 {type(value).__name__}", field)
    return value


def _parse_coding(raw: Any, ctx: str) -> Coding:
    """解析 coding 结构；缺 system/code 视为编码非法。"""
    if not isinstance(raw, dict):
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding 必须是对象", "code")
    system = raw.get("system")
    code = raw.get("code")
    if not isinstance(system, str) or not system:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.system 缺失或为空", "code.system")
    if not isinstance(code, str) or not code:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.code 缺失或为空", "code.code")
    display = raw.get("display")
    if display is not None and not isinstance(display, str):
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.display 必须是字符串", "code.display")
    return Coding(system=system, code=code, display=display)


def _parse_codeable(raw: Any, ctx: str) -> Coding:
    if not isinstance(raw, dict) or not isinstance(raw.get("coding"), list) or not raw["coding"]:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: code.coding 必须是非空数组", "code")
    return _parse_coding(raw["coding"][0], ctx)


def _parse_reference(raw: Any, ctx: str) -> str:
    if not isinstance(raw, dict) or not isinstance(raw.get("reference"), str):
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx}: 引用必须是 {{'reference': 'Type/id'}}", "reference")
    ref = raw["reference"]
    if not _REF_RE.match(ref):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: 引用格式非法: {ref!r}", "reference")
    return ref


def _normalize_patient(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Patient/{rid}"
    name = _require_type(body, "name", (list,), ctx)
    if not name or not all(isinstance(n, dict) and n.get("family") for n in name):
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx}: name 必须是非空数组且每项含 family", "name")
    gender = _require_type(body, "gender", (str,), ctx)
    if gender not in _GENDER_VALUES:
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: gender 取值非法: {gender!r}", "gender")
    birth = _require_type(body, "birthDate", (str,), ctx)
    if not _DATE_RE.match(birth):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: birthDate 须为 YYYY-MM-DD: {birth!r}", "birthDate")
    telecom = body.get("telecom", [])
    if not isinstance(telecom, list):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: telecom 必须是数组", "telecom")

    sensitive = {"name": name, "birthDate": birth, "telecom": telecom}
    payload = {"gender": gender}
    return NormalizedResource(
        resource_type=ResourceType.PATIENT, resource_id=rid, standard_version=version,
        payload=payload, references=[], sensitive_fields=sensitive,
    )


def _normalize_observation(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Observation/{rid}"
    status = _require_type(body, "status", (str,), ctx)
    allowed = _OBS_STATUS[version]
    if status not in allowed:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: status {status!r} 在 {version.value} 中不允许（允许: {sorted(allowed)}）", "status")
    code = _parse_codeable(_require(body, "code", ctx), ctx)
    subject_ref = _parse_reference(_require(body, "subject", ctx), ctx)

    # 版本差异：R4 用 effectiveDateTime，R5 用 effectivePeriod
    if version is StandardVersion.FHIR_R4:
        eff = _require_type(body, "effectiveDateTime", (str,), ctx)
        if not _DATETIME_RE.match(eff):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: effectiveDateTime 格式非法: {eff!r}",
                  "effectiveDateTime")
        effective = {"type": "dateTime", "start": eff, "end": eff}
    else:
        period = _require_type(body, "effectivePeriod", (dict,), ctx)
        start = _require_type(period, "start", (str,), f"{ctx}.effectivePeriod")
        end = _require_type(period, "end", (str,), f"{ctx}.effectivePeriod")
        if not (_DATETIME_RE.match(start) and _DATETIME_RE.match(end)):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: effectivePeriod start/end 格式非法",
                  "effectivePeriod")
        effective = {"type": "Period", "start": start, "end": end}

    value = body.get("valueQuantity")
    if value is not None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), (int, float)):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: valueQuantity.value 必须是数值", "valueQuantity")
        value = {"value": value["value"], "unit": value.get("unit"), "code": value.get("code")}

    payload = {
        "status": status,
        "code": {"system": code.system, "code": code.code, "display": code.display},
        "effective": effective,
        "valueQuantity": value,
    }
    return NormalizedResource(
        resource_type=ResourceType.OBSERVATION, resource_id=rid, standard_version=version,
        payload=payload, references=[subject_ref], sensitive_fields={},
    )


def parse_resource(raw: str | bytes | dict) -> NormalizedResource:
    """解析原始输入为归一化资源。任何失败都抛出带分类原因的 InteropError。"""
    if isinstance(raw, (str, bytes)):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _fail(RejectReason.MALFORMED_JSON, f"输入不是合法 JSON: {e}")
    elif isinstance(raw, dict):
        body = raw
    else:
        _fail(RejectReason.MALFORMED_JSON, f"输入类型不支持: {type(raw).__name__}")

    if not isinstance(body, dict):
        _fail(RejectReason.MALFORMED_JSON, "顶层结构必须是 JSON 对象")

    version = StandardVersion.parse(_require_type(body, "standard_version", (str,), "信封"))
    rtype = ResourceType.parse(_require_type(body, "resourceType", (str,), "信封"))
    rid = _require_type(body, "id", (str,), "信封")
    if not re.match(r"^[A-Za-z0-9_-]+$", rid):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"资源 id 含非法字符: {rid!r}", "id")

    if rtype is ResourceType.PATIENT:
        return _normalize_patient(body, version, rid)
    return _normalize_observation(body, version, rid)
