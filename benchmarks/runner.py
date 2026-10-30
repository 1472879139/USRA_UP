"""固定工具执行器与独立评测编排；不创建LLM客户端或修改聊天会话。"""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.font_manager import FontProperties

from core.session import Session
from .catalog import audit_case, confined_path, file_hash, load_catalog, reference_root
from .metrics import score_rasters, quality_warnings
from .water import run_water_baseline

ARTIFACTS = ("preview.png", "comparison.png", "water_mask.tif", "mndwi.tif",
             "statistics.csv", "method.json", "report.json")


def comparison_figure(prediction_path, reference_path, output):
    with rasterio.open(prediction_path) as pred, rasterio.open(reference_path) as ref:
        p, r = pred.read(1), ref.read(1)
    color = np.ones((*p.shape, 3), dtype="float32") * .95
    color[(p == 1) & (r == 1)] = [.05, .6, .35]
    color[(p == 1) & (r == 0)] = [.95, .5, .05]
    color[(p == 0) & (r == 1)] = [.8, .15, .55]
    color[p == 255] = [.35, .35, .35]
    font = FontProperties(family=["Microsoft YaHei", "DejaVu Sans"])
    fig = Figure(figsize=(12, 4.5), dpi=120, layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    axes[0].imshow(r, vmin=0, vmax=1, cmap="Blues", interpolation="nearest")
    axes[0].set_title("人工参考答案：蓝色为水体", fontproperties=font)
    axes[1].imshow(color, interpolation="nearest")
    axes[1].set_title("绿：找对；橙：误报；紫：漏检；灰：缺测", fontproperties=font)
    for ax in axes:
        ax.set_axis_off()
    fig.savefig(output)


def run_case(project_root, case_id):
    """返回本次报告与产物目录；未通过前置核验不运行算法。"""
    cases = {case["id"]: case for case in load_catalog()}
    case = cases.get(case_id)
    if not case or not case["enabled"]:
        raise ValueError("该案例尚未接入固定工具与自动评分")
    answers = reference_root(project_root)
    integrity = audit_case(project_root, case, answers)
    if not integrity["inputs_ok"] or not integrity["reference_ok"]:
        raise ValueError("输入或参考答案缺失／版本不匹配，请先查看案例完整性检查")
    session = Session()
    output = confined_path(project_root, session.result_relative_dir)
    # 由固定执行器提供任务环境，不改全局os.environ，避免影响并行会话。
    execution_env = {"URSA_RESULTS_DIR": str(output)}
    input_path = confined_path(project_root, case["inputs"][0]["path"])
    method = run_water_baseline(input_path, execution_env["URSA_RESULTS_DIR"])
    # 工具已完成并写出预测，之后才由独立评分器读取答案。
    ref_path = confined_path(answers, case["reference"]["path"])
    scores = score_rasters(output / "water_mask.tif", ref_path)
    quality = quality_warnings(scores)
    integrity_after = audit_case(project_root, case, answers)
    if not integrity_after["inputs_ok"] or not integrity_after["reference_ok"]:
        raise ValueError("执行过程中输入或答案发生改变，本次结果不可用于评分")
    comparison_figure(output / "water_mask.tif", ref_path, output / "comparison.png")
    percent = lambda value: "无法计算" if value is None else f"{value:.1%}"
    pixel_area = method["pixel_area_m2"]
    if pixel_area is not None:
        error_finding = (f"约{scores['fp'] * pixel_area / 1_000_000:.3f}平方公里被误报为水体，"
                         f"约{scores['fn'] * pixel_area / 1_000_000:.3f}平方公里水体被漏掉（投影面积估算，含缺测惩罚）。")
    else:
        error_finding = (f"整幅图中{scores['fp'] / scores['pixels']:.1%}的位置被误报为水体，"
                         f"{scores['fn'] / scores['pixels']:.1%}的位置漏掉了水体（含缺测惩罚）。")
    report = {"run_id": session.result_subdir, "case_id": case_id,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "result_directory": session.result_relative_dir,
              "engineering_status": "completed", "evaluation_scope": "固定方法验收，不包含真实模型调用的对话全过程",
              "headline": ("当前方法存在明显漏检或误报，请先查看质量提示。" if any(flag.startswith("majority_") for flag in quality["flags"])
                           else "处理和答案对比已完成，效果请结合误报与漏检判断。"),
              "quality_flags": quality["flags"],
              "findings": quality["messages"] + [f"标出的水体中，{percent(scores['precision'])}与人工答案一致。",
                           f"人工答案中的水体找到了{percent(scores['recall'])}。",
                           error_finding,
                           f"预测覆盖{percent(scores['prediction_coverage'])}，水体候选占有效像元{percent(method['water_fraction'])}。"],
              "scores": scores, "method": method, "integrity": integrity_after,
              "source": case["source"], "reference_sha256": case["reference"]["sha256"],
              "reference_encoding": case["reference_encoding"],
              "limitations": [*method["limitations"], case["limitation"],
                              "公开样本的评价结果查看后按回归使用，不能代表未见地区的准确率。",
                              "标签0是有效背景；预测255按错误计入FP/FN，出现缺测时标记评分不完整。",
                              "本页不调用模型；普通对话保留既有DeepSeek配置并可使用已接入工具。"],
              "next_step": "查看橙色误报和紫色漏检位置；下一批在未见样本上比较改进方法。",
              "artifacts": list(ARTIFACTS),
              "artifact_hashes": {name: file_hash(output / name) for name in ARTIFACTS if name != "report.json"}}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report, output
