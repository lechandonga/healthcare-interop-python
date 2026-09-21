"""应用服务层：编排 解析 -> 术语校验 -> 引用检查 -> 去标识化 -> 存储。

写入路径上任何一环失败都以 InteropError 显式拒绝，资源不会进入存储。
读取路径默认返回去标识化视图；持令牌可还原。
"""

from __future__ import annotations

from .deidentify import DeidentificationService
from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource
from .parsing import parse_resource
from .references import check_references
from .store import ResourceStore
from .terminology import TerminologyService


class InteropService:
    def __init__(self) -> None:
        self.terminology = TerminologyService()
        self.deidentification = DeidentificationService()
        self.store = ResourceStore()

    # ---- 写入 ----
    def _prepare(self, raw: str | bytes | dict) -> NormalizedResource:
        """解析 + 术语校验 + 引用检查，返回待存储的归一化资源。"""
        resource = parse_resource(raw)
        code = resource.payload.get("code")
        if code:  # Observation 的临床代码必须过本地码表
            validated = self.terminology.validate(
                Coding(system=code["system"], code=code["code"], display=code.get("display")))
            resource.payload["code"] = {
                "system": validated.system, "code": validated.code, "display": validated.display}
        check_references(resource, self.store.resolve)
        return resource

    def ingest(self, raw: str | bytes | dict, *, idempotency_key: str | None = None):
        """新建资源。返回 (去标识化资源, 版本号)。"""
        resource = self._prepare(raw)
        record = self.store.create(resource, idempotency_key=idempotency_key)
        return self.deidentification.deidentify(record.resource), record.version

    def update(self, raw: str | bytes | dict, *, expected_version: int,
               idempotency_key: str | None = None):
        """乐观锁更新。版本不匹配抛 VERSION_CONFLICT。"""
        resource = self._prepare(raw)
        record = self.store.update(resource, expected_version=expected_version,
                                   idempotency_key=idempotency_key)
        return self.deidentification.deidentify(record.resource), record.version

    def ingest_batch(self, raws: list[dict]) -> dict:
        """批量写入：逐条 解析+校验+提交，单条失败不污染整批。"""
        ops: list[dict] = []
        pre_failed: list[dict] = []
        for i, raw in enumerate(raws):
            try:
                resource = self._prepare(raw)
                ops.append({"action": "create", "resource": resource,
                            "idempotency_key": raw.get("idempotency_key"),
                            "_index": i})
            except InteropError as e:
                pre_failed.append({"index": i, "reason": e.reason.value,
                                   "detail": e.detail, "field": e.field})
        result = self.store.write_batch(
            [{k: v for k, v in op.items() if k != "_index"} for op in ops])
        # 还原原始下标，保证失败可复现定位
        for entry, op in zip(result["succeeded"], ops):
            entry["index"] = op["_index"]
        return {"succeeded": result["succeeded"],
                "failed": sorted(pre_failed + result["failed"], key=lambda e: e["index"])}

    # ---- 读取 ----
    def get(self, resource_type: str, resource_id: str, *, token: str | None = None):
        """默认返回去标识化视图；持有效令牌返回还原视图。两种结果可区分。"""
        record = self.store.get(resource_type, resource_id)
        resource = self.deidentification.deidentify(record.resource)
        if token is None:
            return {"view": "deidentified", "version": record.version,
                    "resource": self._to_view(resource)}
        restored = self.deidentification.reidentify(resource, token)
        return {"view": "reidentified", "version": record.version,
                "resource": self._to_view(restored)}

    def issue_token(self, resource_type: str, resource_id: str, role: str) -> str:
        self.store.get(resource_type, resource_id)  # 资源必须存在
        return self.deidentification.issue_token(f"{resource_type}/{resource_id}", role)

    @staticmethod
    def _to_view(resource: NormalizedResource) -> dict:
        return {
            "resourceType": resource.resource_type.value,
            "id": resource.resource_id,
            "standard_version": resource.standard_version.value,
            "payload": resource.payload,
            "references": resource.references,
            "sensitive_fields": resource.sensitive_fields,
        }
