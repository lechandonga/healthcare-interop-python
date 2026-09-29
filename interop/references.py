"""资源间引用完整性与版本兼容检查。

兼容矩阵（集中定义，见 README），版本序 STU3 < R4 < R5：
- 同版本互引：允许
- 高版本资源引用低版本资源（如 R5 -> R4、R4 -> STU3）：允许（向后兼容读）
- 低版本资源引用高版本资源（如 R4 -> R5、STU3 -> R4）：拒绝 REFERENCE_VERSION_MISMATCH
"""

from __future__ import annotations

from typing import Callable

from .errors import InteropError, RejectReason
from .models import NormalizedResource, StandardVersion

_VERSION_ORDER = {
    StandardVersion.FHIR_STU3: 3,
    StandardVersion.FHIR_R4: 4,
    StandardVersion.FHIR_R5: 5,
}


def is_version_compatible(source: StandardVersion, target: StandardVersion) -> bool:
    """source 资源引用 target 资源是否版本兼容。"""
    return _VERSION_ORDER[target] <= _VERSION_ORDER[source]


def check_references(resource: NormalizedResource,
                     resolve: Callable[[str], NormalizedResource | None]) -> None:
    """检查全部引用：目标必须存在且版本兼容，否则显式拒绝。"""
    for ref in resource.references:
        target = resolve(ref)
        if target is None:
            raise InteropError(
                RejectReason.BROKEN_REFERENCE,
                f"{resource.reference_key()} 引用了不存在的资源 {ref!r}",
                field="reference")
        if not is_version_compatible(resource.standard_version, target.standard_version):
            raise InteropError(
                RejectReason.REFERENCE_VERSION_MISMATCH,
                f"{resource.reference_key()}({resource.standard_version.value}) 不能引用 "
                f"{ref}({target.standard_version.value})：旧版本不得引用新版本",
                field="reference")
