"""可区分的拒绝原因：所有校验/写入失败都必须抛出带分类的错误，不得静默跳过。"""

from __future__ import annotations

import enum


class RejectReason(enum.Enum):
    """拒绝原因分类。每类原因在 API 与日志中都可区分。"""

    # 解析与结构
    MALFORMED_JSON = "malformed_json"                # 输入不是合法 JSON
    UNSUPPORTED_STANDARD_VERSION = "unsupported_standard_version"  # 不支持的标准版本
    UNSUPPORTED_RESOURCE_TYPE = "unsupported_resource_type"        # 不支持的资源类型
    MISSING_REQUIRED_FIELD = "missing_required_field"              # 必填字段缺失
    INVALID_FIELD_TYPE = "invalid_field_type"                      # 字段类型非法
    ILLEGAL_CODING = "illegal_coding"                              # 编码结构非法（如缺 system/code）
    FIELD_NOT_DEFINED_FOR_VERSION = "field_not_defined_for_version"  # 声明版本里不该出现的字段（其它版本才有）
    UNKNOWN_FIELD = "unknown_field"                                # 服务完全未定义的字段

    # 术语映射
    UNKNOWN_CODE = "unknown_code"                    # 本地码表无此代码
    MAPPING_CONFLICT = "mapping_conflict"            # 同一源代码映射到多个目标，冲突
    CODE_SYSTEM_VERSION_INCOMPATIBLE = "code_system_version_incompatible"  # 码表版本不兼容

    # 引用与版本兼容
    BROKEN_REFERENCE = "broken_reference"            # 引用目标不存在
    REFERENCE_VERSION_MISMATCH = "reference_version_mismatch"      # 引用目标标准版本不兼容

    # 去标识化与权限
    UNAUTHORIZED_REIDENTIFICATION = "unauthorized_reidentification"  # 未授权还原
    INVALID_TOKEN = "invalid_token"                  # 还原令牌无效

    # 并发与存储
    VERSION_CONFLICT = "version_conflict"            # 乐观锁冲突（并发修改）
    RESOURCE_NOT_FOUND = "resource_not_found"        # 资源不存在
    DUPLICATE_WRITE = "duplicate_write"              # 幂等键重复写入

    # 拒绝回执与修正重放
    RECEIPT_NOT_FOUND = "receipt_not_found"          # 回执不存在
    RECEIPT_ALREADY_RESOLVED = "receipt_already_resolved"  # 回执已解决，重放不得重复生效
    RECEIPT_RESOLVED_ELSEWHERE = "receipt_resolved_elsewhere"  # 并发重放时已被其它调用方解决
    RECEIPT_TARGET_MISMATCH = "receipt_target_mismatch"  # 重放记录与回执指向的资源不一致


class InteropError(Exception):
    """携带可区分拒绝原因的业务错误。"""

    def __init__(self, reason: RejectReason, detail: str, *, field: str | None = None,
                 receipt_id: str | None = None, basis: str | None = None):
        self.reason = reason
        self.detail = detail
        self.field = field
        self.receipt_id = receipt_id
        self.basis = basis
        super().__init__(f"[{reason.value}] {detail}" + (f" (field={field})" if field else ""))

    def to_dict(self) -> dict:
        out = {"reason": self.reason.value, "detail": self.detail, "field": self.field}
        if self.receipt_id is not None:
            out["receipt_id"] = self.receipt_id
        return out
