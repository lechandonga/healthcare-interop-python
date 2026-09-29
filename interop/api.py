"""FastAPI 接口层：统一把 InteropError 映射为可区分的 HTTP 响应。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import InteropError, RejectReason
from .service import InteropService
from .terminology import TerminologyService
from .models import Coding

app = FastAPI(title="healthcare-interop")
service = InteropService()

_STATUS = {
    RejectReason.MALFORMED_JSON: 400,
    RejectReason.UNSUPPORTED_STANDARD_VERSION: 400,
    RejectReason.UNSUPPORTED_RESOURCE_TYPE: 400,
    RejectReason.MISSING_REQUIRED_FIELD: 422,
    RejectReason.INVALID_FIELD_TYPE: 422,
    RejectReason.ILLEGAL_CODING: 422,
    RejectReason.UNKNOWN_CODE: 422,
    RejectReason.MAPPING_CONFLICT: 409,
    RejectReason.CODE_SYSTEM_VERSION_INCOMPATIBLE: 409,
    RejectReason.BROKEN_REFERENCE: 422,
    RejectReason.REFERENCE_VERSION_MISMATCH: 409,
    RejectReason.UNAUTHORIZED_REIDENTIFICATION: 403,
    RejectReason.INVALID_TOKEN: 401,
    RejectReason.VERSION_CONFLICT: 409,
    RejectReason.RESOURCE_NOT_FOUND: 404,
    RejectReason.DUPLICATE_WRITE: 409,
    RejectReason.FIELD_NOT_DEFINED_FOR_VERSION: 422,
    RejectReason.UNKNOWN_FIELD: 422,
    RejectReason.RECEIPT_NOT_FOUND: 404,
    RejectReason.RECEIPT_ALREADY_RESOLVED: 409,
    RejectReason.RECEIPT_RESOLVED_ELSEWHERE: 409,
    RejectReason.RECEIPT_TARGET_MISMATCH: 422,
}


@app.exception_handler(InteropError)
async def interop_error_handler(_: Request, exc: InteropError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS[exc.reason],
                        content={"error": exc.to_dict()})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/resources", status_code=201)
def ingest(body: dict, idempotency_key: str | None = None) -> dict:
    resource, version = service.ingest(body, idempotency_key=idempotency_key)
    return {"key": resource.reference_key(), "version": version}


@app.put("/resources/{resource_type}/{resource_id}")
def update(resource_type: str, resource_id: str, body: dict,
           expected_version: int, idempotency_key: str | None = None) -> dict:
    body = {**body, "resourceType": resource_type, "id": resource_id}
    resource, version = service.update(body, expected_version=expected_version,
                                       idempotency_key=idempotency_key)
    return {"key": resource.reference_key(), "version": version}


@app.post("/resources/batch")
def ingest_batch(bodies: list[dict]) -> dict:
    return service.ingest_batch(bodies)


@app.get("/receipts")
def list_receipts(status: str | None = None) -> dict:
    return service.list_receipts(status=status)


@app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict:
    return service.get_receipt(receipt_id)


@app.post("/receipts/{receipt_id}/replay", status_code=200)
def replay_receipt(receipt_id: str, body: dict,
                   idempotency_key: str | None = None) -> dict:
    return service.replay(receipt_id, body, idempotency_key=idempotency_key)


@app.get("/resources/{resource_type}/{resource_id}")
def get_resource(resource_type: str, resource_id: str, token: str | None = None) -> dict:
    return service.get(resource_type, resource_id, token=token)


@app.post("/tokens/{resource_type}/{resource_id}", status_code=201)
def issue_token(resource_type: str, resource_id: str, role: str) -> dict:
    return {"token": service.issue_token(resource_type, resource_id, role)}


@app.post("/terminology/validate")
def validate_code(coding: dict) -> dict:
    result = service.terminology.validate(
        Coding(system=coding["system"], code=coding["code"], display=coding.get("display")))
    return {"system": result.system, "code": result.code, "display": result.display}


@app.post("/terminology/map")
def map_code(coding: dict, target_system: str) -> dict:
    result = service.terminology.map_code(
        Coding(system=coding["system"], code=coding["code"]), target_system)
    return {"system": result.system, "code": result.code, "display": result.display}
