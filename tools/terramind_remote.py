"""通过私有回环转发调用TerraMind，按原始返回分类生成通俗交付。"""
import csv
import http.client
import json
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from . import terramind_protocol as protocol
from .prithvi_remote import NoRedirect


def settings(project_root):
    path=Path(project_root)/"gpu.local.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size>16384:
        raise ValueError("GPU配置必须为小型本地JSON文件。")
    data=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data,dict):
        raise ValueError("GPU配置不是对象。")
    value=data.get("terramind")
    if value is None:
        return None
    if not isinstance(value,dict):
        raise ValueError("terramind配置不是对象。")
    if value.get("enabled") is not True:
        return None
    url=value.get("base_url","http://127.0.0.1:18766")
    parsed=urllib.parse.urlsplit(url)
    if (parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or parsed.username or parsed.password
            or parsed.path not in ("","/") or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError("地物GPU仅连接127.0.0.1私有SSH转发地址。")
    timeout=value.get("timeout_seconds",120)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 1<=timeout<=300:
        raise ValueError("GPU请求超时应为1～300秒。")
    return {"base_url":url.rstrip("/"),"timeout_seconds":timeout}


def post(config,content,request_id,input_sha):
    request=urllib.request.Request(config["base_url"]+"/v1/landcover",data=content,method="POST",headers={
        "Content-Type":"image/tiff","X-Ursa-Protocol":protocol.PROTOCOL,"X-Ursa-Request-ID":request_id,
        "X-Ursa-Input-SHA256":input_sha,"X-Ursa-Sensor":"Sentinel-2","X-Ursa-Product-Level":"L2A","X-Ursa-Encoding":"DN/10000"})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=config["timeout_seconds"]) as response:
            if response.status!=200 or response.headers.get_content_type()!="application/zip":
                raise ValueError("地物GPU响应类型或状态不符。")
            content=response.read(protocol.MAX_RESPONSE_BYTES+1)
    except urllib.error.HTTPError as exc:
        meanings={400:"输入未通过地物预检",413:"地物影像超过上限",415:"上传格式不符",429:"地物GPU正在处理其他任务",503:"地物模型推理失败"}
        raise RuntimeError(meanings.get(exc.code,"地物服务异常")+f"（HTTP {exc.code}）；未更换算法或生成成功结果。") from exc
    except (urllib.error.URLError,TimeoutError,socket.timeout,ConnectionError,http.client.HTTPException) as exc:
        raise RuntimeError("未连接到TerraMind服务或超时，请检查18766隧道与地物服务窗口；本次尚无结果。") from exc
    if len(content)>protocol.MAX_RESPONSE_BYTES:
        raise ValueError("地物GPU返回包过大。")
    return content


def findings(rows):
    def percent(value):
        return "少于0.01%" if 0 < value < .01 else f"{value:.2f}%"
    occupied=sorted([row for row in rows if row["code"]!=0 and row["pixels"]],key=lambda x:x["pixels"],reverse=True)
    result=["模型推测的主要地物："+"；".join(f'{row["name"]}约占{percent(row["percent"])}' for row in occupied[:3])+"。"] if occupied else ["本次所有有效像元均未判定地物类别，无法给出可靠的主要地物结论。"]
    if rows[0]["pixels"]:
        result.append(f'另有{rows[0]["pixels"]}个有效像元（{percent(rows[0]["percent"])}）未判定类别，不能当作可靠非目标区。')
    result.append("占比以本图有效输入像元为分母；这是候选类别分布，尚未与独立参考核对准确性。")
    if rows[8]["pixels"]:
        result.append(f'模型将{percent(rows[8]["percent"])}有效区域标为云；薄云和云影仍可能影响地物判断。')
    return result


def save_preview(raw,mask,valid,path):
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import Patch
    rgb=raw[[3,2,1]].transpose(1,2,0).copy()
    for channel in range(3):
        low,high=np.percentile(rgb[...,channel][valid],[2,98])
        rgb[...,channel]=np.clip((rgb[...,channel]-low)/max(float(high-low),1e-6),0,1)**.7
    rgb[~valid]=.65
    palette=np.array([[int(color[i:i+2],16)/255 for i in (1,3,5)] for color in protocol.COLORS])
    picture=palette[np.minimum(mask,9)]
    picture[~valid]=.65
    font=FontProperties(family=["Microsoft YaHei","DejaVu Sans"])
    fig=Figure(figsize=(11,5),dpi=120,layout="constrained")
    FigureCanvasAgg(fig)
    for axis,title,image in zip(fig.subplots(1,2),["输入影像RGB","TerraMind候选地物类别"],[rgb,picture]):
        axis.imshow(image,interpolation="nearest")
        axis.set_title(title,fontproperties=font)
        axis.axis("off")
    fig.legend(handles=[Patch(facecolor=protocol.COLORS[i],label=f"{i} {name}") for i,name in enumerate(protocol.NAMES)],
               loc="outside lower center",ncols=5,prop=font,fontsize=9)
    fig.savefig(path)


def run_remote_landcover(input_path,results_dir,project_root=None,request_id=None):
    from .io_kit import _get_project_root
    root=Path(project_root or _get_project_root()).resolve()
    source=Path(input_path).resolve();source.relative_to((root/"data").resolve())
    output=Path(results_dir).resolve();output.relative_to((root/"results").resolve())
    config=settings(root)
    if config is None:
        raise ValueError("TerraMind远程工具尚未启用。")
    identity=protocol.inspect_input(source)
    raw,valid,profile=protocol.read_input(source)
    request_id=request_id or uuid.uuid4().hex
    if not isinstance(request_id,str) or not re.fullmatch(r"[0-9a-f]{32}",request_id):
        raise ValueError("地物请求ID格式不符。")
    content=post(config,source.read_bytes(),request_id,identity["sha256"])
    mask,response=protocol.unpack_response(content,request_id,identity["sha256"],profile,valid)
    if protocol.inspect_input(source)!=identity:
        raise ValueError("地物推理期间输入或元数据发生改变，需要重新确认。")
    output.mkdir(parents=True,exist_ok=True)
    (output/"remote_response.zip").write_bytes(content)
    (output/"final_terramind_classification.tif").write_bytes(protocol.encode_mask(mask,profile))
    rows=protocol.statistics(mask,valid)
    with (output/"terramind_class_statistics.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.writer(stream);writer.writerow(["类别编号","类别","像元数","占比_%"])
        writer.writerows([row["code"],row["name"],row["pixels"],row["percent"]] for row in rows)
    save_preview(raw,mask,valid,output/"final_terramind_preview.png")
    result={"tool":protocol.VERSION,"input":source.relative_to(root/"data").as_posix(),"input_identity":identity,
            "request_id":request_id,"response":response,"statistics":rows,"findings":findings(rows),
            "artifacts_sha256":{name:protocol.file_sha(output/name) for name in [
                "remote_response.zip","final_terramind_classification.tif","terramind_class_statistics.csv","final_terramind_preview.png"]}}
    (output/"remote_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print("[远程地物工具] "+source.name+"："+" ".join(result["findings"]))
    return result
