"""业务适配层：复用模型API的身份、队列与任务，不导入模型运行时。"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
SUBMISSION_KEY = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{16,128}$",
                  "description": "调用前生成并保存；同一提交超时后复用同一键和正文，避免重复GPU执行"}
MODEL_NAMES = {"da-v2-field-delineation": "ursa_delineate_fields",
               "terramind-small-landcover": "ursa_classify_landcover"}


class ServiceError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def public(self):
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


@dataclass(frozen=True)
class Identity:
    owner: str
    api_key: str = field(repr=False)
    scopes: tuple = ("read", "submit", "cancel")


def valid_url(value, public=False):
    """远程地址要求HTTPS；回环HTTP仅用于服务间通信或本机测试。"""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c in value for c in "\r\n")):
        raise ValueError("服务地址无效")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("非本机服务必须使用HTTPS")
    return value.rstrip("/")


def load_config(path):
    source = Path(path).resolve()
    config = json.loads(source.read_text(encoding="utf-8-sig"))
    for key in ["api_base_url", "public_api_base_url", "public_mcp_url"]:
        config[key] = valid_url(config[key])
    private = Path(config["identities_file"])
    config["identities_file"] = str(private if private.is_absolute() else source.parent / private)
    config.setdefault("requests_per_minute", 120)
    if not isinstance(config["requests_per_minute"], int) or not 1 <= config["requests_per_minute"] <= 10000:
        raise ValueError("请求频率配置无效")
    return config


class IdentityStore:
    """每次请求重新读取凭据，撤销后不依赖会话或重启生效。"""
    def __init__(self, path):
        self.path = Path(path)

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return None
        records = json.loads(self.path.read_text(encoding="utf-8-sig"))["identities"]
        owners, hashes, api_keys = set(), set(), set()
        for item in records:
            if (not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item["owner"])
                    or not re.fullmatch(r"[0-9a-f]{64}", item["token_sha256"])
                    or not isinstance(item["api_key"], str) or not item["api_key"]
                    or any(c in item["api_key"] for c in "\r\n")
                    or not set(item.get("scopes", ["read"])) <= {"read", "submit", "cancel"}
                    or item["owner"] in owners or item["token_sha256"] in hashes or item["api_key"] in api_keys):
                raise ValueError("身份记录重复或无效")
            owners.add(item["owner"])
            hashes.add(item["token_sha256"])
            api_keys.add(item["api_key"])
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for item in records:
            if hmac.compare_digest(item["token_sha256"], digest):
                if not item.get("enabled", True):
                    return None
                return Identity(item["owner"], item["api_key"], tuple(item.get("scopes", ["read"])))
        return None


class ModelAPI:
    def __init__(self, base_url, transport=None):
        self.base_url = valid_url(base_url)
        self.transport = transport

    async def request(self, identity, method, path, body=None, key=None):
        headers = {"Authorization": "Bearer " + identity.api_key}
        if key:
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                                         transport=self.transport) as client:
                async with client.stream(method, self.base_url + path, headers=headers, json=body) as response:
                    if 300 <= response.status_code < 400:
                        raise ServiceError("UPSTREAM_REDIRECT", "模型API发生重定向；未转发认证信息，请维护者检查地址")
                    blocks, length = [], 0
                    async for block in response.aiter_bytes():
                        length += len(block)
                        if length > 2 * 1024 ** 2:
                            raise ServiceError("UPSTREAM_LIMIT", "模型API响应超过接口限制")
                        blocks.append(block)
            value = json.loads(b"".join(blocks))
            if not response.is_success:
                error = value.get("error", {})
                message = str(error.get("message", "模型API拒绝请求"))[:600].replace(identity.api_key, "[已隐藏]")
                raise ServiceError(str(error.get("code", "UPSTREAM_ERROR")), message,
                                   bool(error.get("retryable", response.status_code >= 500)))
            if not isinstance(value, dict):
                raise ValueError("响应必须是对象")
            return value
        except httpx.HTTPError:
            raise ServiceError("UPSTREAM_UNAVAILABLE", "模型API连接失败；查询可重试，提交须保留原提交键", True) from None
        except (ValueError, TypeError):
            raise ServiceError("UPSTREAM_INVALID", "模型API响应不符合JSON接口约定") from None


def object_schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def contract_schema(name, spec):
    """仅复制被引用的定义，保留API的输出约束，不手写第二套结果字段。"""
    schemas, seen = spec["components"]["schemas"], {}

    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(x) for x in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "$ref":
                if not item.startswith("#/components/schemas/"):
                    raise ServiceError("CONTRACT_INVALID", "模型API包含不支持的外部契约引用")
                ref = item.split("/")[-1]
                if ref not in seen:
                    seen[ref] = {}
                    seen[ref] = rewrite(deepcopy(schemas[ref]))
                result[key] = "#/$defs/" + ref
            else:
                result[key] = rewrite(item)
        return result

    root = rewrite(deepcopy(schemas[name]))
    if seen:
        root["$defs"] = seen
    Draft202012Validator.check_schema(root)
    return root


def map_schema(value, visit=None, transform_ref=None):
    """仅遍历 Schema 位置，避免把 const 等普通数据误当成引用。"""
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


def checked_schema(value):
    """逐工具契约只允许本地引用，校验过程不访问外部 Schema 地址。"""
    root = deepcopy(value)
    if not isinstance(root, dict):
        raise ServiceError("CONTRACT_INVALID", "逐工具 Schema 须为 JSON 对象")

    def inspect(item):
        for key, child in item.items():
            if key == "$id" or key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                raise ServiceError("CONTRACT_INVALID", "逐工具 Schema 不允许外部引用或重新设置解析地址")
            if key in {"$ref", "$dynamicRef"} and child != "#":
                if not child.startswith("#/"):
                    raise ServiceError("CONTRACT_INVALID", "逐工具 Schema 引用须为本地 JSON Pointer")
                target = root
                try:
                    for part in child[2:].split("/"):
                        part = part.replace("~1", "/").replace("~0", "~")
                        target = target[int(part)] if isinstance(target, list) else target[part]
                except (KeyError, TypeError, IndexError, ValueError):
                    raise ServiceError("CONTRACT_INVALID", "逐工具 Schema 引用的本地定义不存在") from None

    map_schema(root, visit=inspect)
    try:
        Draft202012Validator.check_schema(root)
    except SchemaError:
        raise ServiceError("CONTRACT_INVALID", "逐工具 JSON Schema 格式无效，请维护者核验") from None
    return root


def schema_is_valid(value, contract):
    try:
        return Draft202012Validator(contract, registry=Registry()).is_valid(value)
    except Exception:
        raise ServiceError("CONTRACT_INVALID", "工具 Schema 无法在本地解析，请维护者核验引用") from None


class ToolService:
    def __init__(self, config, backend=None):
        self.config = config
        self.backend = backend or ModelAPI(config["api_base_url"])
        if type(config.get("enable_v2", False)) is not bool:
            raise ValueError("enable_v2 须为布尔值")
        self.v2 = None
        if config.get("enable_v2", False):
            from .v2 import V2Tools
            self.v2 = V2Tools(self.backend, config)

    async def catalog(self, identity):
        return await self.backend.request(identity, "GET", "/v1/capabilities")

    async def definitions(self, identity):
        catalog = await self.catalog(identity)
        spec = await self.backend.request(identity, "GET", "/openapi.json")
        job_schema = contract_schema("Job", spec)
        definitions = []

        def add(name, title, description, inputs, output=None, operation="read", capability=None):
            if operation not in identity.scopes:
                return
            definitions.append({"name": name, "title": title, "description": description,
                                "inputSchema": inputs, "outputSchema": output or {"type": "object"},
                                "annotations": {"readOnlyHint": operation == "read", "destructiveHint": operation == "cancel",
                                                "idempotentHint": True, "openWorldHint": False},
                                "operation": operation, "capability": capability})

        add("ursa_get_service_info", "查询模型服务", "查询API执行模式、上传入口与当前工具目录。影像通过上传API传输，不把影像字节写进MCP参数。", object_schema())
        add("ursa_get_tool_contract", "查询工具契约", "获取指定能力的输入JSON Schema、波段/网格/编码要求、预期结果Schema与产物角色；提交前先核对。",
            object_schema({"tool_id": {"type": "string", **({"enum": [c["id"] for c in catalog["capabilities"]]}
                if catalog["capabilities"] else {"not": {}})}}, ["tool_id"]))
        add("ursa_get_job", "查询本身份任务", "按job_id查询本身份任务；queued/running/cancelling未完成，succeeded/failed/cancelled为终态。断线不自动取消任务。",
            object_schema({"job_id": ID}, ["job_id"]), job_schema)
        add("ursa_cancel_job", "取消本身份任务", "取消本身份排队或运行任务；仍需查询至终态。不会删除已完成产物。",
            object_schema({"job_id": ID}, ["job_id"]), job_schema, operation="cancel")
        add("ursa_get_artifacts", "查询本任务产物", "查询成功任务的文件清单和认证下载地址；下载后核对size_bytes和sha256，密钥通过请求头单独提供。",
            object_schema({"job_id": ID}, ["job_id"]))
        for capability in catalog["capabilities"]:
            if capability.get("availability") == "unavailable" or capability.get("enabled") is False:
                continue
            name = capability.get("mcp_name", MODEL_NAMES.get(capability["id"], "ursa_run_" + capability["id"].replace("-", "_")))
            if not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", name):
                raise ServiceError("CONTRACT_INVALID", "能力ID不能映射为合法MCP工具名")
            metadata_schema = checked_schema(capability["metadata_schema"]) if "metadata_schema" in capability else contract_schema("Metadata", spec)
            output = deepcopy(job_schema)
            if "result_schema" in capability:
                result_schema = checked_schema(capability["result_schema"])
                output.setdefault("$defs", {}).update(result_schema.pop("$defs", {}))
                output["properties"]["result"] = {"anyOf": [result_schema, {"type": "null"}]}
                Draft202012Validator.check_schema(output)
            inputs = object_schema({"input_id": ID, "metadata": metadata_schema,
                                    "idempotency_key": SUBMISSION_KEY}, ["input_id", "metadata", "idempotency_key"])
            add(name, capability.get("display_name", capability["id"]),
                "异步提交" + capability.get("display_name", capability["id"]) + "；会占用GPU。使用本身份已上传input_id和真实元数据。返回job_id后用ursa_get_job查询。结果是未经独立精度评价的候选结果，不自动换模型。",
                inputs, output, operation="submit", capability=capability["id"])
        if self.v2 is not None:
            definitions.extend(await self.v2.definitions(identity))
        if len({d["name"] for d in definitions}) != len(definitions):
            raise ServiceError("CONTRACT_INVALID", "能力目录包含重复MCP工具名")
        return definitions

    async def call(self, identity, name, arguments):
        definitions = {d["name"]: d for d in await self.definitions(identity)}
        definition = definitions.get(name)
        if definition is None:
            raise ServiceError("TOOL_UNAVAILABLE", "工具不存在、已停用或本身份无权调用；请刷新工具目录")
        if not schema_is_valid(arguments, definition["inputSchema"]):
            raise ServiceError("INVALID_ARGUMENTS", "参数不符合该工具inputSchema；请查询工具契约，不要传入服务器路径或任意URL")
        if name.startswith("ursa_v2_") and self.v2 is not None:
            result = await self.v2.call(identity, name, arguments, definition)
        elif definition["capability"]:
            result = await self.backend.request(identity, "POST", "/v1/jobs",
                {"capability_id": definition["capability"], "input_id": arguments["input_id"], "metadata": arguments["metadata"]}, arguments["idempotency_key"])
        elif name == "ursa_get_service_info":
            catalog = await self.catalog(identity)
            result = {**catalog, "health": await self.backend.request(identity, "GET", "/health"),
                      "upload": {"url": self.config["public_api_base_url"] + "/v1/inputs", "method": "POST",
                                 "content_type": "image/tiff", "max_bytes": 16777216,
                                 "authentication": "使用单独交付的同身份模型API密钥；MCP令牌不透传给模型API"}}
        elif name == "ursa_get_tool_contract":
            catalog = await self.catalog(identity)
            cap = next(c for c in catalog["capabilities"] if c["id"] == arguments["tool_id"])
            spec = await self.backend.request(identity, "GET", "/openapi.json")
            result = {"tool_id": cap["id"], "capability": cap, "mcp_name": cap.get("mcp_name", MODEL_NAMES.get(cap["id"])),
                      "metadata_schema": checked_schema(cap["metadata_schema"]) if "metadata_schema" in cap else contract_schema("Metadata", spec),
                      "job_schema": contract_schema("Job", spec),
                      "result_schema": checked_schema(cap["result_schema"]) if "result_schema" in cap else contract_schema("Result", spec),
                      "file_transport": "先通过模型API上传TIFF，返回input_id后提交MCP任务；文件不内嵌MCP消息"}
            if "registry_revision" in catalog:
                result["registry_revision"] = catalog["registry_revision"]
        elif name == "ursa_cancel_job":
            result = await self.backend.request(identity, "POST", "/v1/jobs/" + arguments["job_id"] + "/cancel", {})
        else:
            result = await self.backend.request(identity, "GET", "/v1/jobs/" + arguments["job_id"])
            if name == "ursa_get_artifacts":
                if result["status"] != "succeeded":
                    raise ServiceError("JOB_NOT_SUCCEEDED", "任务尚未成功；请先查询状态和错误")
                artifacts = []
                for artifact in result["result"]["artifacts"]:
                    if not re.fullmatch(r"/v1/artifacts/[0-9a-f]{32}", artifact["download_path"]):
                        raise ServiceError("ARTIFACT_INVALID", "产物下载路径不符合模型API契约")
                    artifacts.append({**artifact, "download_url": self.config["public_api_base_url"] + artifact["download_path"]})
                result = {"job_id": result["job_id"], "execution_mode": result["result"]["execution_mode"],
                          "quality_status": result["result"]["quality_status"], "accuracy_evaluated": result["result"]["accuracy_evaluated"],
                          "artifacts": artifacts}
        if identity.api_key in json.dumps(result, ensure_ascii=False):
            raise ServiceError("RESULT_CONTAINS_SECRET", "模型API响应意外包含认证材料，已阻止返回")
        if not schema_is_valid(result, definition["outputSchema"]):
            raise ServiceError("RESULT_INVALID", "模型API结果不符合已发布的输出契约，未作为有效结果交付")
        return result
