"""敏感字段去标识化与受控还原。

- deidentify: 返回去标识化副本，敏感字段以不可逆哈希占位符替换，原值只留在
  本服务的保密库中（本地模拟 KMS），不随副本外泄。
- reidentify: 必须持有效且具 "clinical" 角色的令牌；未知令牌 -> INVALID_TOKEN，
  角色不足 -> UNAUTHORIZED_REIDENTIFICATION。两种失败可区分，且都不泄漏原值。
"""

from __future__ import annotations

import dataclasses
import hashlib
import secrets

from .errors import InteropError, RejectReason
from .models import NormalizedResource

_AUTHORIZED_ROLE = "clinical"


def _redact(value: object) -> str:
    digest = hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:12]
    return f"REDACTED:{digest}"


def scrub_sensitive_text(text: str, secret_values: list[str]) -> str:
    """把拒绝回执文本中可能出现的敏感原值替换为 REDACTED 占位符。

    secret_values 是从原始输入中抽取的敏感字段内容的稳定序列化串；
    回执 detail 里若回显了这些值（如畸形结构里的姓名/电话），一律遮蔽，
    保证回执可查但不泄漏敏感原文。
    """
    scrubbed = text
    for secret in secret_values:
        if secret and len(secret) >= 2 and secret in scrubbed:
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
            scrubbed = scrubbed.replace(secret, f"REDACTED:{digest}")
    return scrubbed


class DeidentificationService:
    def __init__(self) -> None:
        self._vault: dict[str, dict] = {}          # resource_key -> 敏感字段原值
        self._tokens: dict[str, dict] = {}         # token -> {"key":..., "role":...}

    # ---- 令牌管理（本地模拟授权服务） ----
    def issue_token(self, resource_key: str, role: str) -> str:
        token = secrets.token_urlsafe(24)
        self._tokens[token] = {"key": resource_key, "role": role}
        return token

    def revoke_token(self, token: str) -> None:
        self._tokens.pop(token, None)

    # ---- 去标识化 / 还原 ----
    def deidentify(self, resource: NormalizedResource) -> NormalizedResource:
        """返回去标识化副本；原值存入保密库，副本中只留哈希占位符。"""
        if not resource.sensitive_fields:
            return resource
        key = resource.reference_key()
        self._vault[key] = dict(resource.sensitive_fields)
        redacted = {name: _redact(value) for name, value in resource.sensitive_fields.items()}
        return dataclasses.replace(resource, sensitive_fields=redacted)

    def reidentify(self, resource: NormalizedResource, token: str) -> NormalizedResource:
        """凭令牌还原敏感字段。任何失败都不泄漏原值。"""
        grant = self._tokens.get(token)
        if grant is None:
            raise InteropError(RejectReason.INVALID_TOKEN, "还原令牌无效或已撤销")
        if grant["role"] != _AUTHORIZED_ROLE:
            raise InteropError(
                RejectReason.UNAUTHORIZED_REIDENTIFICATION,
                f"角色 {grant['role']!r} 无权还原敏感字段（需要 {_AUTHORIZED_ROLE!r}）")
        key = resource.reference_key()
        if grant["key"] != key:
            raise InteropError(
                RejectReason.UNAUTHORIZED_REIDENTIFICATION,
                f"令牌授权范围不含资源 {key!r}")
        original = self._vault.get(key)
        if original is None:
            raise InteropError(RejectReason.RESOURCE_NOT_FOUND,
                               f"资源 {key!r} 无保密库记录，无法还原")
        return dataclasses.replace(resource, sensitive_fields=dict(original))
