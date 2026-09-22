"""应用服务层：编排解析、术语、引用、存储、权限与脱敏。

读权限模型（返回结果严格可区分）
-----------------------------
* 持有 ``reidentify:read`` -> 返回原始字段 + ``"view": "identified"``
* 否则持有 ``patient:read`` 或 ``deid:read``
  -> 返回脱敏视图 + ``"view": "de-identified"``
* 两者皆无 -> ``FORBIDDEN``（不接触/不返回任何原始字段）
"""
from __future__ import annotations

from typing import Any

from app.batch import BatchService
from app.deid import redact_resource
from app.models import BatchItemResult, StoredResource
from app.parser import ResourceParser
from app.references import check_references
from app.security import Principal, audit, require
from app.storage import ResourceStore
from app.terminology import TerminologyService


class InteropService:
    """门面服务：组装所有子系统。"""

    def __init__(self, store: ResourceStore | None = None) -> None:
        self.terminology = TerminologyService()
        self.terminology.load_defaults()
        self.store = store or ResourceStore()
        self.parser = ResourceParser(self.terminology)
        self.batch = BatchService(self.store, self.parser)

    # ------------------------------------------------------------------
    def _serialize(self, stored: StoredResource, principal: Principal) -> dict[str, Any]:
        r = stored.resource
        base: dict[str, Any] = {
            "resourceType": r.kind.value,
            "id": r.resource_id,
            "meta": {
                "version": stored.version,
                "sourceVersion": r.source_version,
                "createdAt": stored.created_at,
                "updatedAt": stored.updated_at,
            },
            "provenance": r.field_provenance,
        }
        if principal.has("reidentify:read"):
            base["view"] = "identified"
            base["data"] = r.data
            return base
        # 未授权还原：返回脱敏视图，绝不返回原始敏感字段
        base["view"] = "de-identified"
        base["data"] = redact_resource(r)
        return base

    def create_resource(
        self, payload: Any, fhir_version: str, principal: Principal,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        require(principal, "resource:write")
        resource = self.parser.parse(payload, fhir_version)
        if resource_type and resource.kind.value != resource_type:
            from app.errors import RejectReason, RejectionError
            raise RejectionError(
                RejectReason.INCOMPATIBLE_FIELD,
                f"URL 资源类型 {resource_type} 与 body resourceType "
                f"{resource.kind.value} 不一致",
                field="resourceType",
            )
        check_references(resource, self.store)
        stored = self.store.put_new(resource)
        audit(principal, "resource.create",
              resource_id=resource.resource_id, version=stored.version)
        return self._serialize(stored, principal)

    def update_resource(
        self,
        resource_id: str,
        payload: Any,
        fhir_version: str,
        expected_version: int,
        principal: Principal,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        require(principal, "resource:write")
        resource = self.parser.parse(payload, fhir_version, expected_id=resource_id)
        if resource_type and resource.kind.value != resource_type:
            from app.errors import RejectReason, RejectionError
            raise RejectionError(
                RejectReason.INCOMPATIBLE_FIELD,
                f"URL 资源类型 {resource_type} 与 body resourceType 不一致",
                field="resourceType",
            )
        check_references(resource, self.store)
        stored = self.store.update(resource, expected_version)
        audit(principal, "resource.update",
              resource_id=resource_id, version=stored.version,
              expected_version=expected_version)
        return self._serialize(stored, principal)

    def read_resource(
        self,
        resource_id: str,
        principal: Principal,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        stored = self.store.get(resource_id)
        if resource_type and stored.resource.kind.value != resource_type:
            from app.errors import RejectReason, RejectionError
            raise RejectionError(
                RejectReason.NOT_FOUND,
                f"{resource_type}/{resource_id} 不存在（该 ID 属于其他类型）",
            )
        if not (principal.has("reidentify:read")
                or principal.has("patient:read")
                or principal.has("deid:read")):
            from app.errors import RejectReason, RejectionError
            raise RejectionError(
                RejectReason.FORBIDDEN,
                "主体没有任何读取权限",
            )
        audit(principal, "resource.read",
              resource_id=resource_id,
              view="identified" if principal.has("reidentify:read")
              else "de-identified")
        return self._serialize(stored, principal)

    def write_batch(
        self,
        entries: list[dict[str, Any]],
        fhir_version: str,
        principal: Principal,
    ) -> list[BatchItemResult]:
        return self.batch.write_batch(entries, fhir_version, principal)
