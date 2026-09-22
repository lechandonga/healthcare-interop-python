"""批量写入的部分失败处理。

策略（best-effort，单条隔离）
---------------------------
* 逐条目独立解析/校验/引用检查/写入，单条失败不回滚其他成功条目，
  也不污染已有状态。
* 每条结果带稳定错误码、字段路径与可复现判定依据，顺序与输入一致。
* 单条目的写入仍受存储乐观并发约束，天然避免重复写入。
"""
from __future__ import annotations

import logging
from typing import Any

from app.errors import RejectReason, RejectionError
from app.models import BatchItemResult
from app.parser import ResourceParser
from app.references import check_references
from app.security import Principal, require
from app.storage import ResourceStore

logger = logging.getLogger("healthcare_interop.batch")


class BatchService:
    def __init__(self, store: ResourceStore, parser: ResourceParser) -> None:
        self._store = store
        self._parser = parser

    def write_batch(
        self,
        entries: list[dict[str, Any]],
        fhir_version: str,
        principal: Principal,
    ) -> list[BatchItemResult]:
        require(principal, "resource:write")
        results: list[BatchItemResult] = []
        for index, entry in enumerate(entries):
            logger.info(
                "batch entry index=%s fhir_version=%s sample=%r",
                index, fhir_version, _safe_sample(entry),
            )
            result = self._write_one(index, entry, fhir_version, principal)
            logger.info(
                "batch verdict index=%s ok=%s reason=%s field=%s",
                index, result.ok, result.reason, result.field_path,
            )
            results.append(result)
        return results

    def _write_one(
        self,
        index: int,
        entry: Any,
        fhir_version: str,
        principal: Principal,
    ) -> BatchItemResult:
        if not isinstance(entry, dict):
            return BatchItemResult(
                index=index, ok=False,
                reason=RejectReason.BATCH_ENTRY_INVALID.value,
                message="批量条目必须为 JSON 对象",
                details={"actual_type": type(entry).__name__},
            )

        # 条目的操作模式：默认 create；可给 expectedVersion 做条件更新
        expected_version = entry.get("expectedVersion")
        if expected_version is not None and not isinstance(expected_version, int):
            return BatchItemResult(
                index=index, ok=False,
                reason=RejectReason.BATCH_ENTRY_INVALID.value,
                message="expectedVersion 必须为整数",
                field_path="expectedVersion",
            )

        try:
            resource = self._parser.parse(entry, fhir_version)
            check_references(resource, self._store)
            if expected_version is None:
                stored = self._store.put_new(resource)
            else:
                stored = self._store.update(resource, expected_version)
            return BatchItemResult(
                index=index, ok=True,
                resource_id=resource.resource_id,
                version=stored.version,
            )
        except RejectionError as exc:
            return BatchItemResult(
                index=index, ok=False,
                resource_id=entry.get("id") if isinstance(entry, dict) else None,
                reason=exc.reason.value,
                message=exc.message,
                field_path=exc.field,
                details=exc.details,
            )


def _safe_sample(entry: Any) -> Any:
    """生成日志样本：截断超长内容，避免日志膨胀（不做脱敏语义保证，
    敏感字段的访问控制在 API 层；测试主要观察判定依据）。"""
    text = repr(entry)
    return text if len(text) <= 500 else text[:500] + "...<truncated>"
