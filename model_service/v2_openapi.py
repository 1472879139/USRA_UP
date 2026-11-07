"""v2 外层 HTTP 契约；逐工具输入、参数和输出以工具目录为准。"""
from .v2_contract import FILE_REFERENCE, ID, job_schema, object_schema


def specification():
    response = lambda name: {"description": name, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + name}}}}
    def operation(name, result, body=None, status="200", parameters=None):
        value = {"summary": name, "responses": {status: response(result), "default": response("Error")}}
        if body:
            value["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + body}}}}
        if parameters:
            value["parameters"] = parameters
        return value
    path_id = lambda name: {"name": name, "in": "path", "required": True, "schema": ID if name != "tool_id" else {"type": "string"}}
    paths = {
        "/v2/tools": {"get": operation("分页查询命名输入槽和独立 JSON Schema", "Catalog", parameters=[
            {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
            {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}}])},
        "/v2/tools/{tool_id}": {"get": operation("查询单工具完整契约", "ToolContract", parameters=[path_id("tool_id")])},
        "/v2/files": {"post": operation("上传原始文件字节，媒体类型须被已启用工具声明", "File", status="201")},
        "/v2/jobs": {"post": operation("按独立契约显式提交异步任务", "Job", "JobRequest", "202",
            [{"name": "Idempotency-Key", "in": "header", "required": True,
              "schema": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{16,128}$"}}])},
        "/v2/jobs/{job_id}": {"get": operation("查询本身份 v2 任务", "Job", parameters=[path_id("job_id")])},
        "/v2/jobs/{job_id}/cancel": {"post": operation("取消本身份 v2 任务", "Job", "Empty", parameters=[path_id("job_id")])},
        "/v2/artifacts/{artifact_id}": {"get": {"summary": "认证下载本身份文件；核对大小和 SHA256",
            "parameters": [path_id("artifact_id")], "responses": {"200": {"description": "文件原始字节",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}, "default": response("Error")}}},
        "/v2/openapi.json": {"get": operation("查询 v2 HTTP 契约", "OpenAPI")},
    }
    paths["/v2/files"]["post"]["requestBody"] = {"required": True, "content": {
        media: {"schema": {"type": "string", "format": "binary", "maxLength": 16777216}}
        for media in ["image/tiff", "text/csv", "application/json", "application/octet-stream"]}}
    paths["/v2/jobs"]["post"]["responses"]["200"] = response("Job")
    schemas = {
        "JobRequest": object_schema({"tool_id": {"type": "string"}, "inputs": {"type": "object"}, "parameters": {"type": "object"}},
                                    ["tool_id", "inputs", "parameters"]),
        "Job": job_schema(),
        "FileReference": FILE_REFERENCE,
        "File": {"type": "object", "required": ["file_id", "sha256", "size_bytes", "media_type"],
                 "properties": {"file_id": ID, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                "size_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216}, "media_type": {"type": "string"}}},
        "Catalog": {"type": "object", "required": ["tools", "registry_revision", "has_more", "next_offset", "total"],
            "properties": {"tools": {"type": "array", "items": {"$ref": "#/components/schemas/ToolContract"}},
                           "registry_revision": {"type": "string"}, "total": {"type": "integer", "minimum": 0},
                           "has_more": {"type": "boolean"}, "next_offset": {"type": ["integer", "null"]}}},
        "ToolContract": {"type": "object", "required": ["tool_id", "tool_version", "input_slots", "input_schema", "parameters_schema", "output_schema"],
                        "properties": {name: {"type": "object"} for name in ["input_slots", "input_schema", "parameters_schema", "output_schema"]}},
        "Empty": object_schema(), "OpenAPI": {"type": "object"},
        "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "object", "required": ["code", "message", "retryable"]}}},
    }
    return {"openapi": "3.1.0", "info": {"title": "USRA 异构工具 API", "version": "2.0.0"},
            "security": [{"BearerAuth": []}], "paths": paths,
            "components": {"schemas": schemas, "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}}}
