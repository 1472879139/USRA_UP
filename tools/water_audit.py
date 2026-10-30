"""固定工具无答案核验；只检查本次产物与输入，不导入评分清单。"""
import json
import math
from pathlib import Path

from .artifact_paths import confined_path, file_hash
from .water_workflow import MANIFEST, TOOL_VERSIONS
from .water_tool import VERSION
from .water_methods import METHODS, read_index, otsu_diagnostics


def _current_file(session, project_root, relative):
    name = session.result_subdir + "/" + relative
    if name not in session.result_audit.get("files", []):
        raise ValueError("固定工具产物不在本次执行清单内")
    path = confined_path(Path(project_root)/session.result_relative_dir, relative)
    if not path.is_file():
        raise ValueError("固定工具产物不存在")
    return path


def attach_water_audit(session, project_root):
    """核对工具清单与当前文件后，追加逐幅产物事实。"""
    if session.tool_plan.get("name") not in TOOL_VERSIONS:
        return
    manifest_path = _current_file(session, project_root, MANIFEST)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("固定工具清单过大")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("固定工具清单必须为JSON对象")
    inputs = session.tool_plan["inputs"]
    if (manifest.get("tool") != session.tool_plan["name"] or manifest.get("version") != session.tool_plan.get("version") or
            len(manifest.get("scenes", [])) != len(inputs)):
        raise ValueError("固定工具清单与锁定计划不一致")
    scenes = []
    for entry, planned in zip(manifest["scenes"], inputs):
        if entry["input"] != planned["file_name"] or entry["input_sha256"] != planned["sha256"]:
            raise ValueError("本次产物与锁定输入不一致")
        prediction = _current_file(session, project_root, entry["prediction"])
        _current_file(session, project_root, entry["statistics"])
        _current_file(session, project_root, entry["preview"])
        if file_hash(prediction) != entry["prediction_sha256"]:
            raise ValueError("固定工具预测在执行后发生改变")
        method = entry["method"]
        method_id = planned.get("method", "mndwi_zero")
        if method_id not in METHODS or method.get("tool_version")!=METHODS[method_id]["version"]:
            raise ValueError("实际方法版本与计划不一致")
        expected_index, valid = read_index(confined_path(Path(project_root)/"data", planned["file_name"]))
        threshold = 0 if method_id=="mndwi_zero" else otsu_diagnostics(expected_index, valid)["threshold"]
        if threshold is None or method.get("threshold")!=threshold or method.get("threshold_rule")!=">":
            raise ValueError("实际阈值与冻结方法不一致")
        # 从栅格重新计算关键统计，不把清单中的数字本身当作核验依据。
        import numpy as np
        import rasterio
        with rasterio.open(prediction) as src:
            data = src.read(1)
            expected = np.full(expected_index.shape, 255, dtype="uint8")
            expected[valid] = (expected_index[valid]>threshold).astype("uint8")
            if not np.array_equal(data, expected):
                raise ValueError("预测像元与声明方法、阈值不一致")
            if not np.isin(data, [0, 1, 255]).all():
                raise ValueError("固定工具栅格含未声明类别")
            if (int((data == 1).sum()) != method["water_pixels"] or
                    int((data != 255).sum()) != method["valid_pixels"] or
                    int(data.size) != method["total_pixels"]):
                raise ValueError("固定工具清单统计与分类栅格不一致")
            with rasterio.open(confined_path(Path(project_root)/"data", planned["file_name"])) as source:
                if (src.shape != source.shape or src.crs != source.crs or
                        not src.transform.almost_equals(source.transform, precision=1e-8)):
                    raise ValueError("固定工具分类栅格与输入网格不一致")
            expected_fraction = method["water_pixels"] / method["valid_pixels"] if method["valid_pixels"] else None
            expected_area = None
            expected_pixel_area = None
            if src.crs and src.crs.is_projected and src.crs.linear_units in ("metre", "meter"):
                transform = src.transform
                expected_pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
                expected_area = method["water_pixels"] * expected_pixel_area
            for actual, expected in ((method["water_fraction"], expected_fraction), (method["area_m2"], expected_area),
                                     (method["pixel_area_m2"], expected_pixel_area)):
                if ((actual is None) != (expected is None) or
                        (expected is not None and not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9))):
                    raise ValueError("固定工具面积或占比与栅格不一致")
        fraction = method["water_fraction"]
        area = method["area_m2"]
        area_text = f"标出的水面候选约{area / 1_000_000:.3f}平方公里，" if area is not None else "当前坐标条件不足，无法可靠换算水面面积；"
        findings = [area_text + (f"占可分析区域{fraction:.1%}。" if fraction is not None else "可分析区域不足，占比无法计算。")]
        missing = method["total_pixels"] - method["valid_pixels"]
        quality_flags = ["incomplete_prediction"] if missing else []
        if missing:
            findings.append(f"影像有{missing:,}个位置未确认，识别结果不完整。")
        findings.append("本次只检查产物、网格与统计一致性；没有读取参考答案，识别准确性尚未验证。")
        evaluation = {"status": "unavailable", "reason": "正常分析不读取人工答案，需交卷后独立测评。"}
        scenes.append({"label": entry["label"], "input": entry["input"], **method,
                       "quality_flags": quality_flags,
                       "findings": findings, "evaluation": evaluation,
                       "prediction_path": session.result_subdir + "/" + entry["prediction"],
                       "preview_path": session.result_subdir + "/" + entry["preview"]})
    session.result_audit["scenes"] = scenes
    session.result_audit["facts"] += "\n\n[固定工具逐幅无答案核验]\n使用绿色B3与短波红外B11计算MNDWI；预览为输入影像与候选图的左右对照。\n" + "\n".join(
        scene["label"] + f"：实际方法版本{scene['tool_version']}，阈值>{scene['threshold']}；" + "；".join(scene["findings"]) for scene in scenes)
    session.tracer.add_event("tool_call", agent="SupervisorAgent",
                            data={"tool_name": "water_artifact_audit", "scenes": scenes},
                            status="warning")
