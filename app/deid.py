"""敏感字段去标识化与受控还原。

* :func:`redact_resource` 把敏感字段替换为不可逆掩码标记
  ``<redacted:字段路径>``，使调用方能区分“字段存在但被脱敏”与
  “字段本身不存在”，且从返回值无法反推原始值。
* 受控还原不是本模块的职责：服务层仅在持有 ``reidentify:read``
  权限时直接返回原始视图；未授权路径根本不接触原始数据。
"""
from __future__ import annotations

import copy
from typing import Any

from app.models import NormalizedResource, ResourceKind

# 各资源类型下的敏感字段（归一化 data 中的点路径）
SENSITIVE_FIELDS: dict[ResourceKind, frozenset[str]] = {
    ResourceKind.PATIENT: frozenset(
        {"name", "telecom", "birthDate", "address", "identifier"}
    ),
    ResourceKind.OBSERVATION: frozenset(),
    ResourceKind.CONDITION: frozenset(),
}

REDACT_PREFIX = "<redacted:"
REDACT_SUFFIX = ">"


def redaction_marker(path: str) -> str:
    return f"{REDACT_PREFIX}{path}{REDACT_SUFFIX}"


def _redact_at(obj: Any, path: str, sensitive: frozenset[str]) -> Any:
    if path in sensitive:
        return redaction_marker(path)
    if isinstance(obj, dict):
        return {
            k: _redact_at(v, f"{path}.{k}" if path else k, sensitive)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [
            _redact_at(v, f"{path}[{i}]", sensitive) for i, v in enumerate(obj)
        ]
    return obj


def redact_resource(resource: NormalizedResource) -> dict[str, Any]:
    """返回脱敏视图（深拷贝，不修改原始对象）。"""
    sensitive = SENSITIVE_FIELDS.get(resource.kind, frozenset())
    return _redact_at(copy.deepcopy(resource.data), "", sensitive)
