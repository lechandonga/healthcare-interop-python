"""本地存储：乐观并发控制、幂等写入、批量部分失败。

一致性保证：
- 所有写操作在单把可重入锁内完成"读版本-校验-写入"原子序列，
  并发修改同一资源时必有一方看到 VERSION_CONFLICT，不会半更新或互相覆盖。
- 每个资源记录已应用的幂等键：重复提交同一幂等键直接返回原结果，
  不产生重复写入。
- 批量写入逐条独立提交：单条失败只记入 failed，不回滚、不污染其它记录，
  部分成功结果可查询。
"""

from __future__ import annotations

import threading

from .errors import InteropError, RejectReason
from .models import NormalizedResource, StoredRecord


class ResourceStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, StoredRecord] = {}

    @staticmethod
    def _key(resource_type: str, resource_id: str) -> str:
        return f"{resource_type}/{resource_id}"

    # ---- 单条读写 ----
    def create(self, resource: NormalizedResource, *, idempotency_key: str | None = None) -> StoredRecord:
        key = resource.reference_key()
        with self._lock:
            existing = self._data.get(key)
            if existing is not None:
                if idempotency_key and idempotency_key in existing.idempotency_keys:
                    return existing  # 幂等重放：不重复写入
                raise InteropError(RejectReason.DUPLICATE_WRITE,
                                   f"资源 {key} 已存在且幂等键未匹配，拒绝覆盖式创建")
            keys = {idempotency_key} if idempotency_key else set()
            record = StoredRecord(resource=resource, version=1, idempotency_keys=keys)
            self._data[key] = record
            return record

    def update(self, resource: NormalizedResource, *, expected_version: int,
               idempotency_key: str | None = None) -> StoredRecord:
        key = resource.reference_key()
        with self._lock:
            current = self._data.get(key)
            if current is None:
                raise InteropError(RejectReason.RESOURCE_NOT_FOUND, f"资源 {key} 不存在，无法更新")
            if idempotency_key and idempotency_key in current.idempotency_keys:
                return current  # 该幂等键已应用过：直接返回，不重复写入
            if current.version != expected_version:
                raise InteropError(
                    RejectReason.VERSION_CONFLICT,
                    f"资源 {key} 版本冲突: 期望 {expected_version}, 实际 {current.version}，"
                    f"请重新读取后重试")
            keys = set(current.idempotency_keys)
            if idempotency_key:
                keys.add(idempotency_key)
            record = StoredRecord(resource=resource, version=current.version + 1,
                                  idempotency_keys=keys)
            self._data[key] = record
            return record

    def get(self, resource_type: str, resource_id: str) -> StoredRecord:
        key = self._key(resource_type, resource_id)
        with self._lock:
            record = self._data.get(key)
            if record is None:
                raise InteropError(RejectReason.RESOURCE_NOT_FOUND, f"资源 {key} 不存在")
            return record

    def resolve(self, reference_key: str) -> NormalizedResource | None:
        """供引用检查使用：不存在返回 None 而非抛错。"""
        with self._lock:
            record = self._data.get(reference_key)
            return record.resource if record else None

    # ---- 批量写入（部分失败） ----
    def write_batch(self, ops: list[dict]) -> dict:
        """逐条独立提交。返回 {"succeeded": [...], "failed": [...]}，失败原因可区分。"""
        succeeded: list[dict] = []
        failed: list[dict] = []
        for index, op in enumerate(ops):
            try:
                action = op.get("action")
                resource: NormalizedResource = op["resource"]
                idem = op.get("idempotency_key")
                if action == "create":
                    record = self.create(resource, idempotency_key=idem)
                elif action == "update":
                    record = self.update(resource,
                                         expected_version=op["expected_version"],
                                         idempotency_key=idem)
                else:
                    raise InteropError(RejectReason.INVALID_FIELD_TYPE,
                                       f"未知批量操作: {action!r}", field="action")
                succeeded.append({"index": index, "key": record.resource.reference_key(),
                                  "version": record.version})
            except InteropError as e:
                failed.append({"index": index, "reason": e.reason.value,
                               "detail": e.detail, "field": e.field})
        return {"succeeded": succeeded, "failed": failed}
