"""GPU主机的独立MCP发布安装器；保留既有模型/API环境与用户数据。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import zipfile


def unpack(bundle, expected_sha, root):
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("发布包哈希不匹配")
    with zipfile.ZipFile(bundle) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or len(names) > 24 or sum(i.file_size for i in package.infolist()) > 1024 ** 2:
            raise ValueError("发布包结构异常")
        manifest = json.loads(package.read("release-manifest.json"))
        if manifest.get("service") != "ursa_mcp" or not re.fullmatch(r"[0-9a-f]{16}", manifest["release_id"]):
            raise ValueError("发布身份无效")
        if set(names) != set(manifest["files"]) | {"release-manifest.json"}:
            raise ValueError("发布包文件清单不一致")
        release = root / "releases" / manifest["release_id"]
        if not release.resolve().is_relative_to(root.resolve()):
            raise ValueError("发布目录经符号链接越界")
        for name, expected in manifest["files"].items():
            if not (re.fullmatch(r"ursa_mcp/[a-z_][a-z0-9_]*\.py", name) or name == "requirements-mcp.lock"):
                raise ValueError("发布包包含未允许路径")
            target = release / name
            if not target.resolve().is_relative_to(release.resolve()):
                raise ValueError("源文件目标经符号链接越界")
            data = package.read(name)
            if hashlib.sha256(data).hexdigest() != expected or target.is_symlink():
                raise ValueError("源文件哈希或目标异常")
            if target.exists() and target.read_bytes() != data:
                raise ValueError("同发布ID内容不一致，未覆盖")
        for name in manifest["files"]:
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(package.read(name))
    return release, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--api-profile", required=True)
    parser.add_argument("--relay-config", required=True)
    parser.add_argument("--owner", default="application-dev")
    parser.add_argument("--enable-v2", action=argparse.BooleanOptionalAction, default=None,
                        help="启用或关闭 v2；不传时保留已有服务配置")
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info < (3, 11):
        raise RuntimeError("请在GPU主机使用Python3.11+运行，推荐Python3.12")
    os.umask(0o077)
    root = Path.home() / "ursa-mcp-service"
    if root.is_symlink() or root.exists() and not (root / "installation-marker").is_file():
        raise RuntimeError("目标不是本安装器管理目录，未覆盖")
    root.mkdir(exist_ok=True)
    (root / "installation-marker").write_text("ursa-mcp-v1\n")
    release, manifest = unpack(Path(args.bundle).resolve(), args.sha256, root)
    venv = release / "venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    subprocess.run([str(python), "-m", "pip", "install", "--require-hashes", "--no-deps", "-r", str(release / "requirements-mcp.lock")],
                   env=environment, check=True)
    private = root / "private"
    private.mkdir(exist_ok=True)
    api = json.loads(Path(args.api_profile).read_text())
    relay = json.loads(Path(args.relay_config).read_text())
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", relay["host"]) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", relay["user"]):
        raise ValueError("云端连接字段无效")
    public = urlsplit(api["base_url"])
    if public.scheme != "https":
        raise ValueError("公网API必须使用HTTPS")
    public_mcp = public._replace(path="/ursa-mcp/mcp", query="", fragment="").geturl()
    # issue_key依赖业务校验库，使用独立环境加载，避免向原模型解释器安装依赖。
    client_file = private / (args.owner + ".local.json")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.owner):
        raise ValueError("调用身份无效")
    if not client_file.exists():
        subprocess.run([str(python), "-m", "ursa_mcp.issue_key", "--identity-file", str(private / "identities.local.json"),
            "--api-connection", str(Path(args.api_profile).resolve()), "--client-file", str(client_file),
            "--owner", args.owner, "--public-mcp-url", public_mcp], cwd=release, env=environment, check=True)
    config = {"api_base_url": "http://127.0.0.1:18767", "public_api_base_url": api["base_url"].rstrip("/"),
              "public_mcp_url": public_mcp, "identities_file": str(private / "identities.local.json"), "requests_per_minute": 120}
    config_file = private / "server.local.json"
    old_config = json.loads(config_file.read_text()) if config_file.exists() else {}
    config["enable_v2"] = args.enable_v2 if args.enable_v2 is not None else old_config.get("enable_v2", False)
    if type(config["enable_v2"]) is not bool:
        raise ValueError("已有 enable_v2 配置不是布尔值")
    config_file.write_text(json.dumps(config, indent=2))
    relay_key = private / "relay-key"
    if not relay_key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ursa-mcp-relay", "-f", str(relay_key)], check=True)
    known = private / "known-hosts"
    known.write_text(relay["host"] + " " + relay["host_key"] + "\n")
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    service = f'''[Unit]
Description=USRA MCP service
After=network-online.target

[Service]
Type=simple
WorkingDirectory={release}
ExecStart="{python}" -m ursa_mcp.server --config "{config_file}" --port 18768
Environment=PYTHONIOENCODING=utf-8
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
'''
    tunnel = f'''[Unit]
Description=USRA MCP reverse SSH relay
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -i "{relay_key}" -o "UserKnownHostsFile={known}" -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -N -T -R 127.0.0.1:18768:127.0.0.1:18768 {relay['user']}@{relay['host']}
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
'''
    backup = root / "backups" / str(time.time_ns())
    backup.mkdir(parents=True)
    previous = {}
    for name, text in [("ursa-mcp.service", service), ("ursa-mcp-relay.service", tunnel)]:
        target = units / name
        old = target.read_bytes() if target.exists() else None
        if old is not None and b"Description=USRA MCP" not in old:
            raise RuntimeError("同名服务由其他任务管理，未覆盖")
        previous[target] = old
        if old:
            (backup / name).write_bytes(old)
        target.write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    try:
        subprocess.run(["systemctl", "--user", "enable", "ursa-mcp.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "ursa-mcp.service"], check=True)
        opener = build_opener(ProxyHandler({}))
        for attempt in range(20):
            try:
                with opener.open("http://127.0.0.1:18768/health", timeout=2) as response:
                    assert json.load(response)["server"] == "ursa_mcp"
                break
            except (OSError, ValueError, AssertionError):
                if attempt == 19:
                    raise RuntimeError("新服务健康检查失败")
                time.sleep(0.5)
    except Exception:
        subprocess.run(["systemctl", "--user", "stop", "ursa-mcp.service"], check=False)
        for target, old in previous.items():
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        if previous[units / "ursa-mcp.service"]:
            subprocess.run(["systemctl", "--user", "start", "ursa-mcp.service"], check=False)
        raise
    record = {"release_id": manifest["release_id"], "bundle_sha256": args.sha256,
              "gpu_dependencies_modified": False, "relay_started": False, "unit_backup": str(backup)}
    (root / "deployment.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record))
    print("MCP已在GPU回环启动；请在云端配置独立受限公钥后再启用ursa-mcp-relay.service。")


if __name__ == "__main__":
    main()
