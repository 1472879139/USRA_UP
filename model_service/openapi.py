"""可导出的OpenAPI契约；与服务器使用相同能力ID和限制。"""
from .contract import API_VERSION, BANDS, MAX_INPUT_BYTES


def specification():
    ref = lambda name: {"$ref": "#/components/schemas/" + name}
    response = lambda name: {"description": name, "content": {"application/json": {"schema": ref(name)}}}
    def operation(summary, result, status="200", body=None, parameters=None):
        value = {"summary": summary, "responses": {status: response(result), "default": response("Error")}}
        if body:
            value["requestBody"] = {"required": True, "content": {"application/json": {"schema": ref(body)}}}
        if parameters:
            value["parameters"] = parameters
        return value
    job_id = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}
    artifact_id = {**job_id, "name": "artifact_id"}
    upload = operation("上传TIFF，返回不可变输入ID；像元级预检在任务中执行", "Input", "201")
    upload["requestBody"] = {"required": True, "content": {"image/tiff": {"schema": {"type": "string", "format": "binary", "maxLength": MAX_INPUT_BYTES}}}}
    paths = {
        "/health": {"get": {**operation("服务存活与API版本；不证明模型精度或CUDA当前可用", "Health"), "security": []}},
        "/v1/capabilities": {"get": operation("查询能力、输入条件、模型版本与显式模拟标志", "Catalog")},
        "/v1/inputs": {"post": upload},
        "/v1/jobs": {"post": operation("显式提交一个模型任务；幂等重试不会重复运行GPU", "Job", "202", "JobRequest",
            [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{16,128}$"}}])},
        "/v1/jobs/{job_id}": {"get": operation("查询本身份任务；重启后仍可查询", "Job", parameters=[job_id])},
        "/v1/jobs/{job_id}/cancel": {"post": operation("取消排队或运行任务；终态保持原样", "Job", body="EmptyObject", parameters=[job_id])},
        "/v1/artifacts/{artifact_id}": {"get": {"summary": "下载本身份产物；服务器与SDK均核对SHA256", "parameters": [artifact_id],
            "responses": {"200": {"description": "产物原始字节", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}, "default": response("Error")}}},
        "/openapi.json": {"get": operation("查询本接口契约", "OpenAPI")},
    }
    paths["/v1/jobs"]["post"]["responses"]["200"] = response("Job")
    paths["/health"]["get"]["responses"]["503"] = response("Health")
    metadata = {"type": "object", "additionalProperties": False, "required": ["sensor", "product_level", "encoding", "bands"],
        "properties": {"sensor": {"const": "Sentinel-2"}, "product_level": {"const": "L2A"},
                       "encoding": {"enum": ["dn_10000", "reflectance_0_1"]},
                       "bands": {"type": "array", "minItems": 12, "maxItems": 12, "uniqueItems": True, "items": {"enum": BANDS}}}}
    schemas = {
        "Metadata": metadata,
        "JobRequest": {"type": "object", "additionalProperties": False, "required": ["capability_id", "input_id", "metadata"],
            "properties": {"capability_id": {"enum": ["da-v2-field-delineation", "terramind-small-landcover"]},
                           "input_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}, "metadata": ref("Metadata")}},
        "Job": {"type": "object", "required": ["job_id", "status", "capability_id", "input_id", "error", "result"],
            "properties": {"job_id": {"type": "string"}, "capability_id": {"type": "string"}, "input_id": {"type": "string"},
                           "status": {"enum": ["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]},
                           "created_at": {"type": "string"}, "started_at": {"type": ["string", "null"]}, "finished_at": {"type": ["string", "null"]},
                           "error": {"type": ["object", "null"]}, "result": {"anyOf": [ref("Result"), {"type": "null"}]}}},
        "Input": {"type": "object", "required": ["input_id", "sha256", "size_bytes", "created_at"], "properties": {
            "input_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_INPUT_BYTES}, "created_at": {"type": "string", "format": "date-time"}}},
        "Catalog": {"type": "object", "required": ["api_version", "execution_mode", "capabilities", "implicit_model_fallback"],
            "properties": {"api_version": {"const": API_VERSION}, "execution_mode": {"enum": ["gpu", "mock"]},
                "implicit_model_fallback": {"const": False}, "capabilities": {"type": "array", "items": ref("Capability")}}},
        "Capability": {"type": "object", "required": ["id", "task", "tier", "model", "model_revision", "input", "outputs", "availability", "classes"],
            "properties": {"id": {"type": "string"}, "task": {"type": "string"}, "tier": {"enum": ["specialist", "general"]},
                "model": {"type": "string"}, "model_revision": {"type": "string"}, "input": {"type": "object"},
                "outputs": {"type": "array", "items": {"type": "string"}}, "classes": {"type": "object", "additionalProperties": {"type": "string"}},
                "availability": {"enum": ["ready", "configured", "unavailable"]}}},
        "Artifact": {"type": "object", "required": ["artifact_id", "name", "sha256", "size_bytes", "media_type", "role", "download_path"],
            "properties": {"artifact_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}, "name": {"type": "string", "pattern": "^[a-zA-Z0-9_.-]{1,96}$"},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "size_bytes": {"type": "integer", "minimum": 0},
                "media_type": {"type": "string"}, "role": {"enum": ["classification", "field_boundaries", "statistics", "preview", "provenance"]},
                "download_path": {"type": "string", "pattern": "^/v1/artifacts/[0-9a-f]{32}$"}}},
        "Statistic": {"type": "object", "required": ["code", "name", "pixels", "area_m2", "fraction"],
            "properties": {"code": {"type": "integer"}, "name": {"type": "string"}, "pixels": {"type": "integer", "minimum": 0},
                "area_m2": {"type": "number", "minimum": 0}, "fraction": {"type": "number", "minimum": 0, "maximum": 1}}},
        "Result": {"type": "object", "required": ["execution_mode", "quality_status", "accuracy_evaluated", "capability_id", "model", "model_revision",
                "input_sha256", "width", "height", "crs", "transform", "valid_pixels", "pixel_area_m2", "classes", "statistics", "model_run", "warnings", "artifacts"],
            "properties": {"execution_mode": {"enum": ["gpu", "mock"]}, "quality_status": {"enum": ["synthetic_demo", "unverified_candidate"]},
                "accuracy_evaluated": {"const": False}, "capability_id": {"type": "string"}, "model": {"type": "string"}, "model_revision": {"type": "string"},
                "input_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1},
                "crs": {"type": "string"}, "transform": {"type": "array", "minItems": 9, "maxItems": 9, "items": {"type": "number"}},
                "valid_pixels": {"type": "integer", "minimum": 1}, "pixel_area_m2": {"type": "number", "exclusiveMinimum": 0},
                "classes": {"type": "object", "additionalProperties": {"type": "string"}}, "statistics": {"type": "array", "items": ref("Statistic")},
                "model_run": {"type": "object"}, "warnings": {"type": "array", "items": {"type": "string"}}, "artifacts": {"type": "array", "items": ref("Artifact")}}},
        "Health": {"type": "object", "required": ["status", "api_version", "execution_mode"],
            "properties": {"status": {"enum": ["ready", "degraded"]}, "api_version": {"const": API_VERSION}, "execution_mode": {"enum": ["gpu", "mock"]}}},
        "Error": {"type": "object", "required": ["api_version", "error"], "properties": {"api_version": {"const": API_VERSION},
             "error": {"type": "object", "required": ["code", "message", "retryable", "request_id"],
                       "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "retryable": {"type": "boolean"}, "request_id": {"type": "string"}}}}},
        "EmptyObject": {"type": "object", "additionalProperties": False},
        "OpenAPI": {"type": "object"},
    }
    return {"openapi": "3.1.0", "info": {"title": "USRA模型服务与应用交接API", "version": API_VERSION,
            "description": "专用/通用模型统一契约。应用负责DeepSeek与兜底，服务不隐式换模型。默认无外部真值评价。"},
            "servers": [{"url": "http://127.0.0.1:18767", "description": "本机模拟/SSH转发；远程真实入口使用HTTPS"}],
            "security": [{"BearerAuth": []}], "paths": paths,
            "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": schemas}}
