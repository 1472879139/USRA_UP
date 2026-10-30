"""只监听本机回环地址的单GPU地物分类服务；不读取标签、凭据或主项目配置。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading

import terramind_protocol as protocol


class LandcoverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, model, scratch):
        if address[0] != "127.0.0.1":
            raise ValueError("GPU服务仅允许监听127.0.0.1。")
        self.model, self.scratch = model, Path(scratch)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.gpu_slot = threading.BoundedSemaphore(1)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def send_content(self, status, content, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def answer(self, status, message):
        self.send_content(status, json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path != "/health":
            self.answer(404, {"error": "unknown_path"})
            return
        self.answer(200, {"status": "ready", "protocol": protocol.PROTOCOL, "revision": protocol.REVISION,
                          "input": "Sentinel-2 L2A / DN10000 / 12bands / 224x224",
                          "ground_truth_read": False,"checkpoint_sha256":protocol.CHECKPOINT_SHA,
                          "tokenizer_sha256":protocol.TOKENIZER_SHA,"seed":protocol.SEED})

    def do_POST(self):
        if self.path != "/v1/landcover":
            self.answer(404, {"error": "unknown_path"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.answer(400, {"error": "bad_content_length"})
            return
        if not 0 < size <= protocol.MAX_INPUT_BYTES:
            self.answer(413, {"error": "input_size_limit"})
            return
        if self.headers.get_content_type() != "image/tiff":
            self.answer(415, {"error": "requires_tiff"})
            return
        request_id, input_sha = self.headers.get("X-Ursa-Request-ID", ""), self.headers.get("X-Ursa-Input-SHA256", "")
        expected = {"X-Ursa-Protocol": protocol.PROTOCOL, "X-Ursa-Sensor": "Sentinel-2",
                    "X-Ursa-Product-Level": "L2A", "X-Ursa-Encoding": "DN/10000"}
        if (not re.fullmatch(r"[0-9a-f]{32}", request_id) or not re.fullmatch(r"[0-9a-f]{64}", input_sha) or
                any(self.headers.get(key) != value for key, value in expected.items())):
            self.answer(400, {"error": "input_contract_mismatch"})
            return
        # 先读完有上限的请求体再回429，避免上传未结束时关闭连接导致客户端丢失错误状态。
        try:
            content = self.rfile.read(size)
        except (OSError, TimeoutError):
            self.answer(408, {"error": "upload_interrupted"})
            return
        if len(content) != size or protocol.sha(content) != input_sha:
            self.answer(400, {"error": "input_digest_mismatch"})
            return
        if not self.server.gpu_slot.acquire(blocking=False):
            self.answer(429, {"error": "gpu_busy"})
            return
        try:
            with tempfile.TemporaryDirectory(prefix="request_", dir=self.server.scratch) as folder:
                source = Path(folder) / "input.tif"
                source.write_bytes(content)
                try:
                    raw, valid, profile = protocol.read_input(source)
                except (ValueError, OSError):
                    self.answer(400, {"error": "input_preflight_failed"})
                    return
                try:
                    prediction, timing = self.server.model.predict(raw, valid)
                    prediction = prediction.astype("uint8")
                    prediction[~valid] = 255
                    response = protocol.package_response(prediction, profile,
                                                         request_id, input_sha, {**self.server.model.info, **timing})
                except Exception as exc:
                    print("GPU执行未完成：" + type(exc).__name__, flush=True)
                    self.answer(503, {"error": "gpu_execution_failed"})
                    return
            self.send_content(200, response, "application/zip")
        finally:
            self.server.gpu_slot.release()

    def log_message(self, format, *args):
        # 请求不包含原始文件名或用户文本，日志只保留固定路由和HTTP状态。
        print("[GPU服务] " + format % args, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError("服务端口应为1024～65535。")
    for name in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"]:
        os.environ[name] = "1"
    from runtime import GPUModel
    print("离线加载已安装的TerraMind Small与LULC解码器……", flush=True)
    model = GPUModel()
    server = LandcoverServer(("127.0.0.1", args.port), model, Path.home()/"ursa-gpu-runtime/requests/landcover")
    print(f"模型已就绪：http://127.0.0.1:{args.port}/health；仅通过私有SSH转发访问。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("GPU服务已停止。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
