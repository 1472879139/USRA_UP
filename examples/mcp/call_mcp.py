"""官方SDK接入示例：默认只读；显式传入影像与元数据才提交GPU任务。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
import uuid

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from model_client import ModelClient


async def run(args):
    settings = json.loads(Path(args.connection_file).read_text(encoding="utf-8-sig"))
    headers = {"Authorization": "Bearer " + settings["mcp_token"]}
    api = ModelClient(settings["api_base_url"], settings["api_key"])
    async with httpx2.AsyncClient(headers=headers, timeout=30, trust_env=False, verify=ssl.create_default_context()) as http:
        async with Client(streamable_http_client(settings["mcp_url"], http_client=http), read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            info = await client.call_tool("ursa_get_service_info", {})
            if info.is_error:
                raise RuntimeError(info.content[0].text)
            print(json.dumps({"connected": True, "execution_mode": info.structured_content["execution_mode"],
                              "tools": [t.name for t in tools.tools]}, ensure_ascii=False), flush=True)
            if not args.image and not args.job_id:
                return
            output = Path(args.results_dir or os.environ.get("URSA_RESULTS_DIR") or ("results/mcp_" + uuid.uuid4().hex))
            output.mkdir(parents=True, exist_ok=True)
            record_path = output / "submission.json"
            if args.image:
                if not args.metadata or args.job_id:
                    raise ValueError("提交影像需要--metadata，且不能同时指定--job-id")
                if record_path.exists():
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if (record["tool"] != args.tool or record["input_sha256"] != hashlib.sha256(Path(args.image).read_bytes()).hexdigest()
                            or record["metadata"] != json.loads(Path(args.metadata).read_text(encoding="utf-8-sig"))):
                        raise ValueError("已有提交记录与本次输入不一致，请为新任务使用新目录")
                else:
                    uploaded = await asyncio.to_thread(api.upload, args.image)
                    record = {"input_id": uploaded["input_id"], "input_sha256": uploaded["sha256"], "tool": args.tool,
                              "metadata": json.loads(Path(args.metadata).read_text(encoding="utf-8-sig")),
                              "idempotency_key": uuid.uuid4().hex}
                    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                submitted = await client.call_tool(record["tool"], {k: record[k] for k in ["input_id", "metadata", "idempotency_key"]})
                if submitted.is_error:
                    raise RuntimeError(submitted.content[0].text)
                job_id = submitted.structured_content["job_id"]
                record["job_id"] = job_id
                record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                job_id = args.job_id
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                response = await client.call_tool("ursa_get_job", {"job_id": job_id})
                if response.is_error:
                    raise RuntimeError(response.content[0].text)
                job = response.structured_content
                print(json.dumps({"job_id": job_id, "status": job["status"]}), flush=True)
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(2)
            else:
                raise TimeoutError("客户端等待超时，服务端任务仍可能运行；用原job_id继续查询")
            (output / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            if job["status"] != "succeeded":
                raise RuntimeError("任务未成功，详情见本次job.json")
            artifacts = await client.call_tool("ursa_get_artifacts", {"job_id": job_id})
            if artifacts.is_error:
                raise RuntimeError(artifacts.content[0].text)
            for artifact in artifacts.structured_content["artifacts"]:
                await asyncio.to_thread(api.download, artifact, output / ("task_" + job_id))
            print(json.dumps({"job_id": job_id, "status": "verified", "artifact_count": len(job["result"]["artifacts"]),
                              "execution_mode": job["result"]["execution_mode"], "accuracy_evaluated": job["result"]["accuracy_evaluated"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--image", help="显式指定后才提交新GPU任务")
    parser.add_argument("--metadata", help="与影像相符的JSON元数据文件")
    parser.add_argument("--tool", choices=["ursa_classify_landcover", "ursa_delineate_fields"], default="ursa_classify_landcover")
    parser.add_argument("--results-dir")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
