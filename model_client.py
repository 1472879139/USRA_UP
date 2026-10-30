"""应用方的标准库SDK：不导入GIS、PyTorch或服务器内部实现。"""
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ModelAPIError(Exception):
    def __init__(self, status, code, message, retryable=False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


class ModelClient:
    def __init__(self, base_url, api_key, timeout=30, ssl_context=None):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API地址必须是无认证信息、查询串和片段的HTTP(S)地址")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("远程服务必须使用HTTPS；HTTP仅用于本机开发或SSH本机转发")
        if not isinstance(api_key, str) or not api_key or any(c in api_key for c in "\r\n"):
            raise ValueError("需要有效API密钥")
        self.base_url, self.api_key, self.timeout = base_url.rstrip("/"), api_key, timeout
        self.ssl_context = ssl_context
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl_context), NoRedirect())

    def request(self, method, path, body=None, media_type="application/json", headers=None, raw=False, maximum=64 * 1024 ** 2):
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n"):
            raise ValueError("无效接口路径")
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method,
                    headers={"Authorization": "Bearer " + self.api_key, "Content-Type": media_type, **(headers or {})})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise ModelAPIError(response.status, "RESPONSE_LIMIT", "服务返回超过客户端限制")
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(32768))["error"]
            except (ValueError, KeyError, TypeError):
                error = {"code": "HTTP_ERROR", "message": "服务返回HTTP错误", "retryable": exc.code in {429, 502, 503, 504}}
            raise ModelAPIError(exc.code, error["code"], error["message"], error.get("retryable", False)) from None
        except urllib.error.URLError:
            raise ModelAPIError(0, "CONNECTION_FAILED", "无法连接模型API，请检查服务、地址与证书", True) from None
        except (TimeoutError, ConnectionError):
            raise ModelAPIError(0, "CONNECTION_FAILED", "模型API连接中断或超时，可按任务ID继续查询", True) from None

    def health(self):
        return self.request("GET", "/health")

    def capabilities(self):
        return self.request("GET", "/v1/capabilities")

    def upload(self, path):
        path = Path(path)
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= 16 * 1024 ** 2:
            raise ValueError("输入必须是不超过16MiB的实际TIFF文件")
        return self.request("POST", "/v1/inputs", path.read_bytes(), media_type="image/tiff")

    def submit(self, capability_id, input_id, metadata, idempotency_key=None):
        return self.request("POST", "/v1/jobs", {"capability_id": capability_id, "input_id": input_id, "metadata": metadata},
                            headers={"Idempotency-Key": idempotency_key or uuid.uuid4().hex})

    def job(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("无效job_id")
        return self.request("GET", "/v1/jobs/" + job_id)

    def wait(self, job_id, timeout=600, interval=0.5, on_status=None):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            job = self.job(job_id)
            if on_status:
                on_status(job)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(interval)
        raise ModelAPIError(0, "WAIT_TIMEOUT", "客户端等待超时；服务端任务未自动取消，请按job_id继续查询或取消", True)

    def cancel(self, job_id):
        self.job(job_id)
        return self.request("POST", "/v1/jobs/" + job_id + "/cancel", {})

    def download(self, artifact, directory):
        name, path = artifact["name"], artifact["download_path"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", name) or name in {".", ".."} or not re.fullmatch(r"/v1/artifacts/[0-9a-f]{32}", path):
            raise ValueError("产物文件名或下载路径异常，未访问外部地址")
        content = self.request("GET", path, raw=True)
        if len(content) != artifact["size_bytes"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("下载产物体积或SHA256核验失败")
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        if target.is_symlink():
            raise ValueError("目标不能是符号链接")
        if target.exists():
            if target.read_bytes() != content:
                raise FileExistsError("目标已存在且内容不同，未覆盖")
        else:
            with target.open("xb") as stream:
                stream.write(content)
        return target


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 认证密钥不跟随重定向发送给其他站点。
        return None
