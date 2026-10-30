"""显式准备独立Python容器；正常分析不会下载或构建镜像。"""
from pathlib import Path
import subprocess
import sys

if __name__=="__main__":
    directory=Path(__file__).resolve().parent
    result=subprocess.run(["docker","build","--tag","docker.io/library/ursa-python-runtime:1",str(directory)])
    if result.returncode:raise SystemExit(result.returncode)
    sys.path.insert(0,str(directory.parents[1]))
    from tools.python_runtime import status
    check=status();print(check)
    if not check["available"]:raise SystemExit("隔离运行时尚未就绪")
