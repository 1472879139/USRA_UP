"""可选 v2 MCP 适配；只访问已配置 API，不导入模型运行环境。"""
import re

from .service import ID, SUBMISSION_KEY, ServiceError, checked_schema, map_schema, object_schema


def mounted_schema(value, pointer):
    return map_schema(checked_schema(value), transform_ref=lambda child: "#/" + pointer + child[1:])


class V2Tools:
    def __init__(self, backend, config):
        self.backend, self.config = backend, config

    async def catalog(self, identity):
        tools, revision, offset = [], None, 0
        for _ in range(13):
            page = await self.backend.request(identity, "GET", f"/v2/tools?limit=10&offset={offset}")
            if revision is not None and revision != page["registry_revision"]:
                raise ServiceError("CATALOG_CHANGED", "翻页期间 v2 目录变更，请重新发现工具", True)
            revision = page["registry_revision"]
            tools.extend(page["tools"])
            if (len(tools) > 128 or len({t["tool_id"] for t in tools}) != len(tools)
                    or any(not isinstance(t["tool_id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", t["tool_id"]) for t in tools)):
                raise ServiceError("CONTRACT_INVALID", "v2 工具数量超限或 ID 重复")
            if not page["has_more"]:
                return tools
            next_offset = page["next_offset"]
            if type(next_offset) is not int or next_offset <= offset:
                raise ServiceError("CONTRACT_INVALID", "v2 分页游标无效")
            offset = next_offset
        raise ServiceError("CONTRACT_INVALID", "v2 目录超出分页上限")

    async def definitions(self, identity):
        tools = await self.catalog(identity)
        spec = await self.backend.request(identity, "GET", "/v2/openapi.json")
        job = checked_schema(spec["components"]["schemas"]["Job"])
        definitions = []

        def add(name, title, description, inputs, output, operation="read", tool_id=None):
            if operation in identity.scopes:
                definitions.append({"name": name, "title": title, "description": description,
                    "inputSchema": checked_schema(inputs), "outputSchema": checked_schema(output),
                    "operation": operation, "capability": tool_id,
                    "annotations": {"readOnlyHint": operation == "read", "destructiveHint": operation == "cancel",
                                    "idempotentHint": True, "openWorldHint": False}})

        ids = [t["tool_id"] for t in tools]
        query_id = {"type": "string", **({"enum": ids} if ids else {"not": {}})}
        add("ursa_v2_get_tool_contract", "查询 v2 工具契约", "查询命名输入槽、媒体类型、参数和独立 JSON 输出契约。",
            object_schema({"tool_id": query_id}, ["tool_id"]), {"type": "object"})
        add("ursa_v2_get_job", "查询本身份 v2 任务", "查询 v2 任务状态、工具版本、outputs 和产物；与 v1 分开存储。",
            object_schema({"job_id": ID}, ["job_id"]), job)
        add("ursa_v2_cancel_job", "取消本身份 v2 任务", "请求取消 v2 任务；继续查询至终态。",
            object_schema({"job_id": ID}, ["job_id"]), job, "cancel")
        add("ursa_v2_get_artifacts", "查询本身份 v2 产物", "成功后获取认证下载清单；核对大小与 SHA256。",
            object_schema({"job_id": ID}, ["job_id"]), {"type": "object"})
        for tool in tools:
            if not tool["enabled"] or tool["availability"] not in {"ready", "configured"}:
                continue
            if not re.fullmatch(r"ursa_v2_[a-zA-Z0-9_]{1,56}", tool["mcp_name"]):
                raise ServiceError("CONTRACT_INVALID", "v2 MCP 名称无效")
            inputs = object_schema({"inputs": mounted_schema(tool["input_schema"], "properties/inputs"),
                                    "parameters": mounted_schema(tool["parameters_schema"], "properties/parameters"),
                                    "idempotency_key": SUBMISSION_KEY}, ["inputs", "parameters", "idempotency_key"])
            add(tool["mcp_name"], tool["title"], tool["description"] +
                "；异步提交后查询 ursa_v2_get_job。文件先经 /v2/files 上传，以 file_id 填入命名槽；重试复用原提交键。",
                inputs, tool["job_schema"], "submit", tool["tool_id"])
        return definitions

    async def call(self, identity, name, arguments, definition):
        if definition["capability"] is not None:
            return await self.backend.request(identity, "POST", "/v2/jobs",
                {"tool_id": definition["capability"], "inputs": arguments["inputs"], "parameters": arguments["parameters"]},
                arguments["idempotency_key"])
        if name == "ursa_v2_get_tool_contract":
            return await self.backend.request(identity, "GET", "/v2/tools/" + arguments["tool_id"])
        path = "/v2/jobs/" + arguments["job_id"]
        if name == "ursa_v2_cancel_job":
            return await self.backend.request(identity, "POST", path + "/cancel", {})
        result = await self.backend.request(identity, "GET", path)
        if name == "ursa_v2_get_artifacts":
            if result["status"] != "succeeded":
                raise ServiceError("JOB_NOT_SUCCEEDED", "v2 任务尚未成功，请先查询状态")
            artifacts = []
            for artifact in result["result"]["artifacts"]:
                if not re.fullmatch(r"/v2/artifacts/[0-9a-f]{32}", artifact["download_path"]):
                    raise ServiceError("ARTIFACT_INVALID", "v2 产物路径不合法")
                artifacts.append({**artifact, "download_url": self.config["public_api_base_url"] + artifact["download_path"]})
            return {"job_id": result["job_id"], "tool_id": result["tool_id"],
                    "execution_mode": result["result"]["execution_mode"], "artifacts": artifacts}
        return result
