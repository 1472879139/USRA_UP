"""每个GPU任务使用固定模型运行时；无安装、下载、Shell或动态用户代码。"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

from .backends import BackendError
from .contract import CAPABILITIES, canonical, sha256


def offline():
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("GPU任务执行阶段禁止网络下载或外联")
        return original(sock, address)
    socket.socket.connect = connect


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(folder):
    # Linux父服务退出时结束本任务，避免遗留GPU子进程。
    if sys.platform == "linux":
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if os.getppid() != parent:
            raise BackendError("SERVER_STOPPED", "服务已停止")
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    if file_sha(request["input_path"]) != request["input_sha256"]:
        raise BackendError("INPUT_CHANGED", "输入影像在提交后发生变化")
    offline()
    from .inference import read_input, predict_da, predict_terramind, write_products
    try:
        raw, valid, profile = read_input(request["input_path"], request["metadata"])
    except BackendError:
        raise
    except (ValueError, OSError) as exc:
        raise BackendError("INPUT_UNSUPPORTED", "TIFF无法读取或其空间/数值编码不受支持") from exc
    import torch
    if not torch.cuda.is_available():
        raise BackendError("GPU_UNAVAILABLE", "GPU不可用，未回退CPU冒充GPU推理", True)
    torch.manual_seed(20261028)
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.cuda.reset_peak_memory_stats()
    capability = next(c for c in CAPABILITIES if c["id"] == request["capability_id"])
    started = time.perf_counter()
    boundaries = None
    if capability["tier"] == "specialist":
        weight = Path(request["runtime"]["da_weights"])
        if file_sha(weight) != capability["weights_sha256"]:
            raise BackendError("WEIGHTS_MISMATCH", "DA V2完整任务权重校验失败")
        from ultralytics import YOLO
        model = YOLO(str(weight), task="segment")
        if model.task != "segment" or model.model.model[-1].nc != 1:
            raise BackendError("WEIGHTS_MISMATCH", "DA V2任务结构不符")
        prediction, boundaries, settings = predict_da(model, raw, valid, profile)
    else:
        runtime_dir = Path(request["runtime"]["terramind_runtime_dir"]).resolve()
        sys.path.insert(0, str(runtime_dir))
        from runtime import GPUModel
        model = GPUModel()
        prediction, settings = predict_terramind(model, raw, valid)
    torch.cuda.synchronize()
    provenance = {"api_version": "1.0.0", "input_sha256": request["input_sha256"], "metadata": request["metadata"],
                  "model": capability["model"], "model_revision": capability["model_revision"],
                  "weights_sha256": capability["weights_sha256"], "full_task_weights": True,
                  "random_fallback_parameters": 0, "seed": 20261028, "precision": "float32", "settings": settings,
                  "elapsed_seconds_load_and_infer": time.perf_counter() - started,
                  "peak_allocated_mib": torch.cuda.max_memory_allocated() / 1024 ** 2,
                  "gpu": torch.cuda.get_device_name(0), "reference_labels_read": False, "accuracy_evaluated": False,
                  "implementation_sha256": sha256((Path(__file__).read_bytes() + Path(__file__).with_name("inference.py").read_bytes()))}
    result = write_products(folder / "output", prediction, raw, valid, profile, capability["id"], provenance, boundaries)
    if settings.get("detection_limit_reached"):
        result["warnings"].append("至少一块达到检测数量上限，可能遗漏候选田块")
    (folder / "worker-result.json").write_text(canonical(result), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True)
    folder = Path(parser.parse_args().job_dir).resolve()
    try:
        run(folder)
    except BackendError as exc:
        (folder / "worker-error.json").write_text(canonical({"code": exc.code, "message": exc.message, "retryable": exc.retryable}), encoding="utf-8")
        raise SystemExit(1)
    except Exception as exc:
        code = "GPU_OUT_OF_MEMORY" if "out of memory" in str(exc).lower() else "MODEL_EXECUTION_FAILED"
        (folder / "worker-error.json").write_text(canonical({"code": code, "message": "模型执行未完成，维护者可按任务ID检查运行日志", "retryable": False}), encoding="utf-8")
        import traceback
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
