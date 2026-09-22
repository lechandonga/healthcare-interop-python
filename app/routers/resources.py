"""资源写入 / 更新 / 读取 HTTP 接口。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response

from app.dependencies import get_fhir_version, get_principal, get_service
from app.errors import RejectReason, RejectionError
from app.security import Principal

router = APIRouter(prefix="/fhir", tags=["resources"])
logger = logging.getLogger("healthcare_interop.api")


async def _read_json(request: Request) -> object:
    raw = await request.body()
    if not raw:
        raise RejectionError(
            RejectReason.MALFORMED_JSON,
            "请求体为空",
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = {}
        if isinstance(exc, json.JSONDecodeError):
            detail = {"position": exc.pos, "line": exc.lineno, "col": exc.colno}
        raise RejectionError(
            RejectReason.MALFORMED_JSON,
            f"请求体不是合法 JSON: {exc.msg if isinstance(exc, json.JSONDecodeError) else '编码错误'}",
            details=detail,
        )


def _parse_if_match(if_match: str | None) -> int:
    """解析 ``If-Match`` 条件版本头，如 ``W/\"2\"`` 或 ``"2"``。"""
    if not if_match:
        raise RejectionError(
            RejectReason.VERSION_CONFLICT,
            "更新必须携带 If-Match 期望版本头",
            field="If-Match",
        )
    token = if_match.strip()
    if token.startswith("W/"):
        token = token[2:].strip()
    token = token.strip('"')
    if not token.isdigit():
        raise RejectionError(
            RejectReason.VERSION_CONFLICT,
            "If-Match 必须为整数版本号，如 W/\"2\"",
            field="If-Match",
            details={"supplied": if_match},
        )
    return int(token)


@router.post("/{resource_type}")
async def create_resource(
    resource_type: str,
    request: Request,
    response: Response,
    version: str = Depends(get_fhir_version),
    principal: Principal = Depends(get_principal),
):
    payload = await _read_json(request)
    logger.info("create type=%s version=%s sample=%r", resource_type, version, payload)
    result = get_service().create_resource(
        payload, version, principal, resource_type=resource_type
    )
    response.status_code = 201
    response.headers["ETag"] = f'W/"{result["meta"]["version"]}"'
    return result


@router.put("/{resource_type}/{resource_id}")
async def update_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    response: Response,
    version: str = Depends(get_fhir_version),
    principal: Principal = Depends(get_principal),
):
    expected = _parse_if_match(request.headers.get("if-match"))
    payload = await _read_json(request)
    logger.info("update type=%s id=%s version=%s expected=%s sample=%r",
                resource_type, resource_id, version, expected, payload)
    result = get_service().update_resource(
        resource_id, payload, version, expected, principal,
        resource_type=resource_type,
    )
    response.headers["ETag"] = f'W/"{result["meta"]["version"]}"'
    return result


@router.get("/{resource_type}/{resource_id}")
async def read_resource(
    resource_type: str,
    resource_id: str,
    principal: Principal = Depends(get_principal),
):
    return get_service().read_resource(
        resource_id, principal, resource_type=resource_type
    )
