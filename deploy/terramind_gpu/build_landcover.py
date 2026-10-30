"""构建只含固定输入与推理源码的自包含GPU执行包，不打包参考图或评分器。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent

BOOTSTRAP = r'''import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import zipfile

source = Path(sys.argv[0]).resolve()
fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
target = Path.home() / "ursa-gpu-runtime" / "services" / ("terramind_lulc_" + fingerprint[:16])
target.mkdir(parents=True, exist_ok=True)
if target.is_symlink():
    raise RuntimeError("执行目录不能是符号链接。")
with zipfile.ZipFile(source) as archive:
    hashes = json.loads(archive.read("payload-sha256.json"))
    if set(archive.namelist()) != set(hashes) | {"__main__.py", "payload-sha256.json"}:
        raise RuntimeError("执行包包含非协议文件。")
    for name, expected in hashes.items():
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts) or ":" in name or "\\" in name:
            raise RuntimeError("执行包路径不合法。")
        data = archive.read(name)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("执行包文件哈希不符。")
        path = target.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != target.parent):
            raise RuntimeError("材料路径不能含符号链接。")
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("已有执行材料被修改；停止，不覆盖。")
        else:
            path.write_bytes(data)
os.environ["URSA_PACKAGE_SHA256"] = fingerprint
sys.path.insert(0, str(target))
sys.argv[0] = str(target / "landcover.py")
runpy.run_path(str(target / "landcover.py"), run_name="__main__")
'''


def build(inputs, output):
    protocol = json.loads((HERE / "landcover-manifest.json").read_text(encoding="utf-8"))
    files = {name: (HERE / name).read_bytes()
             for name in ["deploy.py", "landcover.py", "landcover-manifest.json"]}
    for sample in protocol["samples"]:
        name = sample["id"] + ".tif"
        data = (Path(inputs) / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != sample["sha256"]:
            raise RuntimeError("打包前输入哈希不符：" + name)
        files["inputs/" + name] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    output = Path(output)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", BOOTSTRAP)
        archive.writestr("payload-sha256.json", json.dumps(hashes, indent=2))
        for name, data in files.items():
            archive.writestr(name, data)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(checksum + "  " + output.name + "\n", encoding="utf-8")
    return checksum


def main():
    parser = argparse.ArgumentParser(description="构建无参考答案的TerraMind Small地物执行包")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("执行包SHA256：" + build(args.inputs, args.output))


if __name__ == "__main__":
    main()
