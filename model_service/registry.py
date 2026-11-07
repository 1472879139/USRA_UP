"""已安装工具的统一目录；状态文件只控制可见性和新任务准入。"""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from .contract import APIError, CAPABILITIES, INPUT_CONTRACT, canonical, decode_json, sha256, validate_metadata

RESERVED_NAMES = {"ursa_get_service_info", "ursa_get_tool_contract", "ursa_get_job",
                  "ursa_cancel_job", "ursa_get_artifacts"}
MODEL_NAMES = {"da-v2-field-delineation": "ursa_delineate_fields",
               "terramind-small-landcover": "ursa_classify_landcover"}


def standalone_schema(name, specification):
    """把本服务的 OpenAPI 本地引用转换成独立 JSON Schema。"""
    schemas, definitions = specification["components"]["schemas"], {}

    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "$ref":
                if not item.startswith("#/components/schemas/"):
                    raise ValueError("工具契约仅支持本地 Schema 引用")
                target = item.rsplit("/", 1)[-1]
                if target not in definitions:
                    definitions[target] = {}
                    definitions[target] = rewrite(schemas[target])
                result[key] = "#/$defs/" + target
            else:
                result[key] = rewrite(item)
        return result

    result = rewrite(schemas[name])
    if definitions:
        result["$defs"] = definitions
    return result


@dataclass(frozen=True)
class ToolDefinition:
    capability: dict
    mcp_name: str
    input_contract: dict
    metadata_schema: dict
    metadata_validator: object


def builtin_definitions():
    from .openapi import specification
    metadata = standalone_schema("Metadata", specification())
    return [ToolDefinition(deepcopy(item), MODEL_NAMES[item["id"]], deepcopy(INPUT_CONTRACT),
                           deepcopy(metadata), validate_metadata) for item in CAPABILITIES]


class ToolRegistry:
    def __init__(self, state_file=None, definitions=None):
        self.state_file = Path(state_file) if state_file is not None else None
        self._tools = {}
        for definition in builtin_definitions() if definitions is None else definitions:
            self.register(definition)

    def register(self, definition):
        """仅供维护者代码登记已安装适配器；不接收网络上传的代码或命令。"""
        if not isinstance(definition, ToolDefinition) or not isinstance(definition.capability, dict):
            raise ValueError("需要 ToolDefinition 工具定义")
        item = definition.capability
        required = {"id", "task", "tier", "display_name", "model", "model_revision", "outputs", "classes"}
        if (not required.issubset(item) or not isinstance(item["id"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item["id"])
                or not isinstance(definition.mcp_name, str)
                or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", definition.mcp_name)
                or definition.mcp_name in RESERVED_NAMES
                or any(not isinstance(item[k], str) or not item[k] for k in ["task", "display_name", "model", "model_revision"])
                or item["tier"] not in {"specialist", "general"}
                or not isinstance(item["outputs"], list) or not item["outputs"]
                or any(not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role) for role in item["outputs"])
                or len(set(item["outputs"])) != len(item["outputs"])
                or not isinstance(item["classes"], dict)
                or not callable(definition.metadata_validator)
                or not isinstance(definition.input_contract, dict)
                or not isinstance(definition.metadata_schema, dict)
                or definition.metadata_schema.get("type") != "object"
                or definition.input_contract.get("media_type") != "image/tiff"):
            raise ValueError("工具定义不完整；v1 注册层只接受已适配的 TIFF 工具")
        if item["id"] in self._tools or any(t.mcp_name == definition.mcp_name for t in self._tools.values()):
            raise ValueError("工具 ID 或 MCP 名称重复")
        canonical(item)
        canonical(definition.metadata_schema)
        self._tools[item["id"]] = deepcopy(definition)

    def management_status(self):
        states = self.states()
        return [{"id": tool_id, "name": tool.capability["display_name"],
                 "state": "removed" if states.get(tool_id, {}).get("removed") else
                          "enabled" if states.get(tool_id, {}).get("enabled", True) else "disabled"}
                for tool_id, tool in self._tools.items()]

    def definition(self, tool_id):
        if not isinstance(tool_id, str) or tool_id not in self._tools:
            raise APIError(404, "TOOL_NOT_FOUND", "工具未注册，请刷新工具目录")
        return deepcopy(self._tools[tool_id])

    def ids(self):
        return tuple(self._tools)

    def states(self):
        if self.state_file is None:
            return {}
        try:
            if self.state_file.is_symlink() or self.state_file.stat().st_size > 65536:
                raise ValueError("工具状态文件异常")
            data = decode_json(self.state_file.read_bytes())
            if set(data) != {"version", "tools"} or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["tools"], dict):
                raise ValueError("工具状态版本或结构错误")
            for tool_id, state in data["tools"].items():
                if (tool_id not in self._tools or not isinstance(state, dict)
                        or set(state) != {"enabled", "removed"}
                        or any(type(value) is not bool for value in state.values())
                        or state["removed"] and state["enabled"]):
                    raise ValueError("未知工具或非法启停状态")
            return data["tools"]
        except (OSError, ValueError, APIError) as exc:
            raise APIError(503, "TOOL_REGISTRY_UNAVAILABLE", "工具状态文件缺失或无效；请维护者检查，未默认启用工具") from exc

    def require_enabled(self, tool_id):
        self.definition(tool_id)
        state = self.states().get(tool_id, {"enabled": True, "removed": False})
        if state["removed"]:
            raise APIError(410, "TOOL_REMOVED", "工具已移出目录；已有任务和结果仍可按 ID 查询")
        if not state["enabled"]:
            raise APIError(503, "TOOL_DISABLED", "工具已停用，拒绝新任务；请刷新目录或联系维护者")

    def validate_metadata(self, tool_id, metadata):
        return self.definition(tool_id).metadata_validator(metadata)

    def catalog(self, mode, availability=None):
        from .openapi import specification
        base_result = standalone_schema("Result", specification())
        states, items = self.states(), []
        for tool_id, tool in self._tools.items():
            state = states.get(tool_id, {"enabled": True, "removed": False})
            if state["removed"]:
                continue
            runtime = (availability or {}).get(tool_id, "ready" if mode == "mock" else "unavailable")
            result_schema = deepcopy(base_result)
            result_schema["properties"]["capability_id"] = {"const": tool_id}
            result_schema["$defs"]["Artifact"]["properties"]["role"] = {"enum": tool.capability["outputs"]}
            items.append({**deepcopy(tool.capability), "input": deepcopy(tool.input_contract),
                "metadata_schema": deepcopy(tool.metadata_schema), "mcp_name": tool.mcp_name,
                "result_schema": result_schema,
                "enabled": state["enabled"], "state": "enabled" if state["enabled"] else "disabled",
                "execution_mode": mode, "availability": runtime if state["enabled"] else "unavailable",
                "quality_status": "synthetic_demo" if mode == "mock" else "unverified_candidate"})
        revision = sha256(canonical({"capabilities": items, "states": states}).encode("utf-8"))
        return {"api_version": "1.0.0", "execution_mode": mode, "capabilities": items,
                "registry_revision": revision, "fallback_owner": "application_agent", "implicit_model_fallback": False}

    def contract(self, tool_id, mode, availability=None, catalog=None):
        from .openapi import specification
        catalog = catalog if catalog is not None else self.catalog(mode, availability)
        capability = next((item for item in catalog["capabilities"] if item["id"] == tool_id), None)
        if capability is None:
            raise APIError(404, "TOOL_NOT_FOUND", "工具未注册或已移出目录")
        schemas = specification()
        return {"tool_id": tool_id, "mcp_name": capability["mcp_name"], "capability": deepcopy(capability),
                "metadata_schema": deepcopy(capability["metadata_schema"]),
                "job_schema": standalone_schema("Job", schemas), "result_schema": deepcopy(capability["result_schema"]),
                "registry_revision": catalog["registry_revision"],
                "file_transport": "通过模型 API 上传 TIFF，再使用 input_id 提交；v1 不接受任意服务器路径或 URL"}

    def list_contracts(self, mode, availability=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise APIError(400, "INVALID_PAGINATION", "limit 须为 1～50 的整数，offset 须为非负整数")
        catalog = self.catalog(mode, availability)
        ids = [item["id"] for item in catalog["capabilities"]]
        selected = ids[offset:offset + limit]
        more = offset + len(selected) < len(ids)
        return {"api_version": "1.0.0", "registry_revision": catalog["registry_revision"],
                "tools": [self.contract(i, mode, availability, catalog) for i in selected],
                "total": len(ids), "limit": limit, "offset": offset, "has_more": more,
                "next_offset": offset + len(selected) if more else None}

    def initialize(self):
        if self.state_file is None:
            raise ValueError("需要 --registry-file 指定状态文件")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical({"version": 1, "tools": {}}))

    def change(self, tool_id, action):
        self.definition(tool_id)
        if self.state_file is None or action not in {"enable", "disable", "remove", "restore"}:
            raise ValueError("需要已初始化的状态文件和合法操作")
        lock = self.state_file.with_name(self.state_file.name + ".lock")
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary = None
        try:
            os.close(descriptor)
            states = self.states()
            current = states.get(tool_id, {"enabled": True, "removed": False})
            if current["removed"] and action == "enable":
                raise ValueError("已移出的工具须先 restore，再 enable")
            value = {"enabled": action == "enable", "removed": action == "remove" or current["removed"] and action == "disable"}
            states[tool_id] = value
            descriptor, temporary = tempfile.mkstemp(prefix=".tool-registry-", dir=self.state_file.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(canonical({"version": 1, "tools": states}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
            return {"tool_id": tool_id, **value}
        finally:
            if temporary is not None and Path(temporary).exists():
                Path(temporary).unlink()
            lock.unlink()
