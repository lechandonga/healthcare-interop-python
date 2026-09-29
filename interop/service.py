"""应用服务层：编排 解析 -> 术语校验 -> 引用检查 -> 去标识化 -> 存储。

写入路径上任何一环失败都以 InteropError 显式拒绝：资源不会进入存储，
同时在拒绝回执库中留痕（receipt_id 附在错误上）。
读取路径默认返回去标识化视图；持令牌可还原。
"""

from __future__ import annotations

from .deidentify import DeidentificationService
from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource
from .parsing import parse_resource
from .receipts import RejectionStore
from .references import check_references
from .store import ResourceStore
from .terminology import TerminologyService


class InteropService:
    def __init__(self) -> None:
        self.terminology = TerminologyService()
        self.deidentification = DeidentificationService()
        self.store = ResourceStore()
        self.rejections = RejectionStore()

    # ---- 写入 ----
    def _prepare(self, raw: str | bytes | dict) -> NormalizedResource:
        """解析 + 术语校验 + 引用检查，返回待存储的归一化资源。"""
        resource = parse_resource(raw)
        code = resource.payload.get("code")
        if code:  # Observation / Condition 的临床代码必须过本地码表
            validated = self.terminology.validate(
                Coding(system=code["system"], code=code["code"], display=code.get("display")))
            resource.payload["code"] = {
                "system": validated.system, "code": validated.code, "display": validated.display}
        check_references(resource, self.store.resolve)
        return resource

    def _reject(self, raw, error: InteropError, batch_index: int | None) -> InteropError:
        """为任何被拒写入登记回执，并把 receipt_id 附在错误上（回执不含敏感原文）。"""
        receipt = self.rejections.record(batch_index=batch_index, body=raw, error=error)
        error.receipt_id = receipt.receipt_id
        return error

    def ingest(self, raw: str | bytes | dict, *, idempotency_key: str | None = None):
        """新建资源。返回 (去标识化资源, 版本号)。任何拒绝都登记回执。"""
        try:
            resource = self._prepare(raw)
            record = self.store.create(resource, idempotency_key=idempotency_key)
        except InteropError as e:
            raise self._reject(raw, e, None) from None
        return self.deidentification.deidentify(record.resource), record.version

    def update(self, raw: str | bytes | dict, *, expected_version: int,
               idempotency_key: str | None = None):
        """乐观锁更新。版本不匹配抛 VERSION_CONFLICT。任何拒绝都登记回执。"""
        try:
            resource = self._prepare(raw)
            record = self.store.update(resource, expected_version=expected_version,
                                       idempotency_key=idempotency_key)
        except InteropError as e:
            raise self._reject(raw, e, None) from None
        return self.deidentification.deidentify(record.resource), record.version

    def ingest_batch(self, raws: list[dict]) -> dict:
        """批量写入：逐条走完整校验链路并独立提交，单条失败不污染整批。

        每条失败都登记带 batch_index 的回执（receipt_id 出现在 failed 条目中）；
        被拒数据不入库、不污染同资源已有版本。
        """
        succeeded: list[dict] = []
        failed: list[dict] = []
        for i, raw in enumerate(raws):
            try:
                resource = self._prepare(raw)
                record = self.store.create(resource,
                                           idempotency_key=raw.get("idempotency_key"))
            except InteropError as e:
                self._reject(raw, e, i)
                failed.append({"index": i, "reason": e.reason.value, "detail": e.detail,
                               "field": e.field, "receipt_id": e.receipt_id})
                continue
            succeeded.append({"index": i, "key": record.resource.reference_key(),
                              "version": record.version})
        return {"succeeded": succeeded, "failed": failed}

    # ---- 拒绝回执 ----
    def get_receipt(self, receipt_id: str) -> dict:
        return self.rejections.get(receipt_id).to_dict()

    def list_receipts(self, status: str | None = None) -> list[dict]:
        return [r.to_dict() for r in self.rejections.list(status)]

    def replay_rejected(self, receipt_id: str, corrected_raw: dict,
                        *, idempotency_key: str | None = None) -> dict:
        """针对回执重放修正记录。

        - 与首次完全相同的校验链路（解析+术语+引用+存储），不放水；
        - 仍被拒：原回执保持 open，并为本次新拒绝另留一条回执；
        - 成功：原回执置 resolved 且只生效一次；resolved 再重放
          -> RECEIPT_ALREADY_RESOLVED；并发重放其余调用方
          -> RECEIPT_IN_PROGRESS。
        """
        receipt = self.rejections.claim(receipt_id)
        try:
            try:
                resource = self._prepare(corrected_raw)
                record = self.store.create(resource, idempotency_key=idempotency_key)
            except InteropError as e:
                self.rejections.release(receipt)
                raise self._reject(corrected_raw, e, None) from None
            stored_key = record.resource.reference_key()
            self.rejections.resolve(receipt, stored_key, record.version)
        finally:
            pass
        return {"outcome": "applied", "receipt_id": receipt.receipt_id,
                "receipt_status": "resolved", "key": stored_key,
                "version": record.version}

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
