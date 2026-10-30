"""只监听回环地址的认证任务API；公网入口由HTTPS中转承担。"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import uuid

from .contract import (API_VERSION, APIError, MAX_INPUT_BYTES, MAX_JSON_BYTES, canonical,
                       catalog, decode_json, validate_job, sha256)
from .storage import Store
from .backends import BackendError


class Service:
    def __init__(self, root, backend, keys, max_bytes=4 * 1024 ** 3):
        if not keys or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", k) or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in keys.items()):
            raise ValueError("需要身份到API密钥SHA256的映射，不接受空认证")
        self.keys = dict(keys)
        self.store = Store(root, max_bytes)
        self.backend = backend
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.active_id = None
        self.active_cancel = None
        self.worker = threading.Thread(target=self.work, name="single-gpu-worker", daemon=True)
        self.worker.start()

    def owner(self, header):
        if not isinstance(header, str) or not header.startswith("Bearer ") or len(header) > 512:
            raise APIError(401, "UNAUTHORIZED", "需要有效的Bearer API密钥")
        token = header[7:]
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for identity, expected in self.keys.items():
            if hmac.compare_digest(digest, expected):
                return identity
        raise APIError(401, "UNAUTHORIZED", "API密钥无效或已撤销")

    def cancel(self, owner, identity):
        job = self.store.cancel(owner, identity)
        with self.lock:
            if self.active_id == identity and self.active_cancel:
                self.active_cancel.set()
        return job

    def work(self):
        while not self.stop.is_set():
            task = self.store.claim()
            if not task:
                self.stop.wait(0.1)
                continue
            cancelled = threading.Event()
            with self.lock:
                self.active_id, self.active_cancel = task["id"], cancelled
                if self.store.public_job(task["owner"], task["id"])["status"] == "cancelling":
                    cancelled.set()
            folder = self.store.job_dir(task["id"])
            try:
                folder.mkdir()
                source = self.store.input_path(task["input_id"])
                if source.is_symlink() or sha256(source.read_bytes()) != task["input"]["sha"]:
                    raise BackendError("INPUT_CHANGED", "输入在提交后发生变化")
                result = self.backend.run(task, folder, source, cancelled)
                if sha256(source.read_bytes()) != task["input"]["sha"]:
                    raise BackendError("INPUT_CHANGED", "输入在执行中发生变化")
                if cancelled.is_set():
                    raise BackendError("JOB_CANCELLED", "任务已取消")
                if self.stop.is_set():
                    raise BackendError("SERVER_STOPPED", "服务停止中断运行，请显式提交新任务", True)
                self.store.succeed(task, result)
            except BackendError as exc:
                self.store.fail(task["id"], exc.code, exc.message, exc.retryable, cancelled=exc.code == "JOB_CANCELLED" and not self.stop.is_set())
            except Exception:
                self.store.fail(task["id"], "RESULT_CONTRACT_FAILED", "执行或产物核验失败，维护者可按任务ID检查记录")
            finally:
                with self.lock:
                    self.active_id = self.active_cancel = None

    def close(self):
        self.stop.set()
        with self.lock:
            if self.active_cancel:
                self.active_cancel.set()
        self.worker.join(timeout=8)
        if self.worker.is_alive():
            raise RuntimeError("任务工作进程未停止，未关闭存储")
        self.store.close()


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        if address[0] != "127.0.0.1":
            raise ValueError("模型API只监听127.0.0.1；通过认证HTTPS中转提供远程访问")
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def send(self, status, value, media_type="application/json", name=None):
        content = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", self.request_id)
        if name:
            self.send_header("Content-Disposition", 'attachment; filename="' + name + '"')
        self.end_headers()
        self.wfile.write(content)

    def body(self, maximum):
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            raise APIError(400, "INVALID_LENGTH", "只接受单一Content-Length，不接受分块上传")
        try:
            length = int(lengths[0])
        except ValueError as exc:
            raise APIError(400, "INVALID_LENGTH", "请求体长度错误") from exc
        if length < 1 or length > maximum:
            raise APIError(413, "SIZE_LIMIT", "请求体超过接口限制或为空")
        content = self.rfile.read(length)
        if len(content) != length:
            raise APIError(400, "INCOMPLETE_UPLOAD", "请求体未完整上传")
        return content

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "MEDIA_TYPE_UNSUPPORTED", "需要application/json请求")
        return decode_json(self.body(MAX_JSON_BYTES))

    def dispatch(self):
        self.request_id = uuid.uuid4().hex
        service = self.server.service
        try:
            if self.command == "GET" and self.path == "/health":
                ready = service.worker.is_alive() and not service.stop.is_set()
                self.send(200 if ready else 503, {"status": "ready" if ready else "degraded", "api_version": API_VERSION, "execution_mode": service.backend.mode})
                return
            owner = service.owner(self.headers.get("Authorization"))
            if self.command == "GET" and self.path == "/v1/capabilities":
                self.send(200, catalog(service.backend.mode, service.backend.availability()))
            elif self.command == "GET" and self.path == "/openapi.json":
                from .openapi import specification
                self.send(200, specification())
            elif self.command == "POST" and self.path == "/v1/inputs":
                if self.headers.get_content_type() != "image/tiff":
                    raise APIError(415, "MEDIA_TYPE_UNSUPPORTED", "首版输入类型必须是image/tiff")
                content = self.body(MAX_INPUT_BYTES)
                if content[:4] not in {b"II*\x00", b"MM\x00*"}:
                    raise APIError(400, "INPUT_UNSUPPORTED", "需要普通TIFF文件；首版不支持BigTIFF或其他格式")
                self.send(201, service.store.add_input(owner, content))
            elif self.command == "POST" and self.path == "/v1/jobs":
                payload = validate_job(self.json_body())
                if service.backend.availability().get(payload["capability_id"]) == "unavailable":
                    raise APIError(503, "MODEL_UNAVAILABLE", "该模型尚未配置，未自动切换其他模型")
                idem = self.headers.get("Idempotency-Key", "")
                if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", idem):
                    raise APIError(400, "INVALID_IDEMPOTENCY_KEY", "提交任务需要16～128位Idempotency-Key")
                job, created = service.store.submit(owner, payload, idem)
                self.send(202 if created else 200, job)
            elif self.command == "GET" and re.fullmatch(r"/v1/jobs/[0-9a-f]{32}", self.path):
                self.send(200, service.store.public_job(owner, self.path.rsplit("/", 1)[-1]))
            elif self.command == "POST" and re.fullmatch(r"/v1/jobs/[0-9a-f]{32}/cancel", self.path):
                if self.json_body():
                    raise APIError(400, "INVALID_REQUEST", "取消操作请求体须为{}")
                self.send(200, service.cancel(owner, self.path.split("/")[-2]))
            elif self.command == "GET" and re.fullmatch(r"/v1/artifacts/[0-9a-f]{32}", self.path):
                content, media_type, name = service.store.artifact(owner, self.path.rsplit("/", 1)[-1])
                self.send(200, content, media_type, name)
            else:
                raise APIError(404, "NOT_FOUND", "接口不存在")
        except APIError as exc:
            self.send(exc.status, {"api_version": API_VERSION, "error": {"code": exc.code, "message": exc.message,
                     "retryable": exc.retryable, "request_id": self.request_id}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            self.send(500, {"api_version": API_VERSION, "error": {"code": "INTERNAL_ERROR", "message": "服务异常，请按请求ID联系维护者",
                     "retryable": False, "request_id": self.request_id}})

    do_GET = dispatch
    do_POST = dispatch

    def log_message(self, format, *args):
        # 不记录请求头、API密钥、原始文件名或用户正文。
        pass
