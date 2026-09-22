"""批量写入接口（FHIR Bundle 风格）。

请求体::

    {
      "resourceType": "Bundle",
      "type": "batch",
      "entry": [{"resource": { ...单条 FHIR 资源... }}, ...]
    }

外层结构错误将整体拒绝；条目级错误按部分失败逐条返回。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request

from app.dependencies import get_fhir_version, get_principal, get_service
from app.errors import RejectReason, RejectionError
from app.security import Principal

router = APIRouter(prefix="/fhir", tags=["batch"])
logger = logging.getLogger("healthcare_interop.api")


@router.post("/")
async def write_batch(
    request: Request,
    version: str = Depends(get_fhir_version),
    principal: Principal = Depends(get_principal),
):
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RejectionError(
            RejectReason.MALFORMED_JSON,
            "Bundle 请求体不是合法 JSON",
            details={"position": getattr(exc, "pos", None)},
        )

    if not isinstance(body, dict):
        raise RejectionError(
            RejectReason.NOT_A_RESOURCE_OBJECT,
            "Bundle 顶层必须是 JSON 对象",
        )
    if body.get("resourceType") != "Bundle":
        raise RejectionError(
            RejectReason.MISSING_FIELD,
            "批量请求必须是 resourceType=Bundle 的文档",
            field="resourceType",
        )
    if body.get("type") != "batch":
        raise RejectionError(
            RejectReason.INCOMPATIBLE_FIELD,
            "仅支持 type=batch 的 Bundle",
            field="type",
            details={"supplied": body.get("type")},
        )
    entries = body.get("entry")
    if not isinstance(entries, list) or not entries:
        raise RejectionError(
            RejectReason.STRUCTURE_INCOMPLETE,
            "Bundle.entry 必须为非空数组",
            field="entry",
        )
    # 条目的 resource 解包；结构不对在该条记 BATCH_ENTRY_INVALID
    resources: list[dict] = []
    for i, e in enumerate(entries):
        if isinstance(e, dict) and isinstance(e.get("resource"), dict):
            resources.append(e["resource"])
        else:
            resources.append(e)  # 传入让批量服务归类 BATCH_ENTRY_INVALID

    logger.info("batch size=%s version=%s", len(resources), version)
    results = get_service().write_batch(resources, version, principal)
    ok_count = sum(1 for r in results if r.ok)
    return {
        "resourceType": "Bundle",
        "type": "batch-response",
        "total": len(results),
        "successCount": ok_count,
        "failureCount": len(results) - ok_count,
        "entry": [
            {
                "index": r.index,
                "ok": r.ok,
                "resource_id": r.resource_id,
                "version": r.version,
                "error": None
                if r.ok
                else {
                    "reason": r.reason,
                    "message": r.message,
                    "field": r.field_path,
                    "details": r.details,
                },
            }
            for r in results
        ],
    }
