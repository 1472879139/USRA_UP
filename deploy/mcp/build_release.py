"""仅打包明确的MCP源码与锁文件，不携带配置、影像或模型权重。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output):
    root = Path(__file__).resolve().parents[2]
    files = [*sorted((root / "ursa_mcp").glob("*.py")), root / "requirements-mcp.lock"]
    content = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    release_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    manifest = {"service": "ursa_mcp", "release_id": release_id, "files": hashes}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("发布包已存在，未覆盖")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
        for name, data in content.items():
            package.writestr(name, data)
        package.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    return {"release_id": release_id, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "file_count": len(content)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    print(json.dumps(build(parser.parse_args().output)))
