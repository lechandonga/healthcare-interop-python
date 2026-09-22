"""认证与授权（本地令牌 -> 权限范围），以及审计日志。

令牌表（仅用于本地模拟）：

``token-deid``         -> {"deid:read"}
``token-clinician``    -> {"patient:read", "resource:write", "deid:read"}
``token-reidentify``   -> {"reidentify:read", "patient:read", "resource:write", "deid:read"}
``token-admin``        -> {"*"}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.errors import RejectReason, RejectionError

logger = logging.getLogger("healthcare_interop.audit")


@dataclass(frozen=True)
class Principal:
    token: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


TOKEN_TABLE: dict[str, frozenset[str]] = {
    "token-deid": frozenset({"deid:read"}),
    "token-clinician": frozenset(
        {"patient:read", "resource:write", "deid:read"}
    ),
    "token-reidentify": frozenset(
        {"reidentify:read", "patient:read", "resource:write", "deid:read"}
    ),
    "token-admin": frozenset({"*"}),
}

# 令牌名（仅用于审计中的稳定主体标识，不泄漏令牌本身）
_TOKEN_NAMES: dict[str, str] = {
    "token-deid": "deid-client",
    "token-clinician": "clinician",
    "token-reidentify": "privileged-reidentifier",
    "token-admin": "admin",
}


def authenticate(token: str | None) -> Principal:
    """缺失/未知令牌 -> ``UNAUTHORIZED``。"""
    if not token:
        raise RejectionError(
            RejectReason.UNAUTHORIZED,
            "缺少 Bearer 令牌",
            field="Authorization",
        )
    scopes = TOKEN_TABLE.get(token)
    if scopes is None:
        # 不回显令牌内容，避免泄漏
        raise RejectionError(
            RejectReason.UNAUTHORIZED,
            "令牌无效或已注销",
            field="Authorization",
        )
    return Principal(token=token, scopes=scopes)


def require(principal: Principal, scope: str) -> None:
    """权限不足 -> ``FORBIDDEN``。"""
    if not principal.has(scope):
        raise RejectionError(
            RejectReason.FORBIDDEN,
            f"主体缺少所需权限: {scope}",
            details={"required_scope": scope},
        )


def audit(principal: Principal, action: str, **fields: object) -> None:
    """记录审计日志（不记录任何原始敏感值）。

    调用方必须确保 ``fields`` 中不含姓名、出生日期、联系方式等原始值；
    只记录资源 ID、版本、判定结果等元数据。
    """
    logger.info(
        "audit action=%s principal=%s scopes=%s %s",
        action,
        _TOKEN_NAMES.get(principal.token, "unknown"),
        sorted(principal.scopes),
        " ".join(f"{k}={v}" for k, v in fields.items()),
    )
