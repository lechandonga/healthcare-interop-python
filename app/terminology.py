"""本地术语码表：代码系统的本地映射与校验。

设计要点
--------
* 每个代码系统记录其可用 FHIR 版本闭区间；URI 在版本间发生变化的系统
  （如 SNOMED CT）按版本分别登记，跨版本使用即判定不兼容。
* 本地映射把“来源系统 + 来源码”映射到唯一“规范码”。
* 若同一来源码在码表中被声明为映射到多个不同规范码，则该键被登记为
  **冲突墓碑**：加载不吞掉冲突，查询命中时抛 ``MAPPING_CONFLICT``，
  绝不静默选择任一目标（避免改变临床语义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import RejectReason, RejectionError
from app.models import Coding, FhirVersion, VERSION_ORDER


class MappingConflict(Exception):
    """码表加载期检测到的一对多映射冲突。"""

    def __init__(self, source_system: str, source_code: str, targets: list[str]) -> None:
        self.source_system = source_system
        self.source_code = source_code
        self.targets = targets
        super().__init__(
            f"mapping conflict: {source_system}#{source_code} -> {targets}"
        )


@dataclass(frozen=True)
class CodeSystemEntry:
    """代码系统元数据。"""

    uri: str
    name: str
    min_version: str = FhirVersion.DSTU3.value
    max_version: str = FhirVersion.R5.value


@dataclass
class CodeDef:
    """单个代码定义。"""

    system: str
    code: str
    display: str
    valid_versions: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MappingEntry:
    """本地映射条目：来源编码 -> 规范编码。"""

    source_system: str
    source_code: str
    target: Coding


# 冲突墓碑：表示该 (system, code) 存在一对多映射，禁止消解
CONFLICT: Coding = Coding(system="urn:internal:conflict", code="CONFLICT")

_SYSTEM_LOINC = "http://loinc.org"
_SYSTEM_SNOMED_OLD = "http://snomed.info/sct"
_SYSTEM_SNOMED_R4 = "http://snomed.info/sct"
# 为显式表达版本不兼容，STU3 采用其历史试验性 URI
_SYSTEM_SNOMED_STU3 = "http://snomed.info/sct-stu3"
_SYSTEM_ICD10 = "http://hl7.org/fhir/sid/icd-10"
_SYSTEM_GENDER = "http://hl7.org/fhir/administrative-gender"
_SYSTEM_LOCAL_OBS = "http://local.example.org/codes/observation"
_SYSTEM_LOCAL_DX = "http://local.example.org/codes/diagnosis"
_SYSTEM_CONFLICT = "http://local.example.org/codes/conflicting"


def _all_versions() -> tuple[str, ...]:
    return tuple(VERSION_ORDER)


class TerminologyService:
    """本地术语码表服务（构造后只读，线程安全）。"""

    def __init__(self) -> None:
        self._systems: dict[str, CodeSystemEntry] = {}
        self._codes: dict[tuple[str, str], CodeDef] = {}
        self._mappings: dict[tuple[str, str], Coding] = {}

    # ------------------------------------------------------------------ 装载
    def add_system(self, entry: CodeSystemEntry) -> None:
        self._systems[entry.uri] = entry

    def add_code(self, code: CodeDef) -> None:
        if not code.system or not code.code:
            raise ValueError("code system / code 不能为空")
        self._codes[(code.system, code.code)] = code

    def add_mapping(self, entry: MappingEntry) -> None:
        """添加映射；与既有映射目标不同则登记冲突墓碑。"""
        if not entry.source_system or not entry.source_code:
            raise ValueError("mapping source 不能为空")
        if not entry.target.system or not entry.target.code:
            raise ValueError("mapping target 不能为空")
        key = (entry.source_system, entry.source_code)
        existing = self._mappings.get(key)
        if existing is None:
            self._mappings[key] = entry.target
        elif existing is CONFLICT:
            raise MappingConflict(entry.source_system, entry.source_code, [])
        elif (existing.system, existing.code) != (
            entry.target.system,
            entry.target.code,
        ):
            self._mappings[key] = CONFLICT
            raise MappingConflict(
                entry.source_system,
                entry.source_code,
                [f"{existing.system}#{existing.code}",
                 f"{entry.target.system}#{entry.target.code}"],
            )
        # 完全相同的重复映射 -> 幂等忽略

    def load_defaults(self) -> None:
        """装载内置本地码表（含一个已知冲突映射用于校验拒绝路径）。"""
        # ---- 代码系统 ----
        self.add_system(CodeSystemEntry(_SYSTEM_LOINC, "LOINC"))
        self.add_system(CodeSystemEntry(
            _SYSTEM_SNOMED_STU3, "SNOMED CT (STU3 历史 URI)",
            min_version=FhirVersion.DSTU3.value, max_version=FhirVersion.DSTU3.value))
        self.add_system(CodeSystemEntry(
            _SYSTEM_SNOMED_R4, "SNOMED CT",
            min_version=FhirVersion.R4.value, max_version=FhirVersion.R5.value))
        self.add_system(CodeSystemEntry(_SYSTEM_ICD10, "ICD-10"))
        self.add_system(CodeSystemEntry(
            _SYSTEM_GENDER, "Administrative Gender"))
        self.add_system(CodeSystemEntry(_SYSTEM_LOCAL_OBS, "本地观察码"))
        self.add_system(CodeSystemEntry(_SYSTEM_LOCAL_DX, "本地诊断码"))
        self.add_system(CodeSystemEntry(_SYSTEM_CONFLICT, "本地冲突演示码表"))

        # ---- 代码 ----
        def _c(system: str, code: str, display: str,
               versions: tuple[str, ...] | None = None) -> None:
            self.add_code(CodeDef(system, code, display, versions or _all_versions()))

        # LOINC
        _c(_SYSTEM_LOINC, "85354-9", "Blood pressure panel")
        _c(_SYSTEM_LOINC, "2339-0", "Glucose [Mass/volume] in Blood")
        # SNOMED：同一概念码在 STU3 历史 URI 与 R4+ 标准 URI 下分别登记
        _c(_SYSTEM_SNOMED_STU3, "38341003", "Systolic blood pressure",
           (FhirVersion.DSTU3.value,))
        _c(_SYSTEM_SNOMED_R4, "38341003", "Systolic blood pressure",
           (FhirVersion.R4.value, FhirVersion.R5.value))
        _c(_SYSTEM_SNOMED_R4, "16184000", "Drug induced hypotension")
        # ICD-10
        _c(_SYSTEM_ICD10, "I10", "Essential (primary) hypertension")
        _c(_SYSTEM_ICD10, "E11.9", "Type 2 diabetes mellitus without complications")
        # 性别
        for g, d in (("male", "Male"), ("female", "Female"),
                     ("other", "Other"), ("unknown", "Unknown")):
            _c(_SYSTEM_GENDER, g, d)
        # 本地观察码
        _c(_SYSTEM_LOCAL_OBS, "BP", "血压（本地）")
        _c(_SYSTEM_LOCAL_OBS, "GLU", "血糖（本地）")
        # 本地诊断码
        _c(_SYSTEM_LOCAL_DX, "HTN", "高血压（本地）")
        # 冲突演示码
        _c(_SYSTEM_CONFLICT, "X1", "存在多义映射的概念")

        # ---- 映射 ----
        self.add_mapping(MappingEntry(
            _SYSTEM_LOCAL_OBS, "BP",
            Coding(_SYSTEM_LOINC, "85354-9", "Blood pressure panel")))
        self.add_mapping(MappingEntry(
            _SYSTEM_LOCAL_OBS, "GLU",
            Coding(_SYSTEM_LOINC, "2339-0", "Glucose [Mass/volume] in Blood")))
        self.add_mapping(MappingEntry(
            _SYSTEM_LOCAL_DX, "HTN",
            Coding(_SYSTEM_ICD10, "I10", "Essential (primary) hypertension")))
        # X1 的冲突映射：声明两次不同目标
        try:
            self.add_mapping(MappingEntry(
                _SYSTEM_CONFLICT, "X1",
                Coding(_SYSTEM_ICD10, "I10", "hypertension")))
            self.add_mapping(MappingEntry(
                _SYSTEM_CONFLICT, "X1",
                Coding(_SYSTEM_ICD10, "E11.9", "diabetes")))
        except MappingConflict:
            # 冲突已被登记为墓碑；加载阶段记录但不阻止服务启动
            pass

    # ------------------------------------------------------------------ 查询
    def validate_code(self, coding: Coding, fhir_version: str) -> Coding:
        """校验编码合法性与版本兼容性，返回规范 :class:`Coding`。"""
        if not coding.system or not coding.code:
            raise RejectionError(
                RejectReason.ILLEGAL_CODE,
                "编码的 system 或 code 为空",
                field="coding",
                details={"system": coding.system, "code": coding.code},
            )
        if not isinstance(coding.system, str) or not isinstance(coding.code, str):
            raise RejectionError(
                RejectReason.ILLEGAL_CODE,
                "编码的 system/code 必须为字符串",
                field="coding",
            )

        system = self._systems.get(coding.system)
        if system is None:
            raise RejectionError(
                RejectReason.CODE_SYSTEM_UNKNOWN,
                f"未知代码系统: {coding.system}",
                field="coding.system",
                details={"system": coding.system},
            )

        if not (
            VERSION_ORDER.index(system.min_version)
            <= VERSION_ORDER.index(fhir_version)
            <= VERSION_ORDER.index(system.max_version)
        ):
            raise RejectionError(
                RejectReason.TERMINOLOGY_VERSION_INCOMPATIBLE,
                f"代码系统 {system.name} 不支持 FHIR {fhir_version}",
                field="coding.system",
                details={
                    "system": coding.system,
                    "requested_version": fhir_version,
                    "supported_range": [system.min_version, system.max_version],
                },
            )

        code_def = self._codes.get((coding.system, coding.code))
        if code_def is None:
            raise RejectionError(
                RejectReason.CODE_NOT_FOUND,
                f"代码系统中不存在代码: {coding.code}",
                field="coding.code",
                details={"system": coding.system, "code": coding.code},
            )

        if code_def.valid_versions and fhir_version not in code_def.valid_versions:
            raise RejectionError(
                RejectReason.TERMINOLOGY_VERSION_INCOMPATIBLE,
                f"代码 {coding.code} 在 FHIR {fhir_version} 下不可用",
                field="coding.code",
                details={
                    "system": coding.system,
                    "code": coding.code,
                    "requested_version": fhir_version,
                    "valid_versions": list(code_def.valid_versions),
                },
            )

        return Coding(
            system=coding.system,
            code=coding.code,
            display=code_def.display,
        )

    def map_code(self, coding: Coding, fhir_version: str) -> Coding:
        """来源编码 -> 规范码。

        1. 先校验来源编码本身合法；
        2. 若存在本地映射则映射到规范码，并对规范码在目标版本再次校验；
        3. 命中冲突墓碑 -> ``MAPPING_CONFLICT``；
        4. 无映射时返回校验后的原编码。
        """
        self.validate_code(coding, fhir_version)
        key = (coding.system, coding.code)
        target = self._mappings.get(key)
        if target is None:
            return Coding(system=coding.system, code=coding.code,
                          display=coding.display)
        if target is CONFLICT:
            raise RejectionError(
                RejectReason.MAPPING_CONFLICT,
                f"来源编码 {coding.system}#{coding.code} 存在冲突的本地映射，"
                "拒绝自动消解以避免改变临床语义",
                field="coding",
                details={"source_system": coding.system,
                         "source_code": coding.code},
            )
        return self.validate_code(target, fhir_version)
