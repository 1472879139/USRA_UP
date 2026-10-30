"""在已有HTTPS站点增加独立API路径；备份、nginx校验失败自动恢复。"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--relay-user", required=True)
    parser.add_argument("--relay-public-key", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("需要通过sudo管理Nginx；不修改SSH全局策略")
    subprocess.run(["nginx", "-t"], check=True)
    account = pwd.getpwnam(args.relay_user)
    authorized = Path(account.pw_dir) / ".ssh/authorized_keys"
    if authorized.is_symlink() or authorized.parent.is_symlink():
        raise RuntimeError("认证文件不能是符号链接")
    parts = Path(args.relay_public_key).read_text().strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in parts[1]):
        raise ValueError("需要单行ED25519公钥")
    key = ('restrict,port-forwarding,permitopen="127.0.0.1:18767",permitlisten="127.0.0.1:18767",'
           'command="/bin/false" ssh-ed25519 ' + parts[1] + ' ursa-model-api-relay-20261030')
    old_keys = authorized.read_text() if authorized.exists() else ""
    if parts[1] not in old_keys:
        authorized.parent.mkdir(mode=0o700, exist_ok=True)
        authorized.write_text(old_keys.rstrip() + "\n" + key + "\n")
        os.chown(authorized.parent, account.pw_uid, account.pw_gid)
        os.chown(authorized, account.pw_uid, account.pw_gid)
        authorized.chmod(0o600)
    target = Path(args.site).resolve()
    if not target.is_relative_to(Path("/etc/nginx")) or not target.is_file():
        raise ValueError("站点配置必须位于/etc/nginx且已存在")
    original = target.read_bytes()
    source = original.decode()
    include = "    include /etc/nginx/snippets/ursa-model-api-v1.conf;\n"
    anchor = "    location = / {"
    if include not in source:
        if source.count(anchor) != 1 or "listen 443 ssl" not in source:
            raise RuntimeError("HTTPS站点锚点不唯一，未修改")
        source = source.replace(anchor, include + "\n" + anchor, 1)
    snippets = Path("/etc/nginx/snippets/ursa-model-api-v1.conf")
    limits = Path("/etc/nginx/conf.d/ursa-model-api-v1.conf")
    for path in [snippets, limits]:
        if path.exists() and "# USRA Model API v1" not in path.read_text():
            raise RuntimeError("同名配置由其他任务维护，未覆盖")
    backup = Path("/var/backups/ursa-model-api-20261030") / datetime.now().strftime("%Y%m%d_%H%M%S")
    backup.mkdir(mode=0o700, parents=True)
    (backup / "site.conf").write_bytes(original)
    previous = {p: p.read_bytes() if p.exists() else None for p in [snippets, limits]}
    snippets.parent.mkdir(parents=True, exist_ok=True)
    snippets.write_text('''# USRA Model API v1
location ^~ /ursa-model-api/ {
    client_max_body_size 16m;
    client_body_timeout 30s;
    limit_req zone=ursa_model_api_rate burst=20 nodelay;
    limit_req_status 429;
    limit_conn ursa_model_api_connections 4;
    proxy_pass http://127.0.0.1:18767/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
    proxy_connect_timeout 5s;
    proxy_send_timeout 60s;
    proxy_read_timeout 90s;
    proxy_intercept_errors off;
    proxy_buffering off;
    access_log /var/log/nginx/ursa-model-api-access.log ursa_model_api_safe;
}
''')
    limits.write_text('''# USRA Model API v1
limit_req_zone $binary_remote_addr zone=ursa_model_api_rate:1m rate=5r/s;
limit_conn_zone $binary_remote_addr zone=ursa_model_api_connections:1m;
log_format ursa_model_api_safe '$remote_addr $request_method $uri $status $body_bytes_sent';
''')
    target.write_text(source)
    try:
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except Exception:
        target.write_bytes(original)
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(["nginx", "-t"], check=False)
        raise
    record = {"api_prefix": "/ursa-model-api/", "upstream": "127.0.0.1:18767",
              "site_before_sha256": hashlib.sha256(original).hexdigest(),
              "site_after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
              "backup_directory": str(backup), "relay_key_shell_access": False,
              "relay_permitted_port": 18767, "credentials_logged": False}
    (backup / "change-record.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record))


if __name__ == "__main__":
    main()
