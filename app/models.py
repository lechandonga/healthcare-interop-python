"""核心领域模型与类型定义。

归一化后的内部表示 :class:`NormalizedResource` 不绑定某个 FHIR 版本，
同时保留来源版本与逐字段来源，便于解释“这个结论来自哪个标准版本”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FhirVersion(str, Enum):
    """支持的标准版本。"""

    DSTU3 = "3.0"   # FHIR STU3
    R4 = "4.0"      # FHIR R4
    R5 = "5.0"      # FHIR R5


SUPPORTED_VERSIONS: tuple[str, ...] = tuple(v.value for v in FhirVersion)

# 版本顺序，用于版本兼容判断（旧 -> 新）
VERSION_ORDER: tuple[str, ...] = (
    FhirVersion.DSTU3.value,
    FhirVersion.R4.value,
    FhirVersion.R5.value,
)


class ResourceKind(str, Enum):
    PATIENT = "Patient"
    OBSERVATION = "Observation"
    CONDITION = "Condition"


@dataclass(frozen=True)
class Coding:
    """编码的归一表示。"""

    system: str
    code: str
    display: str | None = None


@dataclass
class NormalizedResource:
    """归一化资源（内部统一表示）。

    :param kind: 资源类型
    :param resource_id: 逻辑 ID
    :param source_version: 来源 FHIR 版本
    :param data: 归一后的字段数据
    :param field_provenance: 字段 -> 来源版本/路径的解释信息
    :param references: 该资源持有的引用（``"ResourceType/id"``）
    :param sensitive: 当前包含真实值的敏感字段集合
    """

    kind: ResourceKind
    resource_id: str
    source_version: str
    data: dict[str, Any] = field(default_factory=dict)
    field_provenance: dict[str, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    sensitive: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class StoredResource:
    """存储中的资源快照（含并发控制版本号）。"""

    resource: NormalizedResource
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BatchItemResult:
    """批量中单条记录的结果。"""

    index: int
    ok: bool
    resource_id: str | None = None
    version: int | None = None
    reason: str | None = None
    message: str | None = None
    field_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
