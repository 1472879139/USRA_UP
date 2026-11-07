"""v2 工具契约：命名输入、参数和任意 JSON 输出，使用独立可选依赖。"""
from copy import deepcopy
from dataclasses import dataclass, field
import re
import threading
import time

from jsonschema import Draft202012Validator
from referencing import Registry

from .backends import BackendError
from .contract import APIError, canonical, sha256

ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
FILE_REFERENCE = {"type": "object", "required": ["file_id"], "additionalProperties": False,
                  "properties": {"file_id": ID}}


def object_schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def map_schema(value, visit=None, transform_ref=None):
    """只遍历 Schema 节点，保留 const/default/examples 中的普通 JSON 数据。"""
    if not isinstance(value, dict):
        return deepcopy(value)
    if visit is not None:
        visit(value)
    result = deepcopy(value)
    for key, child in value.items():
        if key in {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"} and isinstance(child, dict):
            result[key] = {name: map_schema(item, visit, transform_ref) for name, item in child.items()}
        elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(child, list):
            result[key] = [map_schema(item, visit, transform_ref) for item in child]
        elif key in {"items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "not",
                     "if", "then", "else", "propertyNames", "contentSchema"}:
            result[key] = map_schema(child, visit, transform_ref)
        elif key in {"$ref", "$dynamicRef"} and transform_ref is not None:
            result[key] = transform_ref(child)
    return result


def schema(value):
    """拒绝外部引用；仅允许能在当前文档解析的本地 JSON Pointer。"""
    root = deepcopy(value)
    if not isinstance(root, dict):
        raise ValueError("Schema 须为对象")

    def inspect(item):
        for key, child in item.items():
            if key == "$id":
                raise ValueError("Schema 不允许改变解析地址")
            if key in {"$ref", "$dynamicRef"}:
                if child != "#" and (not isinstance(child, str) or not child.startswith("#/")):
                    raise ValueError("Schema 只允许本地 JSON Pointer")
                target = root
                if child != "#":
                    try:
                        for part in child[2:].split("/"):
                            part = part.replace("~1", "/").replace("~0", "~")
                            target = target[int(part)] if isinstance(target, list) else target[part]
                    except (KeyError, TypeError, IndexError, ValueError):
                        raise ValueError("Schema 本地引用不存在") from None
    map_schema(root, visit=inspect)
    Draft202012Validator.check_schema(root)
    canonical(root)
    return root


def validate(value, contract, code="INVALID_ARGUMENTS"):
    try:
        canonical(value)
        valid = Draft202012Validator(contract, registry=Registry()).is_valid(value)
    except Exception:
        valid = False
    if not valid:
        raise APIError(400, code, "数据不符合该工具的 JSON Schema，请查询 /v2/tools/{tool_id}")


def mounted_schema(value, pointer):
    """把独立 Schema 安装到父文档，保留其本地引用语义。"""
    return map_schema(value, transform_ref=lambda child: "#/" + pointer + child[1:])


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    title: str
    description: str
    version: str
    mcp_name: str
    inputs: dict
    required_inputs: tuple
    parameters_schema: dict
    output_schema: dict
    artifact_types: dict
    execution_mode: str

    def contract(self):
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.tool_id)
                or not re.fullmatch(r"ursa_v2_[a-zA-Z0-9_]{1,56}", self.mcp_name)
                or not self.title or not self.description or not self.version
                or self.execution_mode not in {"gpu", "cpu", "mock"}
                or not isinstance(self.inputs, dict)
                or len(self.inputs) > 16
                or set(self.required_inputs) - set(self.inputs)):
            raise ValueError("v2 工具基本定义无效")
        properties = {}
        for name, slot in self.inputs.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or not isinstance(slot, dict):
                raise ValueError("输入槽名称或定义无效")
            if slot.get("kind") == "file":
                if (not slot.get("media_types") or type(slot.get("max_bytes")) is not int
                        or not 1 <= slot["max_bytes"] <= 16 * 1024 ** 2
                        or any(not re.fullmatch(r"[a-z0-9.+_-]+/[a-z0-9.+_-]+", m) for m in slot["media_types"])):
                    raise ValueError("文件输入须声明媒体类型和 1～16MiB 的大小限制")
                properties[name] = deepcopy(FILE_REFERENCE)
            elif slot.get("kind") == "json":
                properties[name] = schema(slot["schema"])
            else:
                raise ValueError("输入槽 kind 仅接受 file 或 json")
        for role, media_types in self.artifact_types.items():
            if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role) or not media_types
                    or any(not re.fullmatch(r"[a-z0-9.+_-]+/[a-z0-9.+_-]+", media) for media in media_types)):
                raise ValueError("产物角色及媒体类型不能为空")
        parameters = schema(self.parameters_schema)
        if parameters.get("type") != "object":
            raise ValueError("parameters_schema 须描述 JSON 对象")
        outputs = schema(self.output_schema)
        input_schema = object_schema(properties, self.required_inputs)
        for name, slot in self.inputs.items():
            if slot["kind"] == "json":
                input_schema["properties"][name] = mounted_schema(properties[name], "properties/" + name)
        schema(input_schema)
        result = {"api_version": "2.0.0", "tool_id": self.tool_id, "title": self.title,
            "description": self.description, "tool_version": self.version, "mcp_name": self.mcp_name,
            "input_slots": deepcopy(self.inputs), "input_schema": input_schema,
            "parameters_schema": parameters, "output_schema": outputs,
            "artifact_types": deepcopy(self.artifact_types), "execution_mode": self.execution_mode}
        result["contract_revision"] = sha256(canonical(result).encode("utf-8"))
        if len(canonical(result).encode("utf-8")) > 65536:
            raise ValueError("单工具契约超过 64KiB")
        return result


@dataclass(frozen=True)
class FileInput:
    file_id: str
    path: object = field(repr=False)
    sha256: str
    size_bytes: int
    media_type: str


@dataclass
class ToolContext:
    job_id: str
    job_dir: object
    results_dir: object
    cancelled: threading.Event
    stopping: threading.Event
    deadline: float

    def check_active(self):
        if self.stopping.is_set():
            raise BackendError("SERVER_STOPPED", "服务停止中断任务，请查询已接收任务状态", True)
        if self.cancelled.is_set():
            raise BackendError("JOB_CANCELLED", "任务已取消")
        if time.monotonic() >= self.deadline:
            raise BackendError("JOB_TIMEOUT", "工具执行超过时间上限", True)


@dataclass(frozen=True)
class ProducedArtifact:
    name: str
    role: str
    media_type: str


@dataclass
class AdapterResult:
    outputs: object
    artifacts: list = field(default_factory=list)


class ToolAdapter:
    spec: ToolSpec

    @property
    def capability(self):
        return {"id": self.spec.tool_id, "display_name": self.spec.title}

    def availability(self):
        return "ready"

    def execute(self, context, inputs, parameters):
        raise NotImplementedError


def job_schema(contract=None):
    artifact = {"type": "object", "required": ["artifact_id", "name", "role", "media_type", "size_bytes", "sha256", "download_path"],
        "properties": {"artifact_id": ID, "name": {"type": "string"}, "role": {"type": "string"},
            "media_type": {"type": "string"}, "size_bytes": {"type": "integer", "minimum": 0},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "download_path": {"type": "string", "pattern": "^/v2/artifacts/[0-9a-f]{32}$"}}}
    output = mounted_schema(contract["output_schema"], "properties/result/properties/outputs") if contract else {}
    return {"type": "object", "required": ["api_version", "job_id", "tool_id", "tool_version", "status", "error", "result"],
        "properties": {"api_version": {"const": "2.0.0"}, "job_id": ID, "tool_id": {"type": "string"},
            "tool_version": {"type": "string"}, "status": {"enum": ["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]},
            "error": {"type": ["object", "null"]}, "result": {"type": ["object", "null"], "required": ["outputs", "artifacts", "execution_mode"],
                "properties": {"outputs": output, "artifacts": {"type": "array", "items": artifact},
                    "execution_mode": {"enum": ["gpu", "cpu", "mock"]}}}}}
