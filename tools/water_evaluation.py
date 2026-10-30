"""旧模块兼容入口；正常分析改用water_audit，答案评分仅供外部调用。"""
from .water_audit import attach_water_audit as attach_water_evaluation


def evaluate_known_prediction(project_root, input_sha256, prediction):
    from benchmarks.known_prediction import evaluate_known_prediction as evaluate
    return evaluate(project_root, input_sha256, prediction)
