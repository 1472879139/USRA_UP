"""应用开发者最小示例：只需要Python标准库和服务地址/密钥。"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from model_client import ModelClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", help="私有JSON，字段base_url、api_key；不要提交Git")
    parser.add_argument("--base-url", default="http://127.0.0.1:18767")
    parser.add_argument("--token-env", default="URSA_GPU_API_KEY")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--input", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--capability", choices=["da-v2-field-delineation", "terramind-small-landcover"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.connection_file:
        connection = json.loads(Path(args.connection_file).read_text(encoding="utf-8"))
        client = ModelClient(connection["base_url"], connection["api_key"])
    else:
        token = "mock-development-token-only-for-localhost" if args.mock else os.environ.get(args.token_env, "")
        client = ModelClient(args.base_url, token)
    metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
    health = client.health()
    print("接口版本：", health["api_version"], "执行模式：", health["execution_mode"])
    if health["execution_mode"] == "mock" and not args.mock:
        raise SystemExit("服务是模拟模式，请显式使用--mock，不能当作真实模型结果")
    uploaded = client.upload(args.input)
    job = client.submit(args.capability, uploaded["input_id"], metadata)
    print("任务ID：", job["job_id"])
    final = client.wait(job["job_id"], on_status=lambda value: print("状态：", value["status"]))
    if final["status"] != "succeeded":
        print(json.dumps(final["error"], ensure_ascii=False))
        raise SystemExit(1)
    for item in final["result"]["artifacts"]:
        print("已下载并核验：", client.download(item, args.output))
    root = Path(args.output)
    (root / "receipt.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print("完成；服务返回成功不代表识别精度已验证。")


if __name__ == "__main__":
    main()
