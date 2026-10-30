"""从版本化源码生成无权重、无数据、无密钥的单文件GPU服务包。"""
import argparse
import hashlib
from pathlib import Path
import zipfile

BOOTSTRAP = '''import hashlib
from pathlib import Path
import runpy
import sys
import zipfile
source = Path(sys.argv[0]).resolve()
identity = hashlib.sha256(source.read_bytes()).hexdigest()
root = Path.home()/"ursa-gpu-runtime/services"/("landcover_"+identity[:16])
root.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(source) as bundle:
    names = bundle.namelist()
    if len(names) != 6 or set(names) != {"__main__.py", "service.py", "runtime.py", "terramind_protocol.py", "deploy.py", "landcover.py"}:
        raise ValueError("服务包成员不符。")
    for name in names:
        if name == "__main__.py":
            continue
        target, content = root/name, bundle.read(name)
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise ValueError("已展开的服务源码被改变，保留现场并停止。")
        else:
            with target.open("xb") as handle:
                handle.write(content)
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
sys.argv[0] = str(root/"service.py")
runpy.run_path(str(root/"service.py"), run_name="__main__")
'''


def build(destination):
    root = Path(__file__).resolve().parents[2]
    sources = {"service.py": root/"deploy/terramind_gpu/service.py", "runtime.py": root/"deploy/terramind_gpu/runtime.py",
               "terramind_protocol.py": root/"tools/terramind_protocol.py", "deploy.py":root/"deploy/terramind_gpu/deploy.py",
               "landcover.py":root/"deploy/terramind_gpu/landcover.py"}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("__main__.py", BOOTSTRAP)
        for name, source in sources.items():
            bundle.write(source, name)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print("服务包：", args.output)
    print("SHA-256：", build(args.output))
