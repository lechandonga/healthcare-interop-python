"""归一化后的内部数据模型（与具体标准版本解耦）。"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field


class StandardVersion(enum.Enum):
    """本服务支持的标准版本范围。"""

    FHIR_R4 = "fhir-r4"
    FHIR_R5 = "fhir-r5"

    @classmethod
    def parse(cls, raw: str) -> "StandardVersion":
        for v in cls:
            if v.value == raw:
                return v
        from .errors import InteropError, RejectReason
        raise InteropError(
            RejectReason.UNSUPPORTED_STANDARD_VERSION,
            f"不支持的标准版本: {raw!r}，支持: {[v.value for v in cls]}",
        )


class ResourceType(enum.Enum):
    PATIENT = "Patient"
    OBSERVATION = "Observation"

    @classmethod
    def parse(cls, raw: str) -> "ResourceType":
        for v in cls:
            if v.value == raw:
                return v
        from .errors import InteropError, RejectReason
        raise InteropError(
            RejectReason.UNSUPPORTED_RESOURCE_TYPE,
            f"不支持的资源类型: {raw!r}，支持: {[v.value for v in cls]}",
        )


@dataclass(frozen=True)
class Coding:
    system: str
    code: str
    display: str | None = None


@dataclass
class NormalizedResource:
    """归一化资源：解析后统一为此结构再进入存储。"""

    resource_type: ResourceType
    resource_id: str
    standard_version: StandardVersion
    payload: dict = field(default_factory=dict)          # 归一化后的临床字段
    references: list[str] = field(default_factory=list)  # 形如 "Patient/p1"
    sensitive_fields: dict = field(default_factory=dict) # 敏感字段原文（去标识化前）

    def reference_key(self) -> str:
        return f"{self.resource_type.value}/{self.resource_id}"


@dataclass
class StoredRecord:
    """存储层记录：带版本号用于乐观并发控制。"""

    resource: NormalizedResource
    version: int
    idempotency_keys: set[str] = field(default_factory=set)

    def bump(self) -> "StoredRecord":
        return dataclasses.replace(self, version=self.version + 1)
