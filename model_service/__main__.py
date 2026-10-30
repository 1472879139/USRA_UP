"""独立启动、模拟、发放调用密钥；不读取主应用config.json。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import threading

from .contract import MOCK_TOKEN
from .backends import GPUBackend, MockBackend
from .server import APIServer, Service


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue-key", help="创建新身份的密钥文件，不在终端输出密钥")
    issue.add_argument("--owner", required=True)
    issue.add_argument("--keys-file", required=True)
    issue.add_argument("--client-file", required=True)
    issue.add_argument("--base-url", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--mode", choices=["mock", "gpu"], required=True)
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--port", type=int, default=18767)
    serve.add_argument("--fixtures", default="examples/model_api/mock-fixtures")
    serve.add_argument("--keys-file")
    serve.add_argument("--runtime-config")
    args = parser.parse_args()
    if args.command == "issue-key":
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.owner):
            parser.error("owner只能含英文字母、数字、下划线和连字符")
        keys_path = Path(args.keys_file)
        if Path(args.client_file).exists():
            parser.error("客户端密钥文件已存在，未覆盖")
        keys = json.loads(keys_path.read_text(encoding="utf-8")) if keys_path.exists() else {}
        if args.owner in keys:
            parser.error("该身份已存在，未轮换或覆盖原密钥")
        token = secrets.token_urlsafe(36)
        write_private(args.client_file, {"base_url": args.base_url, "api_key": token, "owner": args.owner})
        keys[args.owner] = hashlib.sha256(token.encode()).hexdigest()
        if keys_path.exists():
            # 不输出原始密钥；此操作只更新维护者显式指定的认证文件。
            keys_path.write_text(json.dumps(keys, indent=2), encoding="utf-8")
            os.chmod(keys_path, 0o600)
        else:
            write_private(keys_path, keys)
        print("身份密钥已写入指定私有文件；服务重启后生效。请通过私密渠道交付客户端文件。")
        return
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在1024～65535之间")
    if os.name != "nt":
        os.umask(0o077)
    if args.mode == "mock":
        keys = {"mock-dev": hashlib.sha256(MOCK_TOKEN.encode()).hexdigest()}
        backend = MockBackend(args.fixtures)
    else:
        if not args.keys_file or not args.runtime_config:
            parser.error("GPU模式必须提供--keys-file和--runtime-config")
        from .prepare_demo import configure_gis
        configure_gis()
        keys = json.loads(Path(args.keys_file).read_text(encoding="utf-8"))
        backend = GPUBackend(json.loads(Path(args.runtime_config).read_text(encoding="utf-8")))
    service = Service(args.data_dir, backend, keys)
    server = APIServer(("127.0.0.1", args.port), service)
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    print(f"模型API已启动：http://127.0.0.1:{args.port}；模式={args.mode}；只读健康检查/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
