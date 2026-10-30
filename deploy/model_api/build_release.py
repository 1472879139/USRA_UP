"""只打包独立模型服务源码；不含应用配置、模型权重、影像和凭据。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(destination):
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "model_service").glob("*.py")) + [root / "model_client.py"]
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    manifest = {"api_version": "1.0.0", "release_id": hashlib.sha256(encoded).hexdigest()[:16], "files": hashes}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in files:
            package.write(path, path.relative_to(root).as_posix())
        package.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    report = {**manifest, "zip_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    destination.with_suffix(".manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"release_id": manifest["release_id"], "zip_sha256": report["zip_sha256"], "path": str(destination)}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    build(parser.parse_args().destination)
