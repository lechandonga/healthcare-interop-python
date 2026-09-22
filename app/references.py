"""资源间引用完整性与版本兼容检查。"""
from __future__ import annotations

from app.errors import RejectReason, RejectionError
from app.models import NormalizedResource, ResourceKind
from app.storage import ResourceStore


def parse_reference(ref: str) -> tuple[str, str]:
    """解析 ``"ResourceType/id"``；非法 -> ``REFERENCE_BROKEN``。"""
    if not isinstance(ref, str) or "/" not in ref:
        raise RejectionError(
            RejectReason.REFERENCE_BROKEN,
            "引用必须形如 'ResourceType/id'",
            details={"reference": ref},
        )
    parts = ref.split("/")
    if len(parts) != 2 or not all(parts):
        raise RejectionError(
            RejectReason.REFERENCE_BROKEN,
            "引用必须形如 'ResourceType/id'，且类型与 ID 均非空",
            details={"reference": ref},
        )
    rtype, rid = parts
    try:
        ResourceKind(rtype)
    except ValueError:
        raise RejectionError(
            RejectReason.REFERENCE_BROKEN,
            f"引用了不支持的资源类型: {rtype}",
            details={"reference": ref, "resource_type": rtype},
        )
    return rtype, rid


def check_references(resource: NormalizedResource, store: ResourceStore) -> None:
    """校验资源所持引用的目标存在且类型匹配。

    * 目标不存在 -> ``REFERENCE_TARGET_NOT_FOUND``
    * 引用结构非法 -> ``REFERENCE_BROKEN``
    """
    for ref in resource.references:
        rtype, rid = parse_reference(ref)
        if not store.exists(rid):
            raise RejectionError(
                RejectReason.REFERENCE_TARGET_NOT_FOUND,
                f"引用目标不存在: {ref}",
                field="references",
                details={"missing_reference": ref},
            )
        target = store.get(rid)
        if target.resource.kind.value != rtype:
            # ID 存在但类型不符（例如指向了 Observation 的 ID）
            raise RejectionError(
                RejectReason.REFERENCE_BROKEN,
                f"引用 {ref} 的实际资源类型为 "
                f"{target.resource.kind.value}，与声明的 {rtype} 不一致",
                field="references",
                details={
                    "reference": ref,
                    "declared_type": rtype,
                    "actual_type": target.resource.kind.value,
                },
            )
