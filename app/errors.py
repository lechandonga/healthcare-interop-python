"""统一错误分类体系。

所有拒绝原因都必须映射到 :class:`RejectReason` 中可区分的错误码，
上层（API / 批处理）据此返回稳定、可复现的错误结构，不得静默跳过。
"""
from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    """可区分的拒绝原因。"""

    # —— 输入解析与结构 ——
    MALFORMED_JSON = "MALFORMED_JSON"
    NOT_A_RESOURCE_OBJECT = "NOT_A_RESOURCE_OBJECT"
    MISSING_FIELD = "MISSING_FIELD"
    EMPTY_FIELD = "EMPTY_FIELD"
    INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
    ILLEGAL_CODE = "ILLEGAL_CODE"
    STRUCTURE_INCOMPLETE = "STRUCTURE_INCOMPLETE"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"

    # —— 标准版本与字段兼容 ——
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    MISSING_VERSION = "MISSING_VERSION"
    UNSUPPORTED_RESOURCE_TYPE = "UNSUPPORTED_RESOURCE_TYPE"
    INCOMPATIBLE_FIELD = "INCOMPATIBLE_FIELD"

    # —— 术语码表 ——
    CODE_SYSTEM_UNKNOWN = "CODE_SYSTEM_UNKNOWN"
    CODE_NOT_FOUND = "CODE_NOT_FOUND"
    MAPPING_CONFLICT = "MAPPING_CONFLICT"
    TERMINOLOGY_VERSION_INCOMPATIBLE = "TERMINOLOGY_VERSION_INCOMPATIBLE"

    # —— 引用完整性 ——
    REFERENCE_TARGET_NOT_FOUND = "REFERENCE_TARGET_NOT_FOUND"
    REFERENCE_BROKEN = "REFERENCE_BROKEN"

    # —— 去标识化与权限 ——
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"

    # —— 并发 ——
    VERSION_CONFLICT = "VERSION_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"

    # —— 批处理 / 通用 ——
    BATCH_ENTRY_INVALID = "BATCH_ENTRY_INVALID"


class RejectionError(Exception):
    """携带可区分原因的拒绝异常。

    :param reason: 稳定错误码
    :param message: 人类可读说明（禁止包含原始敏感值）
    :param field: 出错字段路径（如 ``"code.coding[0].code"``）
    :param details: 可复现的结构化判定依据
    """

    def __init__(
        self,
        reason: RejectReason,
        message: str,
        *,
        field: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict:
        out: dict = {"reason": self.reason.value, "message": self.message}
        if self.field:
            out["field"] = self.field
        if self.details:
            out["details"] = self.details
        return out
