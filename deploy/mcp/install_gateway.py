"""在既有API站点增加MCP路由与独立受限隧道公钥；配置失败恢复本次改动。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--relay-user", required=True)
    parser.add_argument("--relay-public-key", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("需由已授权维护账号通过sudo执行Nginx配置")
    os.umask(0o077)
    subprocess.run(["nginx", "-t"], check=True)
    site = Path(args.site).resolve()
    if not site.is_relative_to(Path("/etc/nginx")) or not site.is_file():
        raise ValueError("需明确指定/etc/nginx中的既有站点")
    original = site.read_bytes()
    text = original.decode()
    anchor = "include /etc/nginx/snippets/ursa-model-api-v1.conf;"
    include = "include /etc/nginx/snippets/ursa-mcp-v1.conf;"
    if include not in text:
        if text.count(anchor) != 1:
            raise RuntimeError("既有API站点锚点不唯一，未覆盖整站")
        text = text.replace(anchor, anchor + "\n    " + include, 1)
    snippets = Path("/etc/nginx/snippets/ursa-mcp-v1.conf")
    limits = Path("/etc/nginx/conf.d/ursa-mcp-v1.conf")
    previous = {p: p.read_bytes() if p.exists() else None for p in [snippets, limits]}
    if any(data is not None and b"# USRA MCP v1" not in data for data in previous.values()):
        raise RuntimeError("同名配置由其他任务维护，未覆盖")
    parts = Path(args.relay_public_key).read_text().strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
        raise ValueError("需提供本次独立MCP隧道的ED25519公钥")
    account = pwd.getpwnam(args.relay_user)
    auth = Path(account.pw_dir) / ".ssh/authorized_keys"
    if auth.is_symlink() or auth.parent.is_symlink():
        raise ValueError("SSH认证文件不能是符号链接")
    old_auth = auth.read_bytes() if auth.exists() else b""
    line = ('restrict,port-forwarding,permitopen="127.0.0.1:18768",permitlisten="127.0.0.1:18768",'
            'command="/bin/false" ssh-ed25519 ' + parts[1] + ' ursa-mcp-relay').encode()
    if parts[1].encode() in old_auth and line not in old_auth.splitlines():
        raise ValueError("该公钥已有不同权限，未擅自更改")
    backup = Path("/var/backups/ursa-mcp") / str(time.time_ns())
    backup.mkdir(parents=True)
    (backup / "site.conf").write_bytes(original)
    (backup / "authorized_keys").write_bytes(old_auth)
    for path, data in previous.items():
        if data is not None:
            (backup / (path.parent.name + "-" + path.name)).write_bytes(data)
    try:
        snippets.write_text('''# USRA MCP v1
location ^~ /ursa-mcp/ {
    client_max_body_size 64k;
    limit_req zone=ursa_mcp_rate burst=20 nodelay;
    limit_req_status 429;
    proxy_pass http://127.0.0.1:18768/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_cache off;
    proxy_connect_timeout 5s;
    proxy_send_timeout 60s;
    proxy_read_timeout 90s;
    proxy_intercept_errors off;
    access_log /var/log/nginx/ursa-mcp-access.log ursa_mcp_safe;
}
''')
        limits.write_text('''# USRA MCP v1
limit_req_zone $binary_remote_addr zone=ursa_mcp_rate:1m rate=5r/s;
log_format ursa_mcp_safe '$remote_addr $request_method $uri $status $body_bytes_sent';
''')
        if site.read_bytes() != original:
            raise RuntimeError("站点在安装期间被修改，停止以保护其他任务")
        site.write_text(text)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        if line not in old_auth.splitlines():
            auth.parent.mkdir(mode=0o700, exist_ok=True)
            with auth.open("ab") as stream:
                stream.write((b"" if not old_auth or old_auth.endswith(b"\n") else b"\n") + line + b"\n")
            os.chown(auth.parent, account.pw_uid, account.pw_gid)
            os.chown(auth, account.pw_uid, account.pw_gid)
            auth.chmod(0o600)
    except Exception:
        if site.read_bytes() == text.encode():
            site.write_bytes(original)
        for path, data in previous.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        subprocess.run(["nginx", "-t"], check=False)
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
        raise
    record = {"prefix": "/ursa-mcp/", "upstream": "127.0.0.1:18768", "backup": str(backup),
              "site_before_sha256": hashlib.sha256(original).hexdigest(), "relay_shell_access": False,
              "relay_permitted_port": 18768, "credentials_logged": False}
    (backup / "change-record.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record))


if __name__ == "__main__":
    main()
