"""多标准版本 / 多资源格式的解析与归一化。

严格解析原则
------------
* 顶层非对象 -> ``NOT_A_RESOURCE_OBJECT``
* 缺少必填字段 -> ``MISSING_FIELD``；必填字段为空值 -> ``EMPTY_FIELD``
* 字段类型错误 -> ``INVALID_FIELD_TYPE``；结构不完整（如 coding 元素非对象）
  -> ``STRUCTURE_INCOMPLETE``
* 未知字段（不在该资源类型规范内）-> ``UNKNOWN_FIELD``
* 字段在声明的 FHIR 版本中不存在 / 语义不兼容
  -> ``INCOMPATIBLE_FIELD``（不按默认值处理，不静默丢弃）
* 非法/未知名码、版本不兼容码、冲突映射 -> 术语服务拒绝

支持范围（内置简化子集）
------------------------
FHIR STU3(3.0) / R4(4.0) / R5(5.0)；
资源：Patient、Observation、Condition。
"""
from __future__ import annotations

from typing import Any

from app.errors import RejectReason, RejectionError
from app.models import (
    Coding,
    FhirVersion,
    NormalizedResource,
    ResourceKind,
    VERSION_ORDER,
)
from app.terminology import TerminologyService

# 字段规格：字段名 -> {"versions": 可用版本集合, "required": 必填版本集合}
# 未列入规格的字段一律按 UNKNOWN_FIELD 拒绝（严格白名单）。
FieldSpec = dict[str, dict[str, Any]]

SPECS: dict[ResourceKind, FieldSpec] = {
    ResourceKind.PATIENT: {
        "resourceType": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "id": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "identifier": {"versions": VERSION_ORDER, "required": ()},
        "name": {"versions": VERSION_ORDER, "required": ()},
        "gender": {"versions": VERSION_ORDER, "required": ()},
        "birthDate": {"versions": VERSION_ORDER, "required": ()},
        "telecom": {"versions": VERSION_ORDER, "required": ()},
        "address": {"versions": VERSION_ORDER, "required": ()},
        "active": {"versions": VERSION_ORDER, "required": ()},
    },
    ResourceKind.OBSERVATION: {
        "resourceType": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "id": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "status": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "code": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "subject": {"versions": VERSION_ORDER, "required": ()},
        # 生效时间：STU3 用 effectiveDateTime / R4 起语义保留同名；
        # effectiveInstant 为 R5 新增，R4/STU3 使用即版本不兼容。
        "effectiveDateTime": {"versions": VERSION_ORDER, "required": ()},
        "effectiveInstant": {
            "versions": (FhirVersion.R5.value,), "required": ()
        },
        # 备注：R4 起 comment 更名为 note，STU3 字段在 R4+ 不兼容
        "comment": {
            "versions": (FhirVersion.DSTU3.value,), "required": ()
        },
        "note": {
            "versions": (FhirVersion.R4.value, FhirVersion.R5.value),
            "required": (),
        },
        "valueQuantity": {"versions": VERSION_ORDER, "required": ()},
        "category": {"versions": VERSION_ORDER, "required": ()},
    },
    ResourceKind.CONDITION: {
        "resourceType": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "id": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "code": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        "subject": {"versions": VERSION_ORDER, "required": VERSION_ORDER},
        # 临床状态：STU3 为字符串 clinicalStatus；R4 起改为 CodeableConcept
        "clinicalStatus": {
            "versions": VERSION_ORDER,
            "required": (FhirVersion.R4.value, FhirVersion.R5.value),
            "kind": {
                FhirVersion.DSTU3.value: "string",
                FhirVersion.R4.value: "CodeableConcept",
                FhirVersion.R5.value: "CodeableConcept",
            },
        },
        "verificationStatus": {
            "versions": (FhirVersion.R4.value, FhirVersion.R5.value),
            "required": (),
        },
        # 起病时间：R5 新增 onsetPeriod，旧版本使用即不兼容
        "onsetDateTime": {"versions": VERSION_ORDER, "required": ()},
        "onsetPeriod": {
            "versions": (FhirVersion.R5.value,), "required": ()
        },
        "assertedDate": {
            "versions": (FhirVersion.DSTU3.value,), "required": ()
        },
        "recordedDate": {
            "versions": (FhirVersion.R4.value, FhirVersion.R5.value),
            "required": (),
        },
    },
}

# 允许的状态枚举（归一化时校验）
OBSERVATION_STATUSES = {
    "registered", "preliminary", "final", "amended",
    "cancelled", "entered-in-error", "unknown",
}
CONDITION_CLINICAL_STRINGS_STU3 = {
    "active", "recurrence", "inactive", "remission",
    "resolved", "unknown",
}
GENDERS = {"male", "female", "other", "unknown"}

_REFERENCE_FIELDS = {"subject": None}  # 占位，实际按资源类型处理


class ResourceParser:
    """FHIR JSON -> :class:`NormalizedResource` 的严格解析器。"""

    def __init__(self, terminology: TerminologyService) -> None:
        self._term = terminology

    # ------------------------------------------------------------------
    def parse(
        self,
        payload: Any,
        fhir_version: str,
        *,
        expected_id: str | None = None,
    ) -> NormalizedResource:
        if fhir_version not in VERSION_ORDER:
            raise RejectionError(
                RejectReason.UNSUPPORTED_VERSION,
                f"不支持的 FHIR 版本: {fhir_version}",
                details={"supplied": fhir_version},
            )
        if not isinstance(payload, dict):
            raise RejectionError(
                RejectReason.NOT_A_RESOURCE_OBJECT,
                "资源顶层必须是 JSON 对象",
                details={"actual_type": type(payload).__name__},
            )

        rtype = payload.get("resourceType")
        if rtype is None:
            raise RejectionError(
                RejectReason.MISSING_FIELD,
                "缺少 resourceType",
                field="resourceType",
            )
        if not isinstance(rtype, str):
            raise RejectionError(
                RejectReason.INVALID_FIELD_TYPE,
                "resourceType 必须为字符串",
                field="resourceType",
            )
        try:
            kind = ResourceKind(rtype)
        except ValueError:
            raise RejectionError(
                RejectReason.UNSUPPORTED_RESOURCE_TYPE,
                f"不支持的资源类型: {rtype}",
                field="resourceType",
                details={"resource_type": rtype},
            )

        spec = SPECS[kind]
        # 未知字段与版本兼容检查
        for name in payload:
            if name not in spec:
                raise RejectionError(
                    RejectReason.UNKNOWN_FIELD,
                    f"资源 {rtype} 含未知字段: {name}",
                    field=name,
                )
            versions = spec[name]["versions"]
            if fhir_version not in versions:
                raise RejectionError(
                    RejectReason.INCOMPATIBLE_FIELD,
                    f"字段 {name} 在 FHIR {fhir_version} 下不被支持 "
                    f"（仅支持 {list(versions)}），拒绝静默丢弃",
                    field=name,
                    details={
                        "field": name,
                        "requested_version": fhir_version,
                        "supported_versions": list(versions),
                    },
                )

        # 必填字段
        for name, meta in spec.items():
            if fhir_version in tuple(meta.get("required", ())):
                if name not in payload:
                    raise RejectionError(
                        RejectReason.MISSING_FIELD,
                        f"必填字段缺失: {name}",
                        field=name,
                    )
                if payload[name] in (None, "", [], {}):
                    raise RejectionError(
                        RejectReason.EMPTY_FIELD,
                        f"必填字段为空: {name}",
                        field=name,
                    )

        rid = self._require_id(payload, expected_id)
        dispatch = {
            ResourceKind.PATIENT: self._parse_patient,
            ResourceKind.OBSERVATION: self._parse_observation,
            ResourceKind.CONDITION: self._parse_condition,
        }
        data, references = dispatch[kind](payload, fhir_version)

        return NormalizedResource(
            kind=kind,
            resource_id=rid,
            source_version=fhir_version,
            data=data,
            field_provenance={
                "_source": f"FHIR/{fhir_version} {kind.value}/{rid}",
            },
            references=references,
            sensitive=set(),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _require_id(payload: dict, expected_id: str | None) -> str:
        rid = payload.get("id")
        if not isinstance(rid, str):
            raise RejectionError(
                RejectReason.INVALID_FIELD_TYPE if "id" in payload
                else RejectReason.MISSING_FIELD,
                "id 必须为非空字符串",
                field="id",
            )
        if not rid.strip():
            raise RejectionError(
                RejectReason.EMPTY_FIELD, "id 不得为空白", field="id"
            )
        if expected_id is not None and rid != expected_id:
            raise RejectionError(
                RejectReason.INCOMPATIBLE_FIELD,
                "URL 中的资源 ID 与请求体 id 不一致，拒绝按默认值处理",
                field="id",
                details={"url_id": expected_id, "body_id": rid},
            )
        return rid

    @staticmethod
    def _type_check(value: Any, py_types: tuple[type, ...], field_name: str,
                    *, empty_ok: bool = False) -> None:
        if value is None:
            return
        if not empty_ok and value in ("", [], {}):
            raise RejectionError(
                RejectReason.EMPTY_FIELD, f"字段为空: {field_name}",
                field=field_name,
            )
        if not isinstance(value, py_types):
            raise RejectionError(
                RejectReason.INVALID_FIELD_TYPE,
                f"字段 {field_name} 类型错误，应为 {py_types}",
                field=field_name,
                details={"actual_type": type(value).__name__},
            )

    def _parse_codings(self, codeable: dict, path: str,
                       fhir_version: str) -> list[dict[str, Any]]:
        """解析 CodeableConcept，映射并校验每个 coding。"""
        if not isinstance(codeable, dict):
            raise RejectionError(
                RejectReason.INVALID_FIELD_TYPE,
                f"{path} 必须为对象",
                field=path,
            )
        raw_codings = codeable.get("coding")
        text = codeable.get("text")
        if raw_codings is None:
            if text:
                # 仅有自由文本、无结构化编码 -> 结构不完整，无法归一
                raise RejectionError(
                    RejectReason.STRUCTURE_INCOMPLETE,
                    f"{path} 仅含 text 而无 coding，无法进行术语归一",
                    field=f"{path}.coding",
                )
            raise RejectionError(
                RejectReason.MISSING_FIELD,
                f"{path} 缺少 coding",
                field=f"{path}.coding",
            )
        if not isinstance(raw_codings, list) or not raw_codings:
            raise RejectionError(
                RejectReason.STRUCTURE_INCOMPLETE,
                f"{path}.coding 必须为非空数组",
                field=f"{path}.coding",
            )

        mapped: list[dict[str, Any]] = []
        for i, c in enumerate(raw_codings):
            cpath = f"{path}.coding[{i}]"
            if not isinstance(c, dict):
                raise RejectionError(
                    RejectReason.STRUCTURE_INCOMPLETE,
                    f"{cpath} 必须为对象",
                    field=cpath,
                )
            system, code, display = c.get("system"), c.get("code"), c.get("display")
            if system is None:
                raise RejectionError(
                    RejectReason.MISSING_FIELD,
                    f"{cpath} 缺少 system",
                    field=f"{cpath}.system",
                )
            if code is None:
                raise RejectionError(
                    RejectReason.MISSING_FIELD,
                    f"{cpath} 缺少 code",
                    field=f"{cpath}.code",
                )
            if not isinstance(system, str) or not isinstance(code, str):
                raise RejectionError(
                    RejectReason.INVALID_FIELD_TYPE,
                    f"{cpath} 的 system/code 必须为字符串",
                    field=cpath,
                )
            if not system or not code:
                raise RejectionError(
                    RejectReason.ILLEGAL_CODE,
                    f"{cpath} 的 system/code 不得为空",
                    field=cpath,
                )
            # 未知字段严格拒绝（coding 内部白名单）
            for k in c:
                if k not in {"system", "code", "display"}:
                    raise RejectionError(
                        RejectReason.UNKNOWN_FIELD,
                        f"{cpath} 含未知字段 {k}",
                        field=f"{cpath}.{k}",
                    )
            try:
                target = self._term.map_code(
                    Coding(system, code, display), fhir_version
                )
            except RejectionError as exc:
                # 术语错误统一补上完整字段路径，便于定位与复现
                suffix = exc.field or ""
                if suffix.startswith("coding."):
                    suffix = suffix[len("coding."):]
                exc.field = f"{cpath}.{suffix}" if suffix and suffix != "coding" else cpath
                raise
            mapped.append({
                "system": target.system,
                "code": target.code,
                "display": target.display,
                "source_system": system,
                "source_code": code,
            })
        return mapped

    def _parse_reference(self, value: Any, field_name: str) -> tuple[str, str]:
        """解析 Reference，返回 (归一引用, 类型)。"""
        if not isinstance(value, dict):
            raise RejectionError(
                RejectReason.INVALID_FIELD_TYPE,
                f"{field_name} 必须为 Reference 对象",
                field=field_name,
            )
        ref = value.get("reference")
        if not isinstance(ref, str) or not ref.strip():
            raise RejectionError(
                RejectReason.REFERENCE_BROKEN,
                f"{field_name}.reference 缺失或为空",
                field=f"{field_name}.reference",
            )
        parts = ref.split("/")
        if len(parts) != 2 or not all(parts):
            raise RejectionError(
                RejectReason.REFERENCE_BROKEN,
                f"{field_name} 引用格式非法，应为 'ResourceType/id'",
                field=f"{field_name}.reference",
                details={"reference": ref},
            )
        rtype, rid = parts
        try:
            ResourceKind(rtype)
        except ValueError:
            raise RejectionError(
                RejectReason.REFERENCE_BROKEN,
                f"{field_name} 引用了不支持的资源类型 {rtype}",
                field=f"{field_name}.reference",
                details={"reference": ref},
            )
        return ref, rtype

    # ------------------------------------------------------------------
    def _parse_patient(self, p: dict, ver: str) -> tuple[dict, list[str]]:
        gender = p.get("gender")
        if gender is not None:
            self._type_check(gender, (str,), "gender")
            if gender not in GENDERS:
                raise RejectionError(
                    RejectReason.ILLEGAL_CODE,
                    f"非法性别代码: {gender}",
                    field="gender",
                    details={"allowed": sorted(GENDERS)},
                )
            coding = self._term.validate_code(
                Coding("http://hl7.org/fhir/administrative-gender", gender), ver
            )
            gender = coding.code
        for f in ("name", "telecom", "address", "identifier"):
            if f in p:
                self._type_check(p[f], (list,), f)
        if "birthDate" in p:
            self._type_check(p["birthDate"], (str,), "birthDate")
        if "active" in p:
            self._type_check(p["active"], (bool,), "active")

        data = {
            "id": p["id"],
            "gender": gender,
            "birthDate": p.get("birthDate"),
            "name": p.get("name", []),
            "telecom": p.get("telecom", []),
            "address": p.get("address", []),
            "identifier": p.get("identifier", []),
            "active": p.get("active"),
        }
        return data, []

    def _parse_observation(self, o: dict, ver: str) -> tuple[dict, list[str]]:
        status = o["status"]
        if status not in OBSERVATION_STATUSES:
            raise RejectionError(
                RejectReason.ILLEGAL_CODE,
                f"非法 Observation.status: {status}",
                field="status",
                details={"allowed": sorted(OBSERVATION_STATUSES)},
            )
        codings = self._parse_codings(o["code"], "code", ver)

        refs: list[str] = []
        if "subject" in o:
            ref, _ = self._parse_reference(o["subject"], "subject")
            refs.append(ref)

        for f in ("effectiveDateTime",):
            if f in o:
                self._type_check(o[f], (str,), f)
        if "effectiveInstant" in o:
            self._type_check(o["effectiveInstant"], (str,), "effectiveInstant")
        if "comment" in o:
            self._type_check(o["comment"], (str,), "comment")
        if "note" in o:
            self._type_check(o["note"], (str,), "note")
        if "valueQuantity" in o:
            vq = o["valueQuantity"]
            if not isinstance(vq, dict):
                raise RejectionError(
                    RejectReason.INVALID_FIELD_TYPE,
                    "valueQuantity 必须为对象",
                    field="valueQuantity",
                )
            if "value" not in vq:
                raise RejectionError(
                    RejectReason.MISSING_FIELD,
                    "valueQuantity 缺少 value",
                    field="valueQuantity.value",
                )
            if not isinstance(vq["value"], (int, float)) or isinstance(
                vq["value"], bool
            ):
                raise RejectionError(
                    RejectReason.INVALID_FIELD_TYPE,
                    "valueQuantity.value 必须为数值",
                    field="valueQuantity.value",
                )
        if "category" in o:
            self._type_check(o["category"], (list,), "category")

        data = {
            "id": o["id"],
            "status": status,
            "codings": codings,
            "effective": o.get("effectiveDateTime")
            or o.get("effectiveInstant"),
            "note": o.get("note") or o.get("comment"),
            "valueQuantity": o.get("valueQuantity"),
        }
        return data, refs

    def _parse_condition(self, c: dict, ver: str) -> tuple[dict, list[str]]:
        codings = self._parse_codings(c["code"], "code", ver)

        refs: list[str] = []
        sub_ref, sub_type = self._parse_reference(c["subject"], "subject")
        if sub_type != ResourceKind.PATIENT.value:
            raise RejectionError(
                RejectReason.REFERENCE_BROKEN,
                f"Condition.subject 必须指向 Patient，实际为 {sub_type}",
                field="subject.reference",
            )
        refs.append(sub_ref)

        clinical = c.get("clinicalStatus")
        if ver == FhirVersion.DSTU3.value:
            if clinical is not None:
                if not isinstance(clinical, str):
                    raise RejectionError(
                        RejectReason.INCOMPATIBLE_FIELD,
                        "STU3 的 clinicalStatus 必须为字符串；"
                        "CodeableConcept 形式属于 R4+ 结构",
                        field="clinicalStatus",
                    )
                if clinical not in CONDITION_CLINICAL_STRINGS_STU3:
                    raise RejectionError(
                        RejectReason.ILLEGAL_CODE,
                        f"非法 STU3 clinicalStatus: {clinical}",
                        field="clinicalStatus",
                    )
            clinical_norm: Any = clinical
        else:
            # R4/R5: 必填 CodeableConcept；收到 STU3 字符串形态属于
            # 跨版本结构不兼容，必须明确拒绝，不静默转换
            if not isinstance(clinical, dict):
                raise RejectionError(
                    RejectReason.INCOMPATIBLE_FIELD,
                    f"FHIR {ver} 的 clinicalStatus 必须为 CodeableConcept 对象；"
                    "字符串形态属于 STU3 结构",
                    field="clinicalStatus",
                    details={
                        "requested_version": ver,
                        "expected_shape": "CodeableConcept",
                        "actual_type": type(clinical).__name__,
                    },
                )
            clinical_codings = self._parse_codings(
                clinical, "clinicalStatus", ver
            )
            clinical_norm = clinical_codings[0]["code"]

        verification = None
        if "verificationStatus" in c:
            verification = self._parse_codings(
                c["verificationStatus"], "verificationStatus", ver
            )[0]["code"]

        for f in ("onsetDateTime", "onsetPeriod", "assertedDate", "recordedDate"):
            if f in c:
                if f == "onsetPeriod":
                    if not isinstance(c[f], dict):
                        raise RejectionError(
                            RejectReason.INVALID_FIELD_TYPE,
                            "onsetPeriod 必须为 Period 对象",
                            field=f,
                        )
                else:
                    self._type_check(c[f], (str,), f)

        data = {
            "id": c["id"],
            "codings": codings,
            "subject": sub_ref,
            "clinicalStatus": clinical_norm,
            "verificationStatus": verification,
            "onset": c.get("onsetDateTime"),
            "onsetPeriod": c.get("onsetPeriod"),
            "recorded": c.get("recordedDate") or c.get("assertedDate"),
        }
        return data, refs
