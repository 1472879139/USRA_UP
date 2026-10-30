"""生成代码仅在断网、非root、限制资源的独立Linux容器中执行。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

IMAGE="docker.io/library/ursa-python-runtime:1"
MAX_OUTPUT=64*1024**2


def docker(args,timeout=5):
    return subprocess.run(["docker",*args],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)


def status():
    """只检查现有运行时；分析期间不启动引擎、不拉镜像或安装依赖。"""
    if not shutil.which("docker"):return {"available":False,"reason":"未安装Docker隔离运行时"}
    try:
        result=docker(["image","inspect",IMAGE,"--format","{{json .}}"])
        if result.returncode:raise ValueError("Docker引擎未就绪或缺少ursa-python-runtime:1镜像；请执行部署准备脚本")
        data=json.loads(result.stdout)
        if data.get("Os")!="linux" or data.get("Config",{}).get("Labels",{}).get("org.ursa.runtime")!="python-v1":raise ValueError("本地镜像不符合项目运行时标记")
        return {"available":True,"image_id":data["Id"],"packages":["numpy","rasterio","matplotlib","Pillow"],
                "limits":{"network":"none","cpus":1,"memory":"1g","pids":64,"output_bytes":MAX_OUTPUT,"timeout_seconds":60}}
    except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
        return {"available":False,"reason":str(exc) if isinstance(exc,ValueError) else "Docker引擎没有及时响应；不会退回宿主机执行代码"}


def container_command(image,name,job):
    job=Path(job).resolve()
    if "," in str(job):raise ValueError("任务目录不能含容器挂载分隔符")
    return ["docker","run","--rm","--pull=never","--name",name,"--label","org.ursa.job="+name,
            "--network=none","--read-only","--cap-drop=ALL","--security-opt=no-new-privileges:true",
            "--user=65534:65534","--cpus=1","--memory=1g","--memory-swap=1g","--pids-limit=64",
            "--ulimit","fsize=67108864:67108864","--log-driver=none",
            "--tmpfs","/tmp:rw,noexec,nosuid,size=268435456,mode=1777",
            "--mount",f"type=bind,source={job/'payload'},target=/work/payload,readonly",
            "--mount",f"type=bind,source={job/'output'},target=/work/output",
            "--workdir=/work/output","--env=URSA_RESULTS_DIR=/work/output",
            "--env=URSA_INPUTS_JSON=/work/payload/inputs.json","--env=MPLCONFIGDIR=/tmp/mpl",
            "--env=HOME=/tmp","--env=GDAL_CACHEMAX=64",image,"python","-I","-B","/work/payload/task.py"]


def run(code,inputs,job_dir,*,timeout=60,active=None):
    """复制已批准输入并执行；捕获有界输出、超时/取消和实际镜像身份。"""
    if not isinstance(code,str) or not 0<len(code)<=32000:raise ValueError("Python代码应为1～32000字符")
    if not isinstance(timeout,(int,float)) or not 0<timeout<=60:raise ValueError("单次代码执行上限60秒")
    available=status()
    if not available["available"]:return {"ok":False,"error":{"code":"runtime_unavailable","message":available["reason"]}}
    job=Path(job_dir).resolve();job.mkdir(parents=True,exist_ok=False)
    payload=job/"payload";payload.mkdir();output=job/"output";output.mkdir()
    mapping={}
    for key,path in inputs.items():
        source=Path(path)
        if not key.replace("_","").isalnum() or source.is_symlink() or not source.is_file() or source.stat().st_size>128*1024**2:raise ValueError("代码输入必须是本次已批准的普通文件")
        target=payload/(key+source.suffix.lower());shutil.copyfile(source,target)
        mapping[key]="/work/payload/"+target.name
    (payload/"inputs.json").write_text(json.dumps(mapping,ensure_ascii=False),encoding="utf-8")
    (payload/"task.py").write_text(code,encoding="utf-8")
    # Linux宿主也允许容器内非root用户只读输入、写本任务输出。
    for path in payload.iterdir():path.chmod(0o644)
    payload.chmod(0o755);output.chmod(0o777)
    name="ursa-python-"+uuid.uuid4().hex
    command=container_command(available["image_id"],name,job)
    buffers=[bytearray(),bytearray()];overflow=threading.Event()
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    def drain(stream,target):
        while True:
            block=stream.read(4096)
            if not block:break
            remaining=65536-len(target)
            target.extend(block[:max(0,remaining)])
            if len(block)>remaining:overflow.set()
    readers=[threading.Thread(target=drain,args=(stream,buffer),daemon=True) for stream,buffer in zip((process.stdout,process.stderr),buffers)]
    for thread in readers:thread.start()
    started=time.monotonic();failure=None
    try:
        while process.poll() is None:
            if active is not None and not active():failure="用户已终止代码执行";break
            if time.monotonic()-started>timeout:failure="代码执行超时";break
            if overflow.is_set():failure="代码日志超过64KiB上限";break
            total,count=0,0
            for path in output.rglob("*"):
                count+=1
                if path.is_symlink():failure="代码输出包含链接";break
                if path.is_file():total+=path.stat().st_size
                if count>128 or total>MAX_OUTPUT:failure="代码产物超过数量或64MiB上限";break
            if failure:break
            time.sleep(.1)
    finally:
        if failure or process.poll() is None:
            try:docker(["rm","-f",name])
            except (OSError,subprocess.TimeoutExpired):pass
        try:process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=3)
        for thread in readers:thread.join(2)
    if overflow.is_set() and failure is None:failure="代码日志超过64KiB上限"
    files=list(output.rglob("*"))
    if any(path.is_symlink() for path in files) or len(files)>128 or sum(path.stat().st_size for path in files if path.is_file())>MAX_OUTPUT:
        failure=failure or "代码产物超过上限或包含链接"
    stdout,stderr=[bytes(value).decode("utf-8",errors="replace") for value in buffers]
    (job/"stdout.txt").write_text(stdout,encoding="utf-8");(job/"stderr.txt").write_text(stderr,encoding="utf-8")
    result={"ok":failure is None and process.returncode==0,"exit_code":process.returncode,"stdout":stdout,"stderr":stderr,
            "duration_seconds":round(time.monotonic()-started,3),"image_id":available["image_id"],"output_dir":str(output)}
    if not result["ok"]:result["error"]={"code":"python_execution_failed","message":failure or "代码异常退出，请根据stderr修复代码"}
    (job/"execution.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result
