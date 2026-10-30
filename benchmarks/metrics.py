"""独立评分器：不改变预测、不重采样、不按答案挑选类别映射。"""

import numpy as np
import rasterio


def quality_warnings(scores):
    """只提示混淆矩阵可直接证明的问题，不设统一F1或80分合格线。"""
    flags, messages = [], []
    if scores["fn"] > scores["tp"]:
        flags.append("majority_water_unconfirmed")
        messages.append("漏掉或未确认的参考水体多于找对的部分，当前面积不能作为完整水体统计。")
    if scores["fp"] - scores.get("missing_background", 0) > scores["tp"]:
        flags.append("majority_candidates_wrong")
        messages.append("已标出的候选中，误报多于找对的水体，当前识别结果需要改进。")
    if scores["missing_predictions"]:
        flags.append("incomplete_prediction")
        messages.append("存在未确认区域，本次识别不完整；评分未忽略这些位置。")
    return {"flags": flags, "messages": messages}


def binary_metrics(prediction, reference):
    """0为背景、1为水体；预测255为弃权，按错误计入保守指标并标记不完整。"""
    prediction, reference = np.asarray(prediction), np.asarray(reference)
    if prediction.shape != reference.shape or not reference.size:
        raise ValueError("预测与答案形状不一致或为空")
    if not np.isin(reference, [0, 1]).all():
        raise ValueError("参考标签含未知值，只支持清单声明的0/1完整标签")
    if not np.isin(prediction, [0, 1, 255]).all():
        raise ValueError("预测含未知类别；必须明确0/1/255语义")
    missing = prediction == 255
    # 弃权按最不利情况计分，不能通过删除难像元提高分数。
    conservative = np.where(missing, 1 - reference, prediction)
    tp = int(((conservative == 1) & (reference == 1)).sum())
    fp = int(((conservative == 1) & (reference == 0)).sum())
    fn = int(((conservative == 0) & (reference == 1)).sum())
    tn = int(((conservative == 0) & (reference == 0)).sum())
    ratio = lambda a, b: a / b if b else None
    return {"status": "incomplete" if missing.any() else "scored", "tp": tp, "fp": fp,
            "fn": fn, "tn": tn, "pixels": int(reference.size),
            "missing_predictions": int(missing.sum()),
            "missing_water": int((missing & (reference == 1)).sum()),
            "missing_background": int((missing & (reference == 0)).sum()),
            "prediction_coverage": float((~missing).mean()),
            "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn), "iou": ratio(tp, tp + fp + fn),
            "accuracy": ratio(tp + tn, reference.size)}


def score_rasters(prediction_path, reference_path):
    """用于冻结水体案例：标签0虽被文件标为NoData，清单明确其语义是有效背景。"""
    with rasterio.open(prediction_path) as pred, rasterio.open(reference_path) as ref:
        if pred.count != 1 or ref.count != 1:
            raise ValueError("评分要求单波段分类栅格")
        if (pred.shape != ref.shape or pred.crs != ref.crs or
                not pred.transform.almost_equals(ref.transform, precision=1e-8)):
            raise ValueError("预测与参考答案网格不一致，拒绝自动重采样")
        if pred.nodata not in (None, 255):
            raise ValueError("预测NoData必须为255，不能把背景设为缺测")
        return binary_metrics(pred.read(1), ref.read(1))
