"""交卷后外部评分；只读答卷与参考答案，不导入角色、不触发重跑。"""
import argparse
import json
from pathlib import Path

from tools.artifact_paths import confined_path
from tools.submission import verify_submission
from tools.result_delivery import render_plain_delivery


def grade_submission(directory, expected_hash, project_root):
    """评分仅返回独立报告，失败及不可评分任务仍保留，绝不写回Session。"""
    submission = verify_submission(directory, expected_hash)
    report = {"task_id": submission["task_id"], "submission_sha256": expected_hash,
              "status": "not_completed" if submission["outcome"] != "completed" else "unscored",
              "request": submission["request"], "stop_reason": submission["stop_reason"],
              "system_answer": render_plain_delivery(submission["delivery"]) + "\n\n[专业详情]\n" + submission["delivery"].get("professional_report", ""),
              "scope": "冻结答卷的外部评价；不反馈到本次任务，不代表真实模型理解能力已全面验证。",
              "isolation": submission["isolation"], "scenes": [],
              "engineering": {"outcome": submission["outcome"], "missing_artifacts": submission["missing_artifacts"],
                              "trace_summary": submission["trace_summary"]},
              "review_items": ["需求是否完整完成、方法是否合适、文字解释是否准确，仍需独立评审；像元分数不能代替这些评价。"]}
    if submission["outcome"] == "completed":
        from .known_prediction import evaluate_known_prediction
        from .metrics import quality_warnings
        for scene in submission["scenes"]:
            prediction = confined_path(directory, scene["prediction"])
            if scene.get('task_type') == 'lst':
                from .lst_metrics import evaluate_temperature
                from tools.lst_tool import VERSION
                evaluation = (evaluate_temperature(project_root, scene['input_sha256'], prediction)
                              if scene.get('tool_version') == VERSION else
                              {'status': 'unavailable', 'reason': '温度工具版本尚未接入外部评分'})
            else:
                evaluation = evaluate_known_prediction(project_root, scene["input_sha256"], prediction)
            scores = evaluation.get("scores")
            quality = ({'flags': evaluation.get('quality_flags', []), 'messages': evaluation['findings']}
                       if scores and scores.get('metric_type') == 'temperature' else
                       quality_warnings(scores) if scores else {"flags": [], "messages": [evaluation["reason"]]})
            report["scenes"].append({"label": scene["label"], **evaluation, "quality_flags": quality["flags"],
                                     "findings": quality["messages"]})
        scored = sum(bool(s.get("scores")) for s in report["scenes"])
        report["status"] = ("scored" if scored and scored == len(report["scenes"]) else "partly_scored" if scored else "unscored")
        if not report["scenes"]:
            report["review_items"].append("本次未提供受支持的逐幅预测，保留任务但不虚构精度。")
    verify_submission(directory, expected_hash)
    return report


def main():
    parser = argparse.ArgumentParser(description="只读评分已封存的答卷，不调用模型")
    parser.add_argument("--submission", required=True, help="解压答卷所在目录")
    parser.add_argument("--sha256", required=True, help="交卷时另行保存的清单SHA256")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True, help="答卷目录外的新报告JSON路径")
    args = parser.parse_args()
    folder, output = Path(args.submission).resolve(), Path(args.output).resolve()
    if output.is_relative_to(folder):
        parser.error("评分报告必须保存在冻结答卷目录外")
    report = grade_submission(folder, args.sha256, args.project_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print("外部评分完成：", report["status"], output)


if __name__ == "__main__":
    main()
