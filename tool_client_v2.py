"""无 GIS/GPU 依赖的 v2 应用客户端；文件、任务和产物按身份隔离。"""
import hashlib
from pathlib import Path
import re
import uuid

from model_client import ModelClient


class ToolClientV2(ModelClient):
    def tools(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise ValueError("limit 须为 1～50，offset 须为非负整数")
        return self.request("GET", f"/v2/tools?limit={limit}&offset={offset}")

    def tool_contract(self, tool_id):
        if not isinstance(tool_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tool_id):
            raise ValueError("无效工具 ID")
        return self.request("GET", "/v2/tools/" + tool_id)

    def upload(self, path, media_type):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16777216:
            raise ValueError("上传须为不超过 16MiB 的实际文件")
        if not re.fullmatch(r"[a-z0-9.+_-]+/[a-z0-9.+_-]+", media_type):
            raise ValueError("无效媒体类型")
        return self.request("POST", "/v2/files", path.read_bytes(), media_type=media_type)

    def submit(self, tool_id, inputs, parameters, idempotency_key=None):
        return self.request("POST", "/v2/jobs", {"tool_id": tool_id, "inputs": inputs, "parameters": parameters},
                            headers={"Idempotency-Key": idempotency_key or uuid.uuid4().hex})

    def job(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("无效 job_id")
        return self.request("GET", "/v2/jobs/" + job_id)

    def cancel(self, job_id):
        self.job(job_id)
        return self.request("POST", "/v2/jobs/" + job_id + "/cancel", {})

    def download(self, artifact, directory):
        name, path = artifact["name"], artifact["download_path"]
        if (not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", name) or name in {".", ".."}
                or not re.fullmatch(r"/v2/artifacts/[0-9a-f]{32}", path)):
            raise ValueError("产物文件名或路径不受支持")
        content = self.request("GET", path, raw=True)
        if len(content) != artifact["size_bytes"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("下载产物大小或 SHA256 校验失败")
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        if target.is_symlink() or target.exists() and target.read_bytes() != content:
            raise FileExistsError("下载目标已有不同内容或为链接，未覆盖")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(content)
        return target
