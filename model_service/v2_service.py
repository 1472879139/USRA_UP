"""v2 准入与执行：同身份文件引用、契约快照和统一产物登记。"""
from copy import deepcopy
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from .backends import BackendError
from .contract import APIError, canonical, decode_json, sha256
from .v2_contract import AdapterResult, FileInput, ProducedArtifact, ToolContext, validate
from .v2_storage import V2Store


class V2Service:
    def __init__(self, root, registry, max_bytes, quota_root, max_seconds=600):
        if Path(root).is_symlink():
            raise ValueError("v2 数据目录不允许符号链接")
        registry.states()
        self.registry, self.max_seconds = registry, max_seconds
        self.store = V2Store(root, quota_root, max_bytes)

    def resolve_inputs(self, owner, payload, contract):
        inputs = deepcopy(payload["inputs"])
        for name, slot in contract["input_slots"].items():
            if slot["kind"] != "file" or name not in inputs:
                continue
            record = self.store.file(owner, inputs[name]["file_id"])
            if record["media_type"] not in slot["media_types"] or record["size"] > slot["max_bytes"]:
                raise APIError(400, "INPUT_UNSUPPORTED", "文件媒体类型或大小不符合命名输入槽要求")
            inputs[name] = FileInput(record["id"], self.store.input_path(record["id"]),
                                     record["sha"], record["size"], record["media_type"])
        return inputs

    def submit(self, owner, payload, idem):
        if set(payload) != {"tool_id", "inputs", "parameters"} or not isinstance(payload.get("tool_id"), str):
            raise APIError(400, "INVALID_REQUEST", "v2 提交须包含 tool_id、inputs 和 parameters")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", idem):
            raise APIError(400, "INVALID_IDEMPOTENCY_KEY", "需要 16～128 位 Idempotency-Key")
        previous = self.store.existing_submission(owner, payload, idem)
        if previous is not None:
            return previous, False
        adapter = self.registry.definition(payload["tool_id"])
        self.registry.require_enabled(payload["tool_id"])
        if adapter.availability() not in {"ready", "configured"}:
            raise APIError(503, "TOOL_UNAVAILABLE", "执行适配器未就绪，未自动切换工具")
        contract = self.registry.snapshot(payload["tool_id"])
        validate(payload["inputs"], contract["input_schema"])
        validate(payload["parameters"], contract["parameters_schema"])
        self.resolve_inputs(owner, payload, contract)
        return self.store.submit(owner, payload, idem, contract)

    @staticmethod
    def check_files(inputs, context):
        for value in inputs.values():
            context.check_active()
            if isinstance(value, FileInput):
                if (value.path.is_symlink() or not value.path.is_file()
                        or value.path.stat().st_size != value.size_bytes
                        or sha256(value.path.read_bytes()) != value.sha256):
                    raise BackendError("INPUT_CHANGED", "上传文件在提交后发生变化")

    def execute(self, task, folder, cancelled, stopping):
        snapshot = task["snapshot"]
        try:
            adapter = self.registry.definition(task["capability_id"])
        except APIError as exc:
            raise BackendError("TOOL_UNAVAILABLE", "已接收任务的适配器不再安装，未自动换用其他工具") from exc
        if self.registry.snapshot(task["capability_id"])["contract_revision"] != snapshot["contract_revision"]:
            raise BackendError("TOOL_CONTRACT_CHANGED", "工具版本或契约已变更，未用新语义执行旧任务")
        inputs = self.resolve_inputs(task["owner"], task["payload"], snapshot)
        context = ToolContext(task["id"], folder, folder / "output", cancelled, stopping, time.monotonic() + self.max_seconds)
        context.check_active()
        self.check_files(inputs, context)
        produced = adapter.execute(context, inputs, deepcopy(task["payload"]["parameters"]))
        context.check_active()
        self.check_files(inputs, context)
        if not isinstance(produced, AdapterResult) or not isinstance(produced.artifacts, list) or len(produced.artifacts) > 16:
            raise BackendError("RESULT_INVALID", "适配器返回的输出或产物结构无效")
        try:
            validate(produced.outputs, snapshot["output_schema"], "RESULT_INVALID")
        except APIError as exc:
            raise BackendError("RESULT_INVALID", "工具输出不符合提交时保存的 output_schema") from exc
        if len(canonical(produced.outputs).encode("utf-8")) > 1024 ** 2:
            raise BackendError("RESULT_INVALID", "JSON 输出超过 1MiB，请通过产物文件返回大数据")
        declared, names = [], set()
        for artifact in produced.artifacts:
            if (not isinstance(artifact, ProducedArtifact) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", artifact.name) or artifact.name in {".", ".."}
                    or artifact.name in names or artifact.media_type not in snapshot["artifact_types"].get(artifact.role, [])):
                raise BackendError("RESULT_INVALID", "产物文件名、角色或媒体类型不符合工具契约")
            names.add(artifact.name)
            path = context.results_dir / artifact.name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()) or path.stat().st_nlink != 1:
                raise BackendError("RESULT_INVALID", "产物缺失、越界或使用链接")
            size = path.stat().st_size
            if size > 64 * 1024 ** 2:
                raise BackendError("RESULT_INVALID", "产物超过 64MiB")
            declared.append({"name": artifact.name, "role": artifact.role, "media_type": artifact.media_type,
                             "size_bytes": size, "sha256": sha256(path.read_bytes())})
        return {"outputs": produced.outputs, "execution_mode": snapshot["execution_mode"],
                "tool_version": snapshot["tool_version"], "files": declared}

    def dispatch(self, handler, owner, service):
        route = urlsplit(handler.path)
        if handler.command == "GET" and route.path == "/v2/tools":
            query = parse_qs(route.query, keep_blank_values=True)
            if set(query) - {"limit", "offset"} or any(len(v) != 1 for v in query.values()):
                raise APIError(400, "INVALID_PAGINATION", "仅接受单一 limit 和 offset")
            try:
                limit, offset = int(query.get("limit", ["20"])[0]), int(query.get("offset", ["0"])[0])
            except ValueError as exc:
                raise APIError(400, "INVALID_PAGINATION", "分页参数须为整数") from exc
            handler.send(200, self.registry.list_contracts(limit=limit, offset=offset))
        elif handler.command == "GET" and re.fullmatch(r"/v2/tools/[a-z][a-z0-9_-]{0,63}", handler.path):
            handler.send(200, self.registry.contract(route.path.rsplit("/", 1)[-1]))
        elif handler.command == "POST" and handler.path == "/v2/files":
            media = handler.headers.get_content_type()
            tools = self.registry.catalog()["tools"]
            allowed = {m for tool in tools if tool["enabled"] for slot in tool["input_slots"].values()
                       if slot["kind"] == "file" for m in slot["media_types"]}
            if media not in allowed:
                raise APIError(415, "MEDIA_TYPE_UNSUPPORTED", "媒体类型未被已启用工具声明")
            content = handler.body(16 * 1024 ** 2)
            if media == "image/tiff" and content[:4] not in {b"II*\x00", b"MM\x00*"}:
                raise APIError(400, "INPUT_UNSUPPORTED", "TIFF 文件头不受支持")
            handler.send(201, self.store.add_file(owner, content, media))
        elif handler.command == "POST" and handler.path == "/v2/jobs":
            if handler.headers.get_content_type() != "application/json":
                raise APIError(415, "MEDIA_TYPE_UNSUPPORTED", "需要 application/json")
            payload = decode_json(handler.body(65536))
            job, created = self.submit(owner, payload, handler.headers.get("Idempotency-Key", ""))
            handler.send(202 if created else 200, job)
        elif handler.command == "GET" and re.fullmatch(r"/v2/jobs/[0-9a-f]{32}", handler.path):
            handler.send(200, self.store.public_job(owner, route.path.rsplit("/", 1)[-1]))
        elif handler.command == "POST" and re.fullmatch(r"/v2/jobs/[0-9a-f]{32}/cancel", handler.path):
            if handler.json_body():
                raise APIError(400, "INVALID_REQUEST", "取消正文须为 {}")
            handler.send(200, service.cancel(owner, route.path.split("/")[-2], v2=True))
        elif handler.command == "GET" and re.fullmatch(r"/v2/artifacts/[0-9a-f]{32}", handler.path):
            content, media, name = self.store.artifact(owner, route.path.rsplit("/", 1)[-1])
            handler.send(200, content, media, name)
        elif handler.command == "GET" and handler.path == "/v2/openapi.json":
            from .v2_openapi import specification
            handler.send(200, specification())
        else:
            raise APIError(404, "NOT_FOUND", "v2 接口不存在")
