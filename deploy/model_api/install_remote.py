"""复用已验收GPU环境安装独立API；不安装依赖、不下载模型、不修改旧服务。"""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def private_json(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--relay-config", required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or version("torch") != "2.10.0+cu128":
        raise RuntimeError("请使用现有ursa-gpu-runtime/run-python.sh运行")
    os.umask(0o077)
    bundle = Path(args.bundle).expanduser().resolve()
    if file_sha(bundle) != args.sha256:
        raise RuntimeError("上传包SHA256不符")
    cloud = json.loads(Path(args.relay_config).read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", cloud["host"]) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", cloud["user"]):
        raise ValueError("中转主机或用户名不符")
    protected = ["torch", "torchvision", "rasterio", "numpy", "terratorch", "diffusers"]
    before = {name: version(name) for name in protected}
    root = Path.home() / "ursa-gpu-runtime/model-api-20261030"
    if root.exists() and any(root.iterdir()) and not (root / "installation-marker").is_file():
        raise RuntimeError("目标目录已有非本安装器管理的内容，未覆盖")
    root.mkdir(parents=True, exist_ok=True)
    (root / "installation-marker").write_text("ursa-model-api-v1\n", encoding="utf-8")
    with zipfile.ZipFile(bundle) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or len(names) > 40 or sum(i.file_size for i in package.infolist()) > 2 * 1024 ** 2:
            raise ValueError("发布包结构或体积异常")
        manifest = json.loads(package.read("release-manifest.json"))
        if set(names) != set(manifest["files"]) | {"release-manifest.json"}:
            raise ValueError("发布包文件与清单不符")
        release = root / "releases" / manifest["release_id"]
        if not re.fullmatch(r"[0-9a-f]{16}", manifest["release_id"]):
            raise ValueError("发布ID错误")
        release.mkdir(parents=True, exist_ok=True)
        for name, sha in manifest["files"].items():
            target = (release / name).resolve()
            if not target.is_relative_to(release) or not (name.startswith("model_service/") and name.endswith(".py") or name == "model_client.py"):
                raise ValueError("发布包路径越界")
            content = package.read(name)
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError("发布源文件SHA256错误")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != content:
                raise ValueError("同发布ID的已有文件不同，未覆盖")
            if not target.exists():
                target.write_bytes(content)
    da = Path.home() / "ursa-gpu-runtime/delineate-v2-20261029"
    tm = Path.home() / "ursa-gpu-runtime/services/landcover_292e61f891f9a042"
    if not (da / "venv/bin/python").is_file() or not (tm / "runtime.py").is_file():
        raise RuntimeError("已验收的模型运行时缺失，未安装替代版本")
    if file_sha(da / "models/DelineateAnythingv2.pt") != "46700b8a279b07922953a11adaeb5e658d9a2384b6334c8e0a3090886218915a":
        raise RuntimeError("DA V2完整权重校验失败")
    private = root / "private"
    private.mkdir(exist_ok=True)
    runtime = {"da_python": str(da / "venv/bin/python"), "da_weights": str(da / "models/DelineateAnythingv2.pt"),
               "terramind_python": sys.executable, "terramind_runtime_dir": str(tm)}
    private_json(root / "runtime.local.json", runtime)
    keys = root / "api-keys.local.json"
    for owner in ["maintainer", "application-dev"]:
        client_file = private / (owner + ".local.json")
        if not client_file.exists():
            subprocess.run([sys.executable, "-m", "model_service", "issue-key", "--owner", owner,
                            "--keys-file", str(keys), "--client-file", str(client_file), "--base-url", cloud["public_url"]], cwd=release, check=True)
    relay_key = root / "relay-key"
    if not relay_key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ursa-model-api-relay-20261030", "-f", str(relay_key)], check=True)
    known_hosts = root / "relay-known-hosts"
    known_hosts.write_text(cloud["host"] + " " + cloud["host_key"] + "\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    wrapper = Path.home() / "ursa-gpu-runtime/run-python.sh"
    api_unit = f'''[Unit]
Description=USRA Model API v1
After=network-online.target

[Service]
Type=simple
WorkingDirectory={release}
ExecStart=/bin/bash {wrapper} -m model_service serve --mode gpu --data-dir {root}/data --keys-file {keys} --runtime-config {root}/runtime.local.json --port 18767
Environment=PYTHONIOENCODING=utf-8
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
'''
    relay_unit = f'''[Unit]
Description=USRA Model API reverse SSH relay
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -p 22 -i {relay_key} -o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -N -T -R 127.0.0.1:18767:127.0.0.1:18767 {cloud['user']}@{cloud['host']}
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
'''
    for name, source in [("ursa-model-api.service", api_unit), ("ursa-model-api-relay.service", relay_unit)]:
        target = units / name
        if target.exists() and "Description=USRA Model API" not in target.read_text():
            raise RuntimeError("同名服务并非本任务维护，未覆盖")
        target.write_text(source, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "ursa-model-api.service"], check=True)
    subprocess.run(["systemctl", "--user", "restart", "ursa-model-api.service"], check=True)
    after = {name: version(name) for name in protected}
    if before != after:
        raise RuntimeError("原GPU依赖发生变化")
    relay_state = subprocess.run(["systemctl", "--user", "is-active", "ursa-model-api-relay.service"],
                                 capture_output=True, text=True, check=False).stdout.strip()
    private_json(root / "deployment.json", {"release_id": manifest["release_id"], "bundle_sha256": args.sha256,
        "base_versions_before": before, "base_versions_after": after, "base_versions_unchanged": True,
        "status": "api_installed", "relay_state": relay_state, "port": 18767})
    print("独立API已安装；原GPU依赖未变化。中转状态：" + relay_state + "；仍需核验完整HTTPS链路。")
    print("RELEASE_ID=" + manifest["release_id"])
    print("RELAY_PUBLIC_KEY_FILE=" + str(relay_key) + ".pub")


if __name__ == "__main__":
    main()
