"""普通水体需求的固定工具契约、计划和执行；生成阶段不读取参考答案。"""
import csv
import json
import re
from pathlib import Path

from core.message import Message, MessageType
from .artifact_paths import confined_path, file_hash
from .task_contract import _intent_text
from .water_tool import VERSION, run_water_baseline
from . import water_methods

TOOL_NAME = "water_mndwi_zero"
AUTO_TOOL_NAME = "water_mndwi_auto"
TOOL_VERSIONS = {TOOL_NAME: VERSION, AUTO_TOOL_NAME: water_methods.ROUTER_VERSION}
MANIFEST = "water_workflow.json"


def is_basic_water_request(session):
    contract = session.task_contract
    text = _intent_text("\n".join([session.user_request, *session.revision_feedback]))
    if contract.get("task_type") != "segmentation" or contract.get("targets") != ["水体"]:
        return False
    # 额外方法、范围和交付要求不能被默认工具悄悄覆盖。
    return not re.search(r"NDWI|MNDWI|Otsu|阈值|模型|深度|双指数|交集|保守|严格|最小|斑块|"
                         r"指定|按照|采用|行政|流域|边界|裁剪|缓冲|比较|对比|变化|前后|"
                         r"水深|水质|水位|水温|水量|库容|流量|浊度|叶绿素|宽度|长度|"
                         r"矢量|GeoJSON|shapefile|\bSHP\b|岸线|轮廓|"
                         r"只要|只用|仅需|不要|不需要|无需|不输出|[<>≥≤]", text, re.I)


def inspect_supported_input(path):
    """只接收声明了Sentinel-2六波段及DN编码的输入，不能靠B11猜传感器。"""
    import rasterio
    sidecar = path.parent / "metadata.json"
    if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 256 * 1024:
        raise ValueError("缺少可核对的本地波段与编码说明 metadata.json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8-sig"))
    if not isinstance(metadata, dict):
        raise ValueError("波段说明必须为JSON对象")
    if not re.search(r"Sentinel[-_ ]?2", str(metadata.get("dataset", "")), re.I):
        raise ValueError("没有声明Sentinel-2数据来源，不能把B11猜成短波红外")
    if "反射率DN/10000" not in str(metadata.get("encoding", "")).replace(" ", ""):
        raise ValueError("尚未核对本工具需要的DN/10000反射率编码")
    normalize = lambda name: re.sub(r"^B0+", "B", str(name or "").upper())
    with rasterio.open(path) as src:
        names = [normalize(name) for name in src.descriptions]
        declared = [normalize(name) for name in metadata.get("bands", [])]
        if (set(names) != {"B2", "B3", "B4", "B8", "B11", "B12"} or
                len(names) != 6 or names != declared):
            raise ValueError("实际波段与六波段语义说明不一致")
        if src.width * src.height > 4_000_000:
            raise ValueError("当前固定工具只支持不超过400万像元的小型影像")
    return {"sha256": file_hash(path), "metadata_sha256": file_hash(sidecar)}


def select_water_tool(session, policy=None):
    """已安装工具的确定性适用性分流；不改用户原始需求，不按文件名挑算法。"""
    if not is_basic_water_request(session):
        return None
    names = session.task_contract.get("inputs", [])
    root = session.data_context.get("data_root")
    if not root or not names or len(names) > 8:
        return None
    inputs = []
    policy = policy or ("auto" if water_methods.AUTO_ENABLED else "mndwi_zero")
    if policy not in ("auto", *water_methods.METHODS):
        raise ValueError("未知水体选路策略")
    try:
        for name in names:
            path = confined_path(root, name)
            identity = inspect_supported_input(path)
            selection = (water_methods.choose_method(path) if policy=="auto" else
                         {"method": policy, "reason": "使用锁定版本的候选方法", "diagnostics": {}})
            method_id = selection["method"]
            inputs.append({"file_name": name, **identity, "method": method_id,
                           "method_version": water_methods.METHODS[method_id]["version"], "selection": selection})
    except (OSError, ValueError, TypeError):
        return None
    tool = TOOL_NAME if all(item["method"]=="mndwi_zero" for item in inputs) else AUTO_TOOL_NAME
    return {"name": tool, "version": TOOL_VERSIONS[tool], "inputs": inputs, "policy": policy,
            "scope": "已安装水体候选方法；适用性检查不代表精度，交卷后独立评价"}


def tool_code_message(session, request):
    """Engineer构建固定包装代码；消息中的计划必须等于Scientist锁定计划。"""
    if request != session.tool_plan or request.get("name") not in TOOL_VERSIONS or request.get("version") != TOOL_VERSIONS.get(request.get("name")):
        return Message(sender="Engineer", receiver="Manager", msg_type=MessageType.ERROR,
                       content="工具计划与本次锁定任务不一致，请返回规划环节重新检查。")
    code = ("import os\nfrom tools.water_workflow import run_water_workflow\n"
            f"run_water_workflow({request!r}, os.environ['URSA_RESULTS_DIR'])\n")
    return Message(sender="Engineer", receiver="Executor", content=code, msg_type=MessageType.CODE,
                   metadata={"tool_request": request})


def run_water_workflow(plan, results_dir):
    """由Executor子进程调用，固定配方只读取锁定输入与其元数据，统一写本任务目录。"""
    if plan.get("name") not in TOOL_VERSIONS or plan.get("version") != TOOL_VERSIONS.get(plan.get("name")):
        raise ValueError("工具名称或版本不匹配，不能执行")
    root = Path.cwd()
    output = Path(results_dir).resolve()
    output.relative_to((root / "results").resolve())
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, item in enumerate(plan["inputs"], 1):
        path = confined_path(root / "data", item["file_name"])
        if inspect_supported_input(path) != {key: item[key] for key in ("sha256", "metadata_sha256")}:
            raise ValueError("确认后的影像或编码说明已变化，需要重新规划")
        folder = output / f"input_{i:02}"
        method_id = item.get("method", "mndwi_zero")
        if method_id not in water_methods.METHODS or (plan["name"]==TOOL_NAME and method_id!="mndwi_zero"):
            raise ValueError("场景方法与锁定工具不一致")
        if item.get("method_version", VERSION) != water_methods.METHODS[method_id]["version"]:
            raise ValueError("场景方法版本不一致")
        if plan.get("policy")=="auto" and item.get("selection")!=water_methods.choose_method(path):
            raise ValueError("输入选路事实与锁定计划不一致")
        method = water_methods.run_method(method_id, path, folder)
        if "selection" in item:
            method["selection"] = item["selection"]
            (folder/"method.json").write_text(json.dumps(method, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if inspect_supported_input(path) != {key: item[key] for key in ("sha256", "metadata_sha256")}:
            raise ValueError("执行过程中影像或编码说明发生改变，本次结果不能交付")
        label = f"{path.parent.name}/{path.name}"
        stem = f"scene_{i:02}"
        renamed = {}
        for name in ("water_mask.tif", "mndwi.tif", "preview.png", "statistics.csv", "method.json"):
            target = ("final_" if name in ("water_mask.tif", "preview.png") else "") + stem + "_" + name
            (folder / name).replace(folder / target)
            renamed[name] = target
        import numpy as np
        import rasterio
        prediction = folder / renamed["water_mask.tif"]
        with rasterio.open(prediction) as src:
            values, counts = np.unique(src.read(1), return_counts=True)
        table = folder / f"{stem}_class_statistics.csv"
        with table.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            area_available = method["pixel_area_m2"] is not None
            columns = ["类别编号", "类别", "像元数", "占比_%", "分类栅格"]
            if area_available:
                columns.append("面积_km2")
            writer.writerow(columns)
            for value, count in zip(values.tolist(), counts.tolist()):
                row = [value, {0: "非水候选", 1: "水体候选", 255: "无效或未确认"}[value],
                       count, count * 100 / method["total_pixels"], prediction.name]
                if area_available:
                    row.append(count * method["pixel_area_m2"] / 1_000_000)
                writer.writerow(row)
        entries.append({"input": item["file_name"], "input_sha256": item["sha256"], "label": label,
                        "prediction": prediction.relative_to(output).as_posix(),
                        "prediction_sha256": file_hash(prediction),
                        "statistics": table.relative_to(output).as_posix(),
                        "preview": (folder/renamed["preview.png"]).relative_to(output).as_posix(),
                        "method": method})
        print(f"[固定工具] {label}：水体候选{method['water_pixels']}像元；占有效像元{method['water_fraction']}; 目录{folder}")
    manifest = {"tool": plan["name"], "version": plan["version"], "scenes": entries}
    (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("本次固定工具清单：", output / MANIFEST)
    return manifest
