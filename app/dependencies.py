"""共享依赖：单例服务、请求鉴权、版本协商。"""
from __future__ import annotations

from fastapi import Header, Request

from app.security import Principal, authenticate
from app.service import InteropService
from app.versioning import resolve_fhir_version

_service: InteropService | None = None


def get_service() -> InteropService:
    global _service
    if _service is None:
        _service = InteropService()
    return _service


def reset_service_for_tests() -> InteropService:
    """测试辅助：重置单例及其存储。"""
    global _service
    _service = InteropService()
    return _service


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return authenticate(token)


def get_fhir_version(
    request: Request,
    content_type: str | None = Header(default=None),
    x_fhir_version: str | None = Header(default=None, alias="X-FHIR-Version"),
) -> str:
    return resolve_fhir_version(content_type, x_fhir_version)
