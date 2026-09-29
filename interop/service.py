"""应用服务层：编排 解析 -> 术语校验 -> 引用检查 -> 去标识化 -> 存储。

写入路径上任何一环失败都以 InteropError 显式拒绝，资源不会进入存储。
读取路径默认返回去标识化视图；持令牌可还原。
"""

from __future__ import annotations

from .deidentify import DeidentificationService
from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource
from .parsing import parse_resource
from .receipts import ReceiptStore
from .references import check_references
from .store import ResourceStore
from .terminology import TerminologyService


class InteropService:
    def __init__(self) -> None:
        self.terminology = TerminologyService()
        self.deidentification = DeidentificationService()
        self.store = ResourceStore()
        self.receipts = ReceiptStore()

    # ---- 写入 ----
    @staticmethod
    def _coordinates(raw: object) -> tuple[str | None, str | None]:
        if isinstance(raw, dict):
            rtype = raw.get("resourceType")
            rid = raw.get("id")
            if isinstance(rtype, str) and isinstance(rid, str):
                return rtype, rid
        return None, None

    def _reject(self, raw: object, error: InteropError, *,
                batch_index: int | None = None) -> InteropError:
        """登记拒绝回执并把 receipt_id 挂到错误上；回执不改变拒绝结论。"""
        rtype, rid = self._coordinates(raw)
        self.receipts.record(payload=raw, error=error,
                             resource_type=rtype, resource_id=rid,
                             batch_index=batch_index)
        return error

    def _prepare(self, raw: str | bytes | dict) -> NormalizedResource:
        """解析 + 术语校验 + 引用检查，返回待存储的归一化资源。"""
        resource = parse_resource(raw)
        # Observation / Condition 的临床代码必须过本地码表
        if resource.resource_type.value in ("Observation", "Condition"):
            code = resource.payload.get("code")
        else:
            code = None
        if code:
            validated = self.terminology.validate(
                Coding(system=code["system"], code=code["code"], display=code.get("display")))
            resource.payload["code"] = {
                "system": validated.system, "code": validated.code, "display": validated.display}
        check_references(resource, self.store.resolve)
        return resource

    def ingest(self, raw: str | bytes | dict, *, idempotency_key: str | None = None):
        """新建资源。返回 (去标识化资源, 版本号)。"""
        try:
            resource = self._prepare(raw)
            record = self.store.create(resource, idempotency_key=idempotency_key)
        except InteropError as e:
            raise self._reject(raw, e) from e
        return self.deidentification.deidentify(record.resource), record.version

    def update(self, raw: str | bytes | dict, *, expected_version: int,
               idempotency_key: str | None = None):
        """乐观锁更新。版本不匹配抛 VERSION_CONFLICT。"""
        try:
            resource = self._prepare(raw)
            record = self.store.update(resource, expected_version=expected_version,
                                       idempotency_key=idempotency_key)
        except InteropError as e:
            raise self._reject(raw, e) from e
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
                self._reject(raw, e, batch_index=i)
                pre_failed.append({"index": i, "reason": e.reason.value,
                                   "detail": e.detail, "field": e.field,
                                   "receipt_id": e.receipt_id})
        result = self.store.write_batch(
            [{k: v for k, v in op.items() if k != "_index"} for op in ops])
        # 还原原始下标，保证失败可复现定位
        for entry, op in zip(result["succeeded"], ops):
            entry["index"] = op["_index"]
        # 存储阶段失败（duplicate/version_conflict）同样逐条留回执
        for failed in result["failed"]:
            idx = failed["index"]
            original_index = ops[idx]["_index"] if idx < len(ops) else idx
            raw = raws[original_index]
            err = InteropError(getattr(RejectReason, failed["reason"].upper()),
                               failed["detail"], field=failed["field"])
            self._reject(raw, err, batch_index=original_index)
            failed["index"] = original_index
            failed["receipt_id"] = err.receipt_id
        return {"succeeded": result["succeeded"],
                "failed": sorted(pre_failed + result["failed"], key=lambda e: e["index"])}

    # ---- 拒绝回执 ----
    def get_receipt(self, receipt_id: str) -> dict:
        return self.receipts.get(receipt_id).to_dict()

    def list_receipts(self, status: str | None = None) -> dict:
        return {"receipts": [r.to_dict() for r in self.receipts.list_open(status=status)]}

    def replay(self, receipt_id: str, raw: dict, *,
               idempotency_key: str | None = None) -> dict:
        """针对回执的修正重放：
        - 走与首次完全相同的 解析+术语+引用+入库 链路，不放水；
        - 成功后回执置为 resolved 并随结果返回；
        - 已 resolved 的回执再重放 -> RECEIPT_ALREADY_RESOLVED，不重复入库；
        - 并发重放由回执原子认领保证恰好一个生效。
        """
        rtype, rid = self._coordinates(raw)
        if rtype is None or rid is None:
            raise InteropError(RejectReason.MISSING_REQUIRED_FIELD,
                               "重放记录必须携带 resourceType 与 id",
                               field="resourceType")
        self.receipts.claim(receipt_id, resource_type=rtype, resource_id=rid)
        try:
            resource = self._prepare(raw)
            record = self.store.create(resource, idempotency_key=idempotency_key)
        except InteropError as e:
            # 修正仍不合格：登记新的拒绝回执（针对本次输入），原回执保持可继续修正
            self.receipts.release(receipt_id)
            raise self._reject(raw, e) from e
        resolved = self.receipts.resolve(receipt_id,
                                         key=record.resource.reference_key(),
                                         version=record.version)
        return {
            "outcome": "resolved",
            "key": record.resource.reference_key(),
            "version": record.version,
            "receipt": resolved.to_dict(),
        }

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
