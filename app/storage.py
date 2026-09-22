"""本地存储：线程安全的资源持久化与并发写入控制。

并发模型（内存模拟 + RLock + 乐观版本号）
--------------------------------------
* 每个资源带单调递增的整数 ``version``；更新必须携带期望版本
  （``If-Match: W/"<n>"`` 语义），不匹配 -> ``VERSION_CONFLICT``。
* 创建使用客户端提供的稳定 ID；ID 已存在 -> ``ALREADY_EXISTS``，
  从而重复请求可判重、不会产生双份数据。
* 所有变更在锁内一次性替换整个不可变快照（:class:`StoredResource`
  为 frozen dataclass），杜绝半更新；读路径返回快照对象本身。
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.errors import RejectReason, RejectionError
from app.models import NormalizedResource, StoredResource


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ResourceStore:
    """线程安全的内存资源库。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, StoredResource] = {}

    def put_new(self, resource: NormalizedResource) -> StoredResource:
        """幂等创建；ID 已存在 -> ``ALREADY_EXISTS``。"""
        with self._lock:
            if resource.resource_id in self._items:
                existing = self._items[resource.resource_id]
                raise RejectionError(
                    RejectReason.ALREADY_EXISTS,
                    f"资源已存在: {resource.kind.value}/{resource.resource_id}",
                    details={
                        "resource_id": resource.resource_id,
                        "existing_version": existing.version,
                    },
                )
            snapshot = StoredResource(
                resource=resource,
                version=1,
                created_at=_now(),
                updated_at=_now(),
            )
            self._items[resource.resource_id] = snapshot
            return snapshot

    def update(
        self, resource: NormalizedResource, expected_version: int
    ) -> StoredResource:
        """带期望版本的更新；版本不匹配 -> ``VERSION_CONFLICT``。

        整个检查-替换过程持锁；版本号递增，时间戳更新，保证
        “要么完整生效、要么完全不变”。
        """
        with self._lock:
            existing = self._items.get(resource.resource_id)
            if existing is None:
                raise RejectionError(
                    RejectReason.NOT_FOUND,
                    f"资源不存在: {resource.resource_id}",
                    details={"resource_id": resource.resource_id},
                )
            if existing.version != expected_version:
                raise RejectionError(
                    RejectReason.VERSION_CONFLICT,
                    f"资源 {resource.resource_id} 已被其他事务修改",
                    details={
                        "resource_id": resource.resource_id,
                        "expected_version": expected_version,
                        "current_version": existing.version,
                    },
                )
            snapshot = StoredResource(
                resource=resource,
                version=existing.version + 1,
                created_at=existing.created_at,
                updated_at=_now(),
            )
            self._items[resource.resource_id] = snapshot
            return snapshot

    def get(self, resource_id: str) -> StoredResource:
        with self._lock:
            snapshot = self._items.get(resource_id)
            if snapshot is None:
                raise RejectionError(
                    RejectReason.NOT_FOUND,
                    f"资源不存在: {resource_id}",
                    details={"resource_id": resource_id},
                )
            return snapshot

    def exists(self, resource_id: str) -> bool:
        with self._lock:
            return resource_id in self._items

    def list_all(self) -> list[StoredResource]:
        with self._lock:
            return list(self._items.values())

    def delete(self, resource_id: str) -> None:
        with self._lock:
            self._items.pop(resource_id, None)

    def reset(self) -> None:
        """清空全部数据（测试隔离用）。"""
        with self._lock:
            self._items.clear()
