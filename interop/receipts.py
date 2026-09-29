"""拒绝回执：每次被拒写入都留痕、可查、可复现、可修正重放。

保证：
- 回执内容由"去标识化后的输入快照 + 拒绝结论"确定性推导：同一输入反复提交拿到
  同一条、结论一致的回执，不会每次堆一条新的。
- 回执只记录，不进主数据；快照中的敏感字段（name/birthDate/telecom 等）按去标识化
  口径替换为 REDACTED:<hash>，不含原文。
- 修正重放走与首次完全相同的校验链路；成功后回执 open -> resolved（只生效一次），
  resolved 再重放得到可区分结果 RECEIPT_ALREADY_RESOLVED。
- 并发重放同一回执：锁内 compare-and-set 状态，恰好一个调用方真正生效，其余得到
  RECEIPT_IN_PROGRESS，无半更新、重复写入或版本错乱。
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from .errors import InteropError, RejectReason
from .parsing import SENSITIVE_PATIENT_FIELDS


class ReceiptStatus(enum.Enum):
    OPEN = "open"          # 待处理，可按回执修正后重放
    RESOLVED = "resolved"  # 已解决：修正记录已成功入库


# 每条拒绝原因对应的判定依据（写进回执，接入方可据此修改）
RULE_BASIS: dict[RejectReason, str] = {
    RejectReason.MALFORMED_JSON: "输入必须是合法 JSON 对象",
    RejectReason.UNSUPPORTED_STANDARD_VERSION:
        "standard_version 仅支持 fhir-stu3/fhir-r4/fhir-r5",
    RejectReason.UNSUPPORTED_RESOURCE_TYPE:
        "resourceType 仅支持 Patient/Observation/Condition",
    RejectReason.MISSING_REQUIRED_FIELD: "必填字段缺失，必须显式提供，不接受默认值",
    RejectReason.INVALID_FIELD_TYPE:
        "字段类型或取值不符合该版本规范（含状态取值集）",
    RejectReason.ILLEGAL_CODING:
        "编码结构非法：coding 必须为非空数组且含非空 system/code",
    RejectReason.VERSION_FIELD_MISMATCH:
        "该字段属于其它标准版本，声明版本中不存在，请按声明版本送数",
    RejectReason.UNKNOWN_FIELD:
        "该字段在任何已定义版本模型中都不存在，请核对字段名",
    RejectReason.UNKNOWN_CODE: "代码不在本地码表或无映射",
    RejectReason.MAPPING_CONFLICT:
        "源代码映射到多个不同目标代码，需人工裁决，不静默取其一",
    RejectReason.CODE_SYSTEM_VERSION_INCOMPATIBLE:
        "代码系统 system|version 与本地码表版本不一致",
    RejectReason.BROKEN_REFERENCE: "引用目标资源不存在，且被拒数据不得入库",
    RejectReason.REFERENCE_VERSION_MISMATCH:
        "低版本资源不得引用高版本资源（版本序 STU3<R4<R5）",
    RejectReason.VERSION_CONFLICT:
        "乐观锁冲突：expected_version 与当前版本不一致",
    RejectReason.RESOURCE_NOT_FOUND: "目标资源不存在",
    RejectReason.DUPLICATE_WRITE: "资源已存在且幂等键未匹配，拒绝覆盖式创建",
}

# 快照中需要去标识化的敏感字段（与 parsing 的敏感字段口径一致）
_SENSITIVE_KEYS = set(SENSITIVE_PATIENT_FIELDS)


def _redact(value: object) -> str:
    digest = hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:12]
    return f"REDACTED:{digest}"


def sanitize(value: Any) -> Any:
    """对输入快照做去标识化：敏感字段值替换为哈希占位符，键名与结构保留以便定位。"""
    if isinstance(value, dict):
        clean = {}
        for k, v in value.items():
            clean[k] = _redact(v) if k in _SENSITIVE_KEYS else sanitize(v)
        return clean
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class RejectionReceipt:
    receipt_id: str
    batch_index: int | None                 # 批量提交中的下标（None 表示非批量单条）
    resource_type: str | None               # 尽量解析；解析失败时为 None
    resource_id: str | None
    standard_version: str | None
    reason: str
    field: str | None
    detail: str
    basis: str                              # 判定依据（规则说明）
    input_sample: dict                      # 去标识化后的输入快照
    fingerprint: str                        # 输入快照+结论的确定性指纹
    status: str = ReceiptStatus.OPEN.value
    resolved_key: str | None = None         # 解决后入库的资源键
    resolved_version: int | None = None
    claimed: bool = False                   # 重放占用标记（仅 open 态有意义）

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class RejectionStore:
    """回执存储 + 重放状态机。全部状态变更在单把锁内原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: dict[str, RejectionReceipt] = {}

    def record(self, *, batch_index: int | None, body: Any,
               error: InteropError) -> RejectionReceipt:
        """登记一次拒绝。同一（输入快照, 结论, 批量下标）反复提交返回同一条回执。"""
        sample = sanitize(body) if isinstance(body, dict) else {"raw": _redact(repr(body))}
        reason = error.reason.value
        field_name = error.field
        basis = RULE_BASIS.get(error.reason, "按对应版本 FHIR 规范校验")
        fingerprint = hashlib.sha256(_canonical(
            {"index": -1 if batch_index is None else batch_index, "input": sample,
             "reason": reason, "field": field_name}).encode("utf-8")).hexdigest()
        receipt_id = "rr-" + fingerprint[:16]
        with self._lock:
            existing = self._receipts.get(receipt_id)
            if existing is not None:
                return existing  # 可复现：不新增、结论不漂移
            receipt = RejectionReceipt(
                receipt_id=receipt_id, batch_index=batch_index,
                resource_type=sample.get("resourceType"), resource_id=sample.get("id"),
                standard_version=sample.get("standard_version"), reason=reason,
                field=field_name, detail=error.detail, basis=basis,
                input_sample=sample, fingerprint=fingerprint)
            self._receipts[receipt_id] = receipt
            return receipt

    def get(self, receipt_id: str) -> RejectionReceipt:
        with self._lock:
            receipt = self._receipts.get(receipt_id)
            if receipt is None:
                raise InteropError(RejectReason.RECEIPT_NOT_FOUND,
                                   f"回执 {receipt_id!r} 不存在", field="receipt_id")
            return receipt

    def list(self, status: str | None = None) -> list[RejectionReceipt]:
        with self._lock:
            receipts = list(self._receipts.values())
        if status is not None:
            receipts = [r for r in receipts if r.status == status]
        return sorted(receipts, key=lambda r: r.receipt_id)

    def claim(self, receipt_id: str) -> RejectionReceipt:
        """领取回执进行重放。resolved / 已被他人领取都以可区分原因拒绝。"""
        with self._lock:
            receipt = self.get(receipt_id)
            if receipt.status == ReceiptStatus.RESOLVED.value:
                raise InteropError(
                    RejectReason.RECEIPT_ALREADY_RESOLVED,
                    f"回执 {receipt_id} 已解决（{receipt.resolved_key} "
                    f"v{receipt.resolved_version}），重放不得重复入库",
                    field="receipt_id")
            if receipt.claimed:
                raise InteropError(
                    RejectReason.RECEIPT_IN_PROGRESS,
                    f"回执 {receipt_id} 正被另一个调用方重放，本次不生效",
                    field="receipt_id")
            receipt.claimed = True
            return receipt

    def resolve(self, receipt: RejectionReceipt, stored_key: str, version: int) -> None:
        with self._lock:
            receipt.status = ReceiptStatus.RESOLVED.value
            receipt.resolved_key = stored_key
            receipt.resolved_version = version
            receipt.claimed = False

    def release(self, receipt: RejectionReceipt) -> None:
        with self._lock:
            receipt.claimed = False
