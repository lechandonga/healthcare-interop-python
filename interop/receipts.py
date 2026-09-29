"""拒绝回执：每次被拒的写入都留下可复现、可查询、可修正重放的留痕。

保证：
- **可复现**：回执 ID 由输入指纹（canonical JSON 的 sha256）决定。同一份输入反复
  提交拿到同一条回执，结论（原因/字段/依据）一致，不会堆叠新回执。
- **只记录不留数据**：回执不保存原始提交内容，只保存指纹与资源坐标；被拒数据
  绝不进入主存储，也不污染已有版本。
- **不泄漏敏感值**：detail 中出现的 Patient 敏感原值（姓名/出生日期/联系方式）
  按去标识化口径替换为 REDACTED 占位符。
- **修正重放**：回执 open -> resolving -> resolved。重放走完整校验链路；
  resolved 后再次重放返回可区分结果（RECEIPT_ALREADY_RESOLVED），不会重复入库。
  并发重放同一条回执时，原子认领保证恰好一个生效，其余得到
  RECEIPT_RESOLVED_ELSEWHERE，无半更新、无重复写入。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from .deidentify import scrub_sensitive_text
from .errors import InteropError, RejectReason
from .parsing import SENSITIVE_PATIENT_FIELDS


def canonical_fingerprint(payload: object) -> str:
    """对输入做稳定序列化后取 sha256。dict 按 key 排序，保证与提交顺序无关。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def extract_sensitive_values(payload: object) -> list[str]:
    """尽力从原始输入中抽取 Patient 敏感字段原值，用于回执文本脱敏。
    畸形输入可能不是 dict，抽不到也不影响拒绝流程。"""
    values: list[str] = []
    if isinstance(payload, dict):
        for name in SENSITIVE_PATIENT_FIELDS:
            if name in payload and payload[name] is not None:
                values.append(_canonical(payload[name]))
        # birthDate/telecom 等也可能以裸字符串出现，统一交给 scrubber 兜底
        for item in payload.values():
            if isinstance(item, (dict, list)):
                values.extend(extract_sensitive_values(item))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(extract_sensitive_values(item))
    return values


@dataclass
class Receipt:
    receipt_id: str
    input_fingerprint: str
    resource_type: str | None
    resource_id: str | None
    batch_index: int | None
    reason: str
    detail: str
    field: str | None
    basis: str
    status: str = "open"  # open | resolving | resolved
    resolved_key: str | None = None
    resolved_version: int | None = None

    def to_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "status": self.status,
            "resource": (f"{self.resource_type}/{self.resource_id}"
                         if self.resource_type and self.resource_id else None),
            "batch_index": self.batch_index,
            "reason": self.reason,
            "detail": self.detail,
            "field": self.field,
            "basis": self.basis,
            "resolved_key": self.resolved_key,
            "resolved_version": self.resolved_version,
        }


class ReceiptStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: dict[str, Receipt] = {}

    @staticmethod
    def make_id(payload: object, batch_index: int | None = None) -> str:
        suffix = f"#{batch_index}" if batch_index is not None else ""
        return "rcpt_" + canonical_fingerprint(payload) + suffix

    def record(self, *, payload: object, error: InteropError,
               resource_type: str | None, resource_id: str | None,
               batch_index: int | None = None) -> Receipt:
        """登记（或取回）一条拒绝回执。相同输入幂等返回同一条、结论一致。"""
        receipt_id = self.make_id(payload, batch_index)
        secret_values = extract_sensitive_values(payload)
        detail = scrub_sensitive_text(error.detail, secret_values)
        with self._lock:
            existing = self._receipts.get(receipt_id)
            if existing is not None:
                error.receipt_id = existing.receipt_id
                return existing
            receipt = Receipt(
                receipt_id=receipt_id,
                input_fingerprint=canonical_fingerprint(payload),
                resource_type=resource_type, resource_id=resource_id,
                batch_index=batch_index,
                reason=error.reason.value, detail=detail, field=error.field,
                basis=error.basis or f"服务规则: {error.reason.value}",
            )
            self._receipts[receipt_id] = receipt
            error.receipt_id = receipt_id
            return receipt

    def get(self, receipt_id: str) -> Receipt:
        with self._lock:
            receipt = self._receipts.get(receipt_id)
            if receipt is None:
                raise InteropError(RejectReason.RECEIPT_NOT_FOUND,
                                   f"回执 {receipt_id!r} 不存在", field="receipt_id")
            return receipt

    def list_open(self, *, status: str | None = None) -> list[Receipt]:
        with self._lock:
            receipts = list(self._receipts.values())
        if status is not None:
            receipts = [r for r in receipts if r.status == status]
        return sorted(receipts, key=lambda r: r.receipt_id)

    def claim(self, receipt_id: str, *, resource_type: str, resource_id: str) -> Receipt:
        """原子认领一条回执用于修正重放。
        - resolved：拒绝重复生效（RECEIPT_ALREADY_RESOLVED）；
        - 已被其它并发调用方认领/解决：RECEIPT_RESOLVED_ELSEWHERE；
        - 资源坐标与回执不符：RECEIPT_TARGET_MISMATCH。
        """
        with self._lock:
            receipt = self.get(receipt_id)
            if receipt.status == "resolved":
                raise InteropError(
                    RejectReason.RECEIPT_ALREADY_RESOLVED,
                    f"回执 {receipt_id} 已由 {receipt.resolved_key} v{receipt.resolved_version} "
                    f"解决，重放不得重复入库",
                    field="receipt_id", receipt_id=receipt_id)
            if receipt.status == "resolving":
                raise InteropError(
                    RejectReason.RECEIPT_RESOLVED_ELSEWHERE,
                    f"回执 {receipt_id} 正被其它调用方修正重放，本次不生效",
                    field="receipt_id", receipt_id=receipt_id)
            if (receipt.resource_type is not None and receipt.resource_id is not None
                    and (receipt.resource_type, receipt.resource_id)
                    != (resource_type, resource_id)):
                raise InteropError(
                    RejectReason.RECEIPT_TARGET_MISMATCH,
                    f"重放记录 {resource_type}/{resource_id} 与回执指向的 "
                    f"{receipt.resource_type}/{receipt.resource_id} 不一致",
                    field="resourceType")
            receipt.status = "resolving"
            return receipt

    def resolve(self, receipt_id: str, *, key: str, version: int) -> Receipt:
        with self._lock:
            receipt = self.get(receipt_id)
            receipt.status = "resolved"
            receipt.resolved_key = key
            receipt.resolved_version = version
            return receipt

    def release(self, receipt_id: str) -> None:
        """重放链路再次失败：回执回到 open，供接入方继续修正。"""
        with self._lock:
            receipt = self._receipts.get(receipt_id)
            if receipt is not None and receipt.status == "resolving":
                receipt.status = "open"
