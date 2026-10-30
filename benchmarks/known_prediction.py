"""仅供外部评分和旧调用兼容，不进入普通分析流程。"""
from tools.artifact_paths import confined_path, file_hash


def evaluate_known_prediction(project_root, input_sha256, prediction):
    """只用输入文件哈希匹配冻结案例，拒绝靠文件名或答案挑类别映射。"""
    from benchmarks.catalog import load_catalog, reference_root
    from benchmarks.metrics import score_rasters
    matches = [case for case in load_catalog() if case.get("enabled") and
               any(item["sha256"] == input_sha256 for item in case["inputs"])]
    if len(matches) != 1:
        return {"status": "unavailable", "reason": "没有与该输入匹配的独立答案，识别精度尚未验证。"}
    case = matches[0]
    try:
        answers = reference_root(project_root)
        if answers is None:
            raise ValueError("尚未配置本地参考答案目录")
        reference = confined_path(answers, case["reference"]["path"])
        if file_hash(reference) != case["reference"]["sha256"]:
            raise ValueError("参考答案版本与冻结清单不一致")
        scores = score_rasters(prediction, reference)
        if file_hash(reference) != case["reference"]["sha256"]:
            raise ValueError("评分期间参考答案发生改变")
        return {"status": scores["status"], "scores": scores, "case_id": case["id"],
                "source": case["source"], "scope": "已使用的公开回归样本，不能代表未见地区精度",
                "reason": "待核实/缺测区域按错误计入保守指标，没有删除难像元。"}
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "unavailable", "reason": "独立评价未完成：" + str(exc)}
