"""远程水体固定计划、角色交接与无答案产物复核；不改变现有通用分析路径。"""
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
import uuid

from core.message import Message, MessageType
from . import prithvi_protocol as protocol
from .prithvi_remote import VERSION, findings, run_remote_water, settings

MANIFEST = "remote_water_workflow.json"


def describe_remote_plan(session, plan):
    session.task_capability["reasons"] = [reason for reason in session.task_capability.get("reasons", [])
                                          if "尚未接入经本系统验证的专业识别模型" not in reason]
    session.task_capability["reasons"].append("已配置固定Prithvi候选水体与指数对照工具；连接在执行时核对，识别准确性须独立评价。")


def confined(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or path.is_symlink():
        raise ValueError("工具文件必须位于本次约定目录，不能使用绝对路径或链接。")
    resolved = path.resolve()
    resolved.relative_to(root)
    return resolved


def select_remote_tool(session, project_root=None):
    from .io_kit import _get_project_root
    root = Path(project_root or _get_project_root()).resolve()
    contract = session.task_contract
    if contract.get("task_type") != "segmentation" or contract.get("targets") != ["水体"]:
        return None
    text = "\n".join([session.user_request, *session.revision_feedback])
    if re.search(r"变化|前后|水深|水质|水位|水温|水量|流量|库容|浊度|叶绿素|岸线|轮廓|"
                 r"矢量|GeoJSON|SHP|行政|边界|裁剪|阈值|NDWI|Otsu|面积|平方|公顷|亩|JPG|JPEG|"
                 r"只用|只要|仅需|只输出|只生成|不要|无需|不需要|不输出|不生成|不保存|[<>≥≤]", text, re.I):
        return None
    names = contract.get("inputs", [])
    if not names or len(names) > 8 or not (root/"data").is_dir() or settings(root) is None:
        return None
    ready, rejected = [], []
    try:
        for name in names:
            path = confined(root / "data", name)
            try:
                identity = protocol.inspect_input(path)
                request_id = uuid.uuid5(uuid.NAMESPACE_URL, session.result_subdir+":"+name+":"+identity["sha256"]).hex
                ready.append({"file_name": name, **identity, "request_id": request_id})
            except protocol.EmptyInputError as exc:
                rejected.append({"file_name": name, "sha256": protocol.file_sha(path),
                                 "metadata_sha256": protocol.file_sha(path.parent/"metadata.json"),
                                 "valid_pixels": 0, "reason": str(exc)})
    except (OSError, ValueError, TypeError):
        return None
    return {"name": VERSION, "version": 1, "inputs": ready, "rejected_inputs": rejected,
            "settings_sha256": protocol.file_sha(root/"gpu.local.json"),
            "scope": "固定Prithvi候选水体与指数对照；分歧不是对错判断；不读取参考答案"}


def confirmation_text(plan):
    """固定工具的确认说明来自实际能力，避免模型另行承诺未实现的处理。"""
    if not plan["inputs"]:
        return "ERROR: 选中的影像均没有有效像元，请补充可用数据；尚未执行分析。"
    names = "、".join(item["file_name"] for item in plan["inputs"])
    text = (f"将分析以下影像：{names}。\n\n"
            "使用已配置的Prithvi模型标出候选水体，保留模型标出的全部候选范围作为主结果；"
            "水体指数只作独立对照，两种方法意见不同的地方另用颜色标出。\n"
            "提供候选水体图、原图像素数量，以及候选水体占可分析区域的比例；"
            "可分析区域指影像中有可用数据的范围。\n"
            "执行时会核对GPU服务连接。本次不读取参考答案，识别准确性尚未验证；"
            "两种方法意见相同或不同都不能直接证明对错，也不能据此判断新增洪水。")
    if plan["rejected_inputs"]:
        rejected = "、".join(item["file_name"] for item in plan["rejected_inputs"])
        text += f"\n以下影像没有有效像元，将记录未处理原因：{rejected}。"
    return text + "\n\n回复 approve 确认，或提出修改意见。"


def tool_message(session, request):
    if request != session.remote_tool_plan or request.get("name") != VERSION or request.get("version") != 1:
        return Message(sender="Engineer", receiver="Manager", msg_type=MessageType.ERROR, content="远程工具计划与本次锁定计划不一致。")
    code = ("import os\nfrom tools.prithvi_workflow import run_remote_workflow\n"
            f"run_remote_workflow({request!r}, os.environ['URSA_RESULTS_DIR'])\n")
    return Message(sender="Engineer", receiver="Executor", msg_type=MessageType.CODE, content=code,
                   metadata={"remote_tool_request": deepcopy(request)})


def verify_rejected(root, items):
    for item in items:
        source = confined(root / "data", item["file_name"])
        if protocol.file_sha(source) != item["sha256"] or protocol.file_sha(source.parent/"metadata.json") != item["metadata_sha256"]:
            raise ValueError("已拒绝输入或元数据发生改变，需要重新规划。")
        try:
            protocol.inspect_input(source)
        except protocol.EmptyInputError:
            continue
        raise ValueError("输入拒绝记录与本次实际输入不一致。")


def run_remote_workflow(plan, results_dir, project_root=None):
    from .io_kit import _get_project_root
    root = Path(project_root or _get_project_root()).resolve()
    if plan.get("name") != VERSION or plan.get("version") != 1 or not plan.get("inputs"):
        raise ValueError("远程固定工具计划不符或没有可分析的输入。")
    if protocol.file_sha(root/"gpu.local.json") != plan["settings_sha256"]:
        raise ValueError("确认后GPU配置已经改变，请重新规划。")
    output = Path(results_dir).resolve()
    output.relative_to((root/"results").resolve())
    verify_rejected(root, plan["rejected_inputs"])
    scenes = []
    for number, item in enumerate(plan["inputs"], 1):
        source = confined(root/"data", item["file_name"])
        identity = {key: item[key] for key in ["sha256", "metadata_sha256", "valid_pixels"]}
        if protocol.inspect_input(source) != identity:
            raise ValueError("确认后影像或来源说明已经改变。")
        folder = f"remote_{number:02}"
        if protocol.file_sha(root/"gpu.local.json") != plan["settings_sha256"]:
            raise ValueError("执行期间GPU配置改变，需要重新规划。")
        result = run_remote_water(source, output/folder, project_root=root, request_id=item["request_id"])
        if protocol.file_sha(root/"gpu.local.json") != plan["settings_sha256"]:
            raise ValueError("执行期间GPU配置改变，本次不能交付。")
        if result["input_identity"] != identity:
            raise ValueError("远程结果未绑定本次锁定输入。")
        scenes.append({"input": item["file_name"], "directory": folder})
    manifest = {"tool": VERSION, "version": 1, "scenes": scenes, "rejected_inputs": plan["rejected_inputs"]}
    output.mkdir(parents=True, exist_ok=True)
    (output/MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return manifest


def attach_remote_audit(session, project_root):
    """重算基线与分歧、核对本任务原始包，不重跑GPU，也不声称语义准确。"""
    import numpy as np
    root = Path(project_root).resolve()
    output = confined(root/"results", session.result_subdir)
    allowed = set(session.result_audit.get("files", []))
    def current(relative):
        path = confined(output, relative)
        if path.relative_to(root/"results").as_posix() not in allowed or not path.is_file():
            raise ValueError("远程产物不在本任务实际生成的核验清单中。")
        return path
    manifest = json.loads(current(MANIFEST).read_text(encoding="utf-8"))
    plan = session.remote_tool_plan
    expected = [{"input": item["file_name"], "directory": f"remote_{number:02}"} for number, item in enumerate(plan["inputs"], 1)]
    if manifest != {"tool": VERSION, "version": 1, "scenes": expected, "rejected_inputs": plan["rejected_inputs"]}:
        raise ValueError("远程清单与锁定计划不一致。")
    verify_rejected(root, plan["rejected_inputs"])
    scenes = []
    for entry, item in zip(manifest["scenes"], plan["inputs"]):
        source = confined(root/"data", entry["input"])
        identity = {key: item[key] for key in ["sha256", "metadata_sha256", "valid_pixels"]}
        if protocol.inspect_input(source) != identity:
            raise ValueError("核验时输入或元数据已变化。")
        folder = entry["directory"]
        result = json.loads(current(folder+"/remote_result.json").read_text(encoding="utf-8"))
        if (result["tool"] != VERSION or result["input"] != entry["input"] or result["input_identity"] != identity or
                result["request_id"] != item["request_id"]):
            raise ValueError("远程产物身份不一致。")
        expected_files = {"final_model_classification.tif", "baseline_classification.tif", "model_class_statistics.csv",
                          "baseline_class_statistics.csv", "final_water_comparison.png", "remote_response.zip"}
        if set(result["artifacts_sha256"]) != expected_files or result["masks"] != {
                "model": "final_model_classification.tif", "baseline": "baseline_classification.tif"} or result["preview"] != "final_water_comparison.png":
            raise ValueError("远程必需产物不完整或被替换。")
        for name, expected_hash in result["artifacts_sha256"].items():
            if protocol.file_sha(current(folder+"/"+name)) != expected_hash:
                raise ValueError("远程产物哈希不一致。")
        raw, valid, profile = protocol.read_input(source)
        masks, response = protocol.unpack_response(current(folder+"/remote_response.zip").read_bytes(), result["request_id"], item["sha256"], raw, valid, profile)
        if result["response"] != response:
            raise ValueError("响应记录与原始返回包不一致。")
        for method, mask_name in [("model", "model_mask.tif"), ("baseline", "baseline_mask.tif")]:
            mask = protocol.decode_mask(current(folder+"/"+result["masks"][method]).read_bytes(), profile, valid)
            if not np.array_equal(mask, masks[mask_name]):
                raise ValueError("本地交付栅格与本次远程返回分类不一致。")
            with current(folder+"/"+method+"_class_statistics.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            expected_rows = [["类别编号", "类别", "像元数", "占比_%"]]
            for value, caption in [(0, "非水体候选"), (1, "水体候选")]:
                count = int(((mask == value) & valid).sum())
                expected_rows.append([str(value), caption, str(count), str(count*100/int(valid.sum()))])
            if rows != expected_rows:
                raise ValueError("远程分类统计与本次原始栅格不一致。")
        facts = protocol.diagnostics(masks["model_mask.tif"], masks["baseline_mask.tif"], valid)
        if facts != result["diagnostics"] or result["findings"] != findings(facts):
            raise ValueError("分歧说明或水体占比与独立重算不一致。")
        if protocol.inspect_input(source) != identity:
            raise ValueError("核验期间输入或元数据发生改变。")
        scenes.append({"input": item["file_name"], "diagnostics": facts, "findings": findings(facts),
                       "preview": session.result_subdir+"/"+folder+"/final_water_comparison.png"})
    session.result_audit["remote_water"] = {"scenes": scenes, "rejected_inputs": plan["rejected_inputs"]}
    session.result_audit["status"] = "needs_review"
    session.result_audit["warnings"] = [warning.replace("聚类和启发式簇命名作为候选分类", "本次模型与指数输出作为候选分类")
                                         for warning in session.result_audit["warnings"]]
    session.result_audit["facts"] = session.result_audit["facts"].replace("聚类和启发式簇命名作为候选分类", "本次模型与指数输出作为候选分类")
    session.result_audit["facts"] += "\n[远程固定工具无答案核验]\n" + "\n".join(scene["input"]+"："+"；".join(scene["findings"]) for scene in scenes)
    session.result_audit["warnings"].append("模型分类的准确性未独立验证；两种方法的分歧只能提示复核。")
    session.tracer.add_event("tool_call", agent="SupervisorAgent", data={"tool_name": "remote_water_audit", "scenes": scenes}, status="warning")


def enrich_delivery(delivery, audit):
    if delivery["completion"] in ("failed", "blocked") or not audit.get("remote_water"):
        return delivery
    remote = audit["remote_water"]
    delivery["headline"] = f"已生成{len(remote['scenes'])}幅影像的候选水体图，并标出两种方法意见不同的区域。"
    delivery["findings"] = [scene["input"]+"："+"；".join(scene["findings"]) for scene in remote["scenes"]]
    for item in remote["rejected_inputs"]:
        delivery["findings"].append(item["file_name"]+"：没有有效像元，本幅未分析，也没有生成假结果。")
    if remote["rejected_inputs"]:
        delivery["headline"] += f"另有{len(remote['rejected_inputs'])}幅因输入无效未处理。"
    delivery["verification"] = "正常分析未读取参考答案。已核对输入、返回包、分类网格、统计及方法分歧；识别准确性尚未验证。"
    delivery["limitations"].append("模型需要匹配的Sentinel-2 L1C输入；方法一致或执行成功不保证识别正确。")
    delivery["next_step"] = "先查看紫色和橙色的分歧区域；有独立标注时，交卷后另行评分。"
    return delivery
