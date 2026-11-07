"""本机 v2 模拟服务和完整自检；无需 GIS、CUDA、权重或生产凭据。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_service.backends import BackendError
from model_service.contract import CAPABILITIES
from model_service.server import APIServer, Service
from model_service.v2_adapters import build_registry
from tool_client_v2 import ToolClientV2


class NoModelBackend:
    mode = "mock"

    def availability(self):
        return {tool["id"]: "unavailable" for tool in CAPABILITIES}

    def run(self, *args):
        raise BackendError("MODEL_UNAVAILABLE", "本地演示不运行模型")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19867)
    parser.add_argument("--check", action="store_true", help="自动运行 JSON 和双 CSV 模拟任务并退出")
    parser.add_argument("--results-dir", help="本次模拟验收的空结果目录，不覆盖已有记录")
    args = parser.parse_args()
    output = Path(args.results_dir or os.environ.get("URSA_RESULTS_DIR") or ("results/v2_demo_" + uuid.uuid4().hex))
    with tempfile.TemporaryDirectory(prefix="ursa-v2-demo-") as temporary:
        runtime = Path(temporary)
        token = secrets.token_urlsafe(36)
        backend = NoModelBackend()
        registry = build_registry(backend, runtime / "tools.local.json", examples=True)
        registry.initialize()
        service = Service(runtime / "data", backend, {"local-demo": hashlib.sha256(token.encode()).hexdigest()}, v2_registry=registry)
        try:
            server = APIServer(("127.0.0.1", args.port), service)
        except Exception:
            service.close()
            raise
        url = "http://127.0.0.1:" + str(server.server_port)
        connection = runtime / "client.local.json"
        connection.write_text(json.dumps({"api_base_url": url, "api_key": token}), encoding="utf-8")
        print(json.dumps({"mode": "mock", "api_url": url, "connection_file": str(connection)}, ensure_ascii=False), flush=True)
        thread = None
        try:
            if args.check:
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
                thread.start()
                output.mkdir(parents=True, exist_ok=True)
                if any(output.iterdir()):
                    raise FileExistsError("结果目录已有运行记录，未覆盖；新演示请使用新的空目录")
                client = ToolClientV2(url, token)
                before, after = runtime / "before.csv", runtime / "after.csv"
                before.write_text("value\n1\n2\n", encoding="utf-8")
                after.write_text("value\n4\n8\n", encoding="utf-8")
                files = {name: {"file_id": client.upload(path, "text/csv")["file_id"]}
                         for name, path in [("before", before), ("after", after)]}
                jobs = []
                for tool, inputs, parameters in [("demo-json-summary", {"values": [1, 2, 3]}, {}),
                                                  ("demo-csv-compare", files, {"column": "value"})]:
                    key = uuid.uuid4().hex
                    record = output / (tool + "-submission.json")
                    record.write_text(json.dumps({"tool_id": tool, "inputs": inputs, "parameters": parameters,
                                                   "idempotency_key": key}), encoding="utf-8")
                    job = client.submit(tool, inputs, parameters, key)
                    result = client.wait(job["job_id"], timeout=10, interval=0.02)
                    if result["status"] != "succeeded":
                        raise RuntimeError("本机模拟任务失败：" + json.dumps(result["error"], ensure_ascii=False))
                    folder = output / tool
                    folder.mkdir()
                    (folder / "job.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    for artifact in result["result"]["artifacts"]:
                        client.download(artifact, folder)
                    jobs.append({"tool_id": tool, "status": "verified", "outputs": result["result"]["outputs"]})
                print(json.dumps({"mode": "mock", "jobs": jobs, "results_dir": str(output.resolve())}, ensure_ascii=False))
            else:
                print("临时输入和任务仅保存在本次运行目录；Ctrl+C 退出后清理。", flush=True)
                server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if thread is not None:
                server.shutdown()
                thread.join(timeout=3)
            server.server_close()
            service.close()


if __name__ == "__main__":
    main()
