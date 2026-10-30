"""生成不含真实凭据/模型的双人交接包，可选只读应用源码快照。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def digest(content):
    return hashlib.sha256(content).hexdigest()


def pack(source, target, context):
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    manifest = {"context": context, "files": {p.relative_to(source).as_posix(): digest(p.read_bytes()) for p in files}}
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for path in files:
            package.write(path, path.relative_to(source).as_posix())
        package.writestr("handoff-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"name": target.name, "sha256": digest(target.read_bytes()), "file_count": len(files)}


def copy_paths(source, destination, names):
    for name in names:
        path = source / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def examples(root):
    from model_client import ModelClient
    from model_service.backends import MockBackend
    from model_service.contract import MOCK_TOKEN, BANDS
    from model_service.server import APIServer, Service
    service = Service(root / ".temporary-example-service", MockBackend(root / "examples/model_api/mock-fixtures"),
                      {"example": digest(MOCK_TOKEN.encode())})
    server = APIServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        client = ModelClient("http://127.0.0.1:" + str(server.server_port), MOCK_TOKEN)
        output = root / "examples/model_api/responses"
        output.mkdir()
        (output / "capabilities.json").write_text(json.dumps(client.capabilities(), ensure_ascii=False, indent=2), encoding="utf-8")
        uploaded = client.upload(root / "examples/model_api/mock-fixtures/input.tif")
        metadata = {"sensor": "Sentinel-2", "product_level": "L2A", "encoding": "dn_10000", "bands": BANDS}
        for capability in ["da-v2-field-delineation", "terramind-small-landcover"]:
            job = client.submit(capability, uploaded["input_id"], metadata)
            final = client.wait(job["job_id"], interval=0.02, timeout=10)
            if final["status"] != "succeeded":
                raise RuntimeError("模拟响应样例生成失败")
            (output / (capability + ".json")).write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        service.close()
    # 此目录只属于本打包器临时树，包含的是刚生成的模拟索引。
    temporary = (root / ".temporary-example-service").resolve()
    if temporary.parent != root.resolve():
        raise ValueError("临时目录越界")
    shutil.rmtree(temporary)


def build(destination, application_source=None):
    import sys
    sys.path.insert(0, str(ROOT))
    from model_service.openapi import specification
    from model_service.prepare_demo import prepare
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    reports = []
    with tempfile.TemporaryDirectory(prefix="ursa-handoff-") as temporary:
        staging = Path(temporary) / "api-developer"
        staging.mkdir()
        names = [p.relative_to(ROOT).as_posix() for p in (ROOT / "model_service").glob("*.py")]
        names += ["model_client.py", "examples/model_api/call_api.py", "examples/model_api/start_mock.ps1",
                  "examples/model_api/connection.example.json", "docs/model-service-handoff.md", "docs/model-api-operations.md",
                  "docs/fixes/2026-10-30-026-model-api-handoff.html", "AGENTS.md", "CONTRIBUTING.md"]
        copy_paths(ROOT, staging, names)
        (staging / "openapi.json").write_text(json.dumps(specification(), ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / "开始使用.md").write_text(
            "# 应用开发包\n\n先阅读 docs/model-service-handoff.md。\n\n"
            "需要 Python 3.10+，不需要安装第三方依赖。包内 TIFF 和结果全部为合成示例。\n\n"
            "在包根目录运行 `python -m model_service serve --mode mock --data-dir .model-service/mock`。\n"
            "另开终端按 docs/model-api-operations.md 第1节运行两个示例。\n\n"
            "网页、DeepSeek配置与工具调度在应用源码包中开发；本包是稳定接口和独立模拟服务。\n"
            "私有 application-dev.local.json 由维护者单独交付；请勿提交或把它放进公开附件。\n"
            "responses/ 中任务ID来自打包时的临时模拟实例，只说明响应格式，不能拿到新实例查询。\n", encoding="utf-8")
        prepare(staging / "examples/model_api/mock-fixtures")
        examples(staging)
        reports.append(pack(staging, destination / "应用开发包_API-v1.zip", {"kind": "sdk_and_mock", "api_version": "1.0.0", "synthetic_fixtures": True}))
        server = Path(temporary) / "server-source"
        server.mkdir()
        server_names = names + [p.relative_to(ROOT).as_posix() for p in (ROOT / "deploy/model_api").glob("*.py")]
        server_names += ["tests/test_model_service.py"]
        copy_paths(ROOT, server, server_names)
        reports.append(pack(server, destination / "模型服务源码_API-v1.zip", {"kind": "model_service_source", "api_version": "1.0.0"}))
        if application_source:
            source = Path(application_source).resolve()
            snapshot = Path(temporary) / "application-source"
            snapshot.mkdir()
            extensions = {".py", ".js", ".html", ".css", ".txt"}
            names = []
            for directory in ["agents", "core", "gui", "prompts", "tools", "benchmarks", "tests"]:
                for path in (source / directory).rglob("*"):
                    if path.is_file() and path.suffix in extensions and "__pycache__" not in path.parts:
                        names.append(path.relative_to(source).as_posix())
            names += ["main.py", "environment.yml", "requirements.txt", "config.example.json", "gpu.example.json", "benchmark.example.json", ".gitignore", "AGENTS.md", "CONTRIBUTING.md", "README.md", "docs/autonomous-agent.md"]
            # 只包含公开案例索引/协议；影像、标签及私有答案目录均不复制。
            names += ["benchmarks/cases.json", "benchmarks/protocols/water_20261025.json", "benchmarks/protocols/water_methods_20261026.json"]
            copy_paths(source, snapshot, names)
            baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=source, text=True).strip()
            (snapshot / "交接快照说明.md").write_text(
                "# 应用源码开发快照\n\n"
                f"来源分支：{branch}；Git基线：{baseline}。\n\n"
                "这是白名单源码副本，包含该工作树未提交的自主 Agent 开发内容，"
                "不代表 GitHub 上已有这些内容。原工作树没有被提交、丢弃或修改。\n\n"
                "不含真实config、输入数据、参考答案、产物、记忆、对话、环境、GPU权重或旧私有接口配置。"
                "tests仅保留代码，不含历史测试材料；依赖本地数据的测试需另行准备。\n\n"
                "README中的历史文档链接未全部打包。此快照尚未接入新API："
                "结合独立应用开发包的 docs/model-service-handoff.md 接入，"
                "先用 mock，再私下配置真实地址和应用方密钥。\n\n"
                "在新目录阅读或迁移；不要覆盖他人工作区。环境按environment.yml重建。"
                "主界面仍含GIS工具，完整应用环境和仅SDK模拟环境不同。\n", encoding="utf-8")
            reports.append(pack(snapshot, destination / "应用源码快照_未接新API.zip", {"kind": "application_source_snapshot", "source_branch": branch, "source_head": baseline, "includes_uncommitted_source": True}))
    (destination / "SHA256清单.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    parser.add_argument("--application-source")
    args = parser.parse_args()
    build(args.destination, args.application_source)
