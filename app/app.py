"""FastAPI 应用工厂与统一错误响应。"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import RejectReason, RejectionError
from app.routers import batch as batch_router
from app.routers import resources as resources_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="本地医疗互操作数据接入与交换服务",
        version="0.1.0",
    )

    @app.exception_handler(RejectionError)
    async def _on_rejection(_: Request, exc: RejectionError) -> JSONResponse:
        # 409：版本冲突；404：不存在；401/403：权限；其余 422
        status = {
            RejectReason.MALFORMED_JSON: 400,
            RejectReason.VERSION_CONFLICT: 409,
            RejectReason.ALREADY_EXISTS: 409,
            RejectReason.NOT_FOUND: 404,
            RejectReason.REFERENCE_TARGET_NOT_FOUND: 422,
            RejectReason.UNAUTHORIZED: 401,
            RejectReason.FORBIDDEN: 403,
        }.get(exc.reason, 422)
        return JSONResponse(status_code=status, content={"error": exc.to_dict()})

    @app.exception_handler(json.JSONDecodeError)
    async def _on_json(_: Request, exc: json.JSONDecodeError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "reason": RejectReason.MALFORMED_JSON.value,
                    "message": "请求体不是合法 JSON",
                    "details": {"position": exc.pos},
                }
            },
        )

    @app.exception_handler(NotImplementedError)
    async def _on_nyi(_: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse(status_code=501, content={"error": {"message": "not implemented"}})

    app.include_router(resources_router.router)
    app.include_router(batch_router.router)

    @app.get("/")
    async def root() -> dict:
        return {"service": "healthcare-interop", "status": "ok"}

    return app


app = create_app()
