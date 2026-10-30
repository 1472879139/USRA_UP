"""地物分类的固定远程计划、角色交接与无答案核验。"""
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
import uuid
from urllib.parse import quote
from core.message import Message,MessageType
from . import terramind_protocol as protocol
from . import terramind_remote as remote
from .prithvi_workflow import confined

MANIFEST="terramind_workflow.json"


def suitable_request(session):
    text="\n".join([session.user_request,*session.revision_feedback])
    return (session.task_contract.get("task_type")=="segmentation" and not session.task_contract.get("targets")
            and bool(re.search(r"地物|地表覆盖|土地覆盖|土地利用",text))
            and not re.search(r"变化|温度|去云|训练|微调|矢量|GeoJSON|SHP|面积|公顷|平方米|行政|裁剪|阈值|只要|只输出|不要|无需",text,re.I))


def rejected_identity(root,name):
    identity={"file_name":name,"sha256":None,"metadata_sha256":None}
    try:
        path=confined(root/"data",name)
        if path.is_file():
            identity["sha256"]=protocol.file_sha(path)
        metadata=path.parent/"metadata.json"
        if metadata.is_file() and not metadata.is_symlink():
            identity["metadata_sha256"]=protocol.file_sha(metadata)
    except (OSError,ValueError):
        pass
    return identity


def select_remote_tool(session,project_root=None):
    from .io_kit import _get_project_root
    if not suitable_request(session):
        return None
    root=Path(project_root or _get_project_root()).resolve()
    if remote.settings(root) is None:
        return None
    names=session.task_contract.get("inputs",[])
    if not names or len(names)>8:
        return None
    ready,rejected=[],[]
    for name in names:
        try:
            path=confined(root/"data",name)
            identity=protocol.inspect_input(path)
            ready.append({"file_name":name,**identity,"request_id":uuid.uuid5(uuid.NAMESPACE_URL,
                          session.result_subdir+":"+name+":"+identity["sha256"]+":"+protocol.VERSION).hex})
        except (ValueError,OSError,TypeError) as exc:
            rejected.append({**rejected_identity(root,name),"reason":str(exc)})
    return {"name":protocol.VERSION,"version":1,"inputs":ready,"rejected_inputs":rejected,
            "settings_sha256":protocol.file_sha(root/"gpu.local.json"),
            "scope":"固定TerraMind地物候选分类；不读取答案，不保证未知场景准确性"}


def describe_remote_plan(session,plan):
    session.task_capability["reasons"]=[reason for reason in session.task_capability.get("reasons",[]) if "专业识别模型" not in reason]
    session.task_capability["reasons"].append("已配置TerraMind Small地物生成工具；输入须通过固定契约，结果作为候选类别，准确性需交卷后独立评价。")


def confirmation_text(plan):
    if not plan["inputs"]:
        return "ERROR: 选中输入均不符合地物模型条件："+ "；".join(item["file_name"]+"："+item["reason"] for item in plan["rejected_inputs"])
    text=("将使用TerraMind Small为"+ "、".join(item["file_name"] for item in plan["inputs"])+"生成地物候选分类图，"
          "说明主要类别及其占有效区域的比例，并提供分类栅格、统计表和可查看的图例。"
          "模型执行时通过私有隧道连接GPU；不读取参考答案，不承诺识别准确率。"
          "当前只支持224×224的Sentinel-2 L2A十二波段原始DN；不会把其他数据静默裁剪或改换模型。")
    if plan["rejected_inputs"]:
        text+="以下输入未处理："+ "；".join(item["file_name"]+"："+item["reason"] for item in plan["rejected_inputs"])
    return text+"\n\n回复 approve 确认，或提出修改意见。"


def tool_message(session,request):
    if request!=session.remote_tool_plan or request.get("name")!=protocol.VERSION or request.get("version")!=1:
        return Message(sender="Engineer",receiver="Manager",msg_type=MessageType.ERROR,content="地物远程计划与本次锁定计划不一致。")
    code=("import os\nfrom tools.terramind_workflow import run_remote_workflow\n"
          f"run_remote_workflow({request!r}, os.environ['URSA_RESULTS_DIR'])\n")
    return Message(sender="Engineer",receiver="Executor",msg_type=MessageType.CODE,content=code,
                   metadata={"remote_tool_request":deepcopy(request)})


def verify_rejected(root,items):
    for item in items:
        if rejected_identity(root,item["file_name"])!={key:item[key] for key in ["file_name","sha256","metadata_sha256"]}:
            raise ValueError("已拒绝的地物输入发生改变，需要重新规划。")
        try:
            protocol.inspect_input(confined(root/"data",item["file_name"]))
        except (OSError,ValueError,TypeError):
            continue
        raise ValueError("原拒绝输入现在可用，需要重新确认，不能忽略。")


def run_remote_workflow(plan,results_dir,project_root=None):
    from .io_kit import _get_project_root
    root=Path(project_root or _get_project_root()).resolve()
    output=Path(results_dir).resolve();output.relative_to((root/"results").resolve())
    if plan.get("name")!=protocol.VERSION or plan.get("version")!=1 or not plan.get("inputs"):
        raise ValueError("地物固定计划不符或无可用输入。")
    if protocol.file_sha(root/"gpu.local.json")!=plan["settings_sha256"]:
        raise ValueError("GPU配置发生改变，需要重新确认。")
    verify_rejected(root,plan["rejected_inputs"])
    entries=[]
    for number,item in enumerate(plan["inputs"],1):
        source=confined(root/"data",item["file_name"])
        identity=protocol.inspect_input(source)
        if identity!={key:item[key] for key in ["sha256","metadata_sha256","valid_pixels"]}:
            raise ValueError("锁定后地物输入或元数据改变。")
        directory=f"landcover_{number:02}"
        remote.run_remote_landcover(source,output/directory,root,item["request_id"])
        entries.append({"input":item["file_name"],"directory":directory})
    (output/MANIFEST).write_text(json.dumps({"tool":protocol.VERSION,"version":1,"scenes":entries,
        "rejected_inputs":plan["rejected_inputs"]},ensure_ascii=False,indent=2),encoding="utf-8")


def attach_remote_audit(session,project_root):
    import numpy as np
    root=Path(project_root).resolve();output=confined(root/"results",session.result_subdir)
    allowed=set(session.result_audit.get("files",[]))
    def current(name):
        path=confined(output,name)
        if path.relative_to(root/"results").as_posix() not in allowed or not path.is_file():
            raise ValueError("地物产物不在本次实际生成的核验清单。")
        return path
    plan=session.remote_tool_plan
    manifest=json.loads(current(MANIFEST).read_text(encoding="utf-8"))
    entries=[{"input":item["file_name"],"directory":f"landcover_{n:02}"} for n,item in enumerate(plan["inputs"],1)]
    if manifest!={"tool":protocol.VERSION,"version":1,"scenes":entries,"rejected_inputs":plan["rejected_inputs"]}:
        raise ValueError("地物任务清单与锁定计划不符。")
    verify_rejected(root,plan["rejected_inputs"])
    scenes=[]
    for entry,item in zip(entries,plan["inputs"]):
        source=confined(root/"data",entry["input"]);identity=protocol.inspect_input(source)
        if identity!={key:item[key] for key in ["sha256","metadata_sha256","valid_pixels"]}:
            raise ValueError("核验时地物输入改变。")
        directory=entry["directory"]
        result=json.loads(current(directory+"/remote_result.json").read_text(encoding="utf-8"))
        if (result["tool"]!=protocol.VERSION or result["input"]!=entry["input"] or result["input_identity"]!=identity
                or result["request_id"]!=item["request_id"]):
            raise ValueError("地物产物身份不符。")
        names={"remote_response.zip","final_terramind_classification.tif","terramind_class_statistics.csv","final_terramind_preview.png"}
        if set(result["artifacts_sha256"])!=names:
            raise ValueError("地物必需产物不完整。")
        for name,digest in result["artifacts_sha256"].items():
            if protocol.file_sha(current(directory+"/"+name))!=digest:
                raise ValueError("地物产物哈希不符。")
        raw,valid,profile=protocol.read_input(source)
        mask,response=protocol.unpack_response(current(directory+"/remote_response.zip").read_bytes(),
                                               item["request_id"],item["sha256"],profile,valid)
        delivered=protocol.decode_mask(current(directory+"/final_terramind_classification.tif").read_bytes(),profile,valid)
        rows=protocol.statistics(mask,valid)
        if not np.array_equal(mask,delivered) or result["response"]!=response or result["statistics"]!=rows or result["findings"]!=remote.findings(rows):
            raise ValueError("地物分类、统计或说明与原始GPU返回包不一致。")
        with current(directory+"/terramind_class_statistics.csv").open(encoding="utf-8-sig",newline="") as stream:
            values=list(csv.reader(stream))
        if values!=[["类别编号","类别","像元数","占比_%"]]+[[str(row["code"]),row["name"],str(row["pixels"]),str(row["percent"])] for row in rows]:
            raise ValueError("地物CSV与原始分类重算不一致。")
        if protocol.inspect_input(source)!=identity:
            raise ValueError("地物核验期间输入改变。")
        scenes.append({"input":entry["input"],"findings":remote.findings(rows),"statistics":rows,
                       "preview":session.result_subdir+"/"+directory+"/final_terramind_preview.png"})
    session.result_audit["remote_landcover"]={"scenes":scenes,"rejected_inputs":plan["rejected_inputs"]}
    session.result_audit["status"]="needs_review"
    session.result_audit["facts"]+="\n[TerraMind地物无答案核验]\n"+"\n".join(scene["input"]+"："+"；".join(scene["findings"]) for scene in scenes)
    session.result_audit["warnings"].append("地物结果是生成式候选分类；没有读取参考答案，不能据此给出准确率。")
    session.tracer.add_event("tool_call",agent="SupervisorAgent",data={"tool_name":"terramind_landcover_audit","scene_count":len(scenes)},status="warning")


def enrich_delivery(delivery,audit):
    if delivery["completion"] in ("failed","blocked") or not audit.get("remote_landcover"):
        return delivery
    value=audit["remote_landcover"]
    delivery["headline"]=f'已生成{len(value["scenes"])}幅影像的地物候选分类图和主要类别占比。'
    delivery["findings"]=[scene["input"]+"："+"；".join(scene["findings"]) for scene in value["scenes"]]
    delivery["scene_results"]=[{"label":scene["input"],"findings":scene["findings"],"preview":next(
        (file for file in delivery["files"] if file["url"].endswith(quote(scene["preview"],safe=""))),None)} for scene in value["scenes"]]
    if value["rejected_inputs"]:
        delivery["headline"]+=f'另有{len(value["rejected_inputs"])}幅输入不符合条件，未处理。'
        delivery["findings"] += [item["file_name"]+"："+item["reason"] for item in value["rejected_inputs"]]
    delivery["verification"]="正常分析未读取参考答案。已核对模型身份、输入、分类网格、CSV和原始返回包；识别准确性尚未独立验证。"
    delivery["limitations"].append("生成式候选图可能混淆农作物、草灌、裸地或小斑块；不能把此前五例分数套用到本次新影像。")
    delivery["next_step"]="先查看分类图及图例；需要精确识别时，封存本次结果后使用外部参考独立评价。"
    return delivery
