"""维护者将独立MCP令牌映射至同身份API凭据，不输出认证令牌。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from .service import valid_url


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def issue(identity_file, api_connection, client_file, owner, public_mcp_url, scopes=("read", "submit", "cancel")):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", owner):
        raise ValueError("调用身份名称无效")
    target, store = Path(client_file), Path(identity_file)
    if target.exists():
        raise FileExistsError("客户端私有文件已存在，未覆盖")
    records = json.loads(store.read_text(encoding="utf-8-sig")) if store.exists() else {"identities": []}
    if any(r["owner"] == owner for r in records["identities"]):
        raise ValueError("同名身份已经配置，未擅自轮换令牌")
    api = json.loads(Path(api_connection).read_text(encoding="utf-8-sig"))
    key = api["api_key"]
    if not isinstance(key, str) or not key or any(c in key for c in "\r\n"):
        raise ValueError("模型API凭据无效")
    if any(r["api_key"] == key for r in records["identities"]):
        raise ValueError("该API身份已经映射，不能让不同调用身份共用内部密钥")
    token = "ursa_mcp_" + secrets.token_urlsafe(32)
    records["identities"].append({"owner": owner, "enabled": True, "scopes": list(scopes),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "api_key": key})
    connection = {"mcp_url": valid_url(public_mcp_url), "transport": "streamable-http", "mcp_token": token,
                  "api_base_url": valid_url(api["base_url"]), "api_key": key, "owner": owner,
                  "auth_mode": "controlled_bearer"}
    write_private(store, records)
    write_private(target, connection)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--api-connection", required=True)
    parser.add_argument("--client-file", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--public-mcp-url", required=True)
    args = parser.parse_args()
    issue(args.identity_file, args.api_connection, args.client_file, args.owner, args.public_mcp_url)
    print("MCP身份已配置，客户端凭据已写入私有文件；未打印令牌。")


if __name__ == "__main__":
    main()
