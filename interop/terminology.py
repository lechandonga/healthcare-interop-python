"""本地术语码表：代码校验与映射。

规则：
- Coding.system 可携带版本："http://loinc.org|2.74"；与本地码表版本不一致即
  CODE_SYSTEM_VERSION_INCOMPATIBLE，绝不按"差不多"处理。
- 一个源代码映射到多个不同目标代码时，查询即抛 MAPPING_CONFLICT，
  绝不静默取其一而改变临床语义。
- 本地码表没有的代码抛 UNKNOWN_CODE。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import InteropError, RejectReason
from .models import Coding


@dataclass
class _CodeSystem:
    name: str
    version: str
    codes: dict[str, str] = field(default_factory=dict)  # code -> display


def _split_system(system: str) -> tuple[str, str | None]:
    if "|" in system:
        name, _, ver = system.partition("|")
        return name, ver
    return system, None


# 内置本地码表（模拟 LOINC 子集 + 本地院内码表）
_SEED_SYSTEMS = [
    _CodeSystem("http://loinc.org", "2.74", {
        "8480-6": "Systolic blood pressure",
        "8462-4": "Diastolic blood pressure",
        "9279-1": "Respiratory rate",
    }),
    _CodeSystem("http://local.example/clinical", "1.0", {
        "SBP": "收缩压",
        "DBP": "舒张压",
    }),
]

# 内置映射：院内码 -> LOINC（一对一，无冲突）
_SEED_MAPPINGS = [
    ("http://local.example/clinical", "1.0", "SBP", "http://loinc.org", "8480-6"),
    ("http://local.example/clinical", "1.0", "DBP", "http://loinc.org", "8462-4"),
]


class TerminologyService:
    def __init__(self) -> None:
        self._systems: dict[str, _CodeSystem] = {s.name: s for s in _SEED_SYSTEMS}
        # (source_system, source_version, source_code) -> set[(target_system, target_code)]
        self._mappings: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
        for ssys, sver, scode, tsys, tcode in _SEED_MAPPINGS:
            self.register_mapping(ssys, sver, scode, tsys, tcode)

    # ---- 码表管理 ----
    def register_system(self, name: str, version: str, codes: dict[str, str]) -> None:
        self._systems[name] = _CodeSystem(name, version, dict(codes))

    def register_mapping(self, source_system: str, source_version: str, source_code: str,
                         target_system: str, target_code: str) -> None:
        key = (source_system, source_version, source_code)
        self._mappings.setdefault(key, set()).add((target_system, target_code))

    # ---- 校验与映射 ----
    def _resolve_system(self, system: str) -> tuple[_CodeSystem, str]:
        name, ver = _split_system(system)
        cs = self._systems.get(name)
        if cs is None:
            raise InteropError(RejectReason.UNKNOWN_CODE, f"未知代码系统: {name!r}", field="code.system")
        if ver is not None and ver != cs.version:
            raise InteropError(
                RejectReason.CODE_SYSTEM_VERSION_INCOMPATIBLE,
                f"代码系统 {name!r} 版本不兼容: 请求 {ver!r}, 本地 {cs.version!r}",
                field="code.system")
        return cs, cs.version

    def validate(self, coding: Coding) -> Coding:
        """校验代码在本地码表中存在；返回补全 display 的 Coding。"""
        cs, _ = self._resolve_system(coding.system)
        display = cs.codes.get(coding.code)
        if display is None:
            raise InteropError(
                RejectReason.UNKNOWN_CODE,
                f"代码 {coding.code!r} 不存在于 {cs.name}|{cs.version}",
                field="code.code")
        return Coding(system=f"{cs.name}|{cs.version}", code=coding.code,
                      display=coding.display or display)

    def map_code(self, coding: Coding, target_system: str) -> Coding:
        """映射到目标代码系统。冲突/无映射/版本不兼容均显式报错。"""
        source = self.validate(coding)
        sname, sver = _split_system(source.system)
        if sname == target_system:
            return source
        key = (sname, sver, source.code)
        targets = self._mappings.get(key, set())
        matched = {(ts, tc) for ts, tc in targets if ts == target_system}
        if not matched:
            raise InteropError(
                RejectReason.UNKNOWN_CODE,
                f"无映射: {sname}|{sver}#{source.code} -> {target_system}")
        if len(matched) > 1:
            raise InteropError(
                RejectReason.MAPPING_CONFLICT,
                f"映射冲突: {sname}#{source.code} 在 {target_system} 下有多个目标 "
                f"{sorted(matched)}，需人工裁决，不静默取其一")
        _, tcode = next(iter(matched))
        return self.validate(Coding(system=target_system, code=tcode))
