"""同一API下的显式模拟后端与隔离进程GPU后端。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .contract import CAPABILITIES, canonical, sha256


class BackendError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class MockBackend:
    mode = "mock"

    def __init__(self, fixtures, delay=0.15):
        self.fixtures = Path(fixtures).resolve()
        self.delay = delay
        if not (self.fixtures / "index.json").is_file():
            raise ValueError("模拟产物不存在：请运行prepare_demo，或使用已带样例的应用开发包")

    def availability(self):
        return {item["id"]: "ready" for item in CAPABILITIES}

    def run(self, task, folder, input_path, cancelled):
        until = time.monotonic() + self.delay
        while time.monotonic() < until:
            if cancelled.wait(0.02):
                raise BackendError("JOB_CANCELLED", "任务已取消")
        kind = "da" if task["capability_id"] == "da-v2-field-delineation" else "terramind"
        # 模拟只验证协议和页面流程，输出均为带水印的合成示例，不分析上传影像。
        index = json.loads((self.fixtures / "index.json").read_text(encoding="utf-8"))
        result = index[kind]
        output = folder / "output"
        output.mkdir()
        for item in result["files"]:
            source = self.fixtures / kind / item["name"]
            if source.is_symlink() or sha256(source.read_bytes()) != item["sha256"]:
                raise BackendError("MOCK_FIXTURE_CHANGED", "模拟样例校验失败")
            shutil.copyfile(source, output / item["name"])
        return {**result, "execution_mode": "mock", "quality_status": "synthetic_demo",
                "accuracy_evaluated": False, "input_sha256": task["input"]["sha"],
                "warnings": ["模拟数据：不分析上传影像，仅验证API、进度、下载和展示；不能作为识别结果"]}


class GPUBackend:
    mode = "gpu"

    def __init__(self, config, max_seconds=600):
        self.config = dict(config)
        self.max_seconds = max_seconds
        self.package_root = Path(__file__).resolve().parent.parent

    def availability(self):
        result = {}
        da = all(Path(self.config.get(key, "/not-configured")).is_file() for key in ["da_python", "da_weights"])
        tm = (Path(self.config.get("terramind_python", "/not-configured")).is_file()
              and (Path(self.config.get("terramind_runtime_dir", "/not-configured")) / "runtime.py").is_file())
        result["da-v2-field-delineation"] = "configured" if da else "unavailable"
        result["terramind-small-landcover"] = "configured" if tm else "unavailable"
        return result

    def run(self, task, folder, input_path, cancelled):
        if self.availability()[task["capability_id"]] == "unavailable":
            raise BackendError("MODEL_UNAVAILABLE", "维护者尚未配置该模型运行时")
        job = {"capability_id": task["capability_id"], "input_path": str(input_path),
               "input_sha256": task["input"]["sha"], "metadata": task["payload"]["metadata"],
               "runtime": self.config}
        (folder / "request.json").write_text(canonical(job), encoding="utf-8")
        python = self.config["da_python" if task["capability_id"] == "da-v2-field-delineation" else "terramind_python"]
        env = {k: v for k, v in os.environ.items() if k not in {"URSA_MODEL_API_TOKEN", "URSA_MODEL_API_KEYS"}}
        env["PYTHONPATH"] = str(self.package_root)
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", YOLO_AUTOINSTALL="false",
                   YOLO_CONFIG_DIR=str(folder / "yolo-settings"), PYTHONIOENCODING="utf-8")
        started = time.monotonic()
        with (folder / "worker.log").open("wb") as log:
            process = subprocess.Popen([python, "-m", "model_service.worker", "--job-dir", str(folder)],
                                       cwd=self.package_root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
            reason = None
            try:
                while process.poll() is None:
                    if cancelled.wait(0.1):
                        reason = BackendError("JOB_CANCELLED", "任务已取消")
                        break
                    if time.monotonic() - started > self.max_seconds:
                        reason = BackendError("JOB_TIMEOUT", "模型任务超过服务时间上限", True)
                        break
                    if (folder / "worker.log").stat().st_size > 2 * 1024 ** 2:
                        reason = BackendError("WORKER_LOG_LIMIT", "模型输出日志超过限制")
                        break
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            if reason:
                raise reason
        result_path = folder / "worker-result.json"
        if process.returncode != 0 or not result_path.is_file():
            error_path = folder / "worker-error.json"
            if error_path.is_file():
                error = json.loads(error_path.read_text(encoding="utf-8"))
                raise BackendError(error["code"], error["message"], error.get("retryable", False))
            raise BackendError("MODEL_EXECUTION_FAILED", "模型运行失败，维护者可按任务ID查看服务器记录")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("execution_mode") != "gpu" or result.get("input_sha256") != task["input"]["sha"]:
            raise BackendError("RESULT_CONTRACT_FAILED", "模型结果来源核验失败")
        return result
