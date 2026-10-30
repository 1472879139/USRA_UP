"""外部连续温度评分；缺测单列覆盖率，误差只对共同有效像元计算。"""
import numpy as np
import rasterio

from tools.artifact_paths import confined_path, file_hash


def score_temperature(prediction, reference, clear_mask, declared_reference_unit=None):
    with rasterio.open(prediction) as pred, rasterio.open(reference) as ref:
        if (pred.count != 1 or ref.count != 1 or pred.shape != ref.shape or pred.crs != ref.crs or
                not pred.transform.almost_equals(ref.transform, precision=1e-8)):
            raise ValueError('预测与参考温度的网格或波段不一致，禁止重采样追分')
        if (pred.units != ('degC',) or (ref.units != ('degC',) and
                not (ref.units == (None,) and declared_reference_unit == 'degC'))):
            raise ValueError('预测和参考必须明确为摄氏度degC')
        if pred.scales != (1.,) or pred.offsets != (0.,) or ref.scales != (1.,) or ref.offsets != (0.,):
            raise ValueError('评分要求已解码的摄氏度，不能忽略额外缩放')
        actual = pred.read(1, masked=True).astype('float64')
        expected = ref.read(1, masked=True).astype('float64')
    clear = np.load(clear_mask, allow_pickle=False)
    if clear.shape != expected.shape or not np.isin(clear, [0, 1]).all():
        raise ValueError('参考有效区掩膜形状或取值不符合冻结约定')
    eligible = clear.astype(bool) & ~np.ma.getmaskarray(expected) & np.isfinite(expected.data)
    usable = eligible & ~np.ma.getmaskarray(actual) & np.isfinite(actual.data)
    total, count = int(eligible.sum()), int(usable.sum())
    if not total:
        raise ValueError('参考有效区没有可评分温度')
    error = actual.data[usable] - expected.data[usable]
    return {'metric_type': 'temperature', 'status': 'complete' if count == total else 'incomplete',
            'reference_pixels': total, 'compared_pixels': count, 'missing_prediction_pixels': total-count,
            'coverage': count/total, 'mae_celsius': float(np.abs(error).mean()) if count else None,
            'rmse_celsius': float(np.sqrt(np.square(error).mean())) if count else None,
            'bias_celsius': float(error.mean()) if count else None,
            'unit': 'degC', 'error_scope': '仅共同有效像元；必须同时报告覆盖率，不能据此断言整图或地面精度'}


def evaluate_temperature(project_root, input_sha256, prediction):
    from .catalog import load_catalog, reference_root
    matches = [case for case in load_catalog() if case.get('assessment_enabled') and case.get('task') == 'regression' and
               any(item['sha256'] == input_sha256 for item in case['inputs'])]
    if len(matches) != 1:
        return {'status': 'unavailable', 'reason': '没有与该输入唯一匹配的温度参考产品，不能计算误差。'}
    case = matches[0]
    try:
        root = reference_root(project_root)
        if root is None:
            raise ValueError('尚未配置参考目录')
        mask = case['evaluation_mask']; reference = case['reference']
        paths = [(confined_path(root, item['path']), item['sha256']) for item in (reference, mask)]
        if any(file_hash(path) != digest for path, digest in paths):
            raise ValueError('温度参考或有效区掩膜哈希与冻结清单不一致')
        scores = score_temperature(prediction, paths[0][0], paths[1][0], case.get('reference_unit'))
        if any(file_hash(path) != digest for path, digest in paths):
            raise ValueError('评分期间温度参考或掩膜改变')
        findings = [f"参考可评分区域覆盖{scores['coverage']:.1%}，缺少预测{scores['missing_prediction_pixels']}像元。"]
        if scores['mae_celsius'] is not None:
            findings.append(f"有预测的位置与官方产品平均相差{scores['mae_celsius']:.3f}℃，均方根差{scores['rmse_celsius']:.3f}℃。")
        findings.append('这是同源辅助量重建官方温度产品的一致性，不能代表与独立地面实测的温度误差，也不表示其他地区同样有效。')
        return {'status': scores['status'], 'scores': scores, 'case_id': case['id'], 'source': case['source'],
                'reason': scores['error_scope'], 'scope': case['limitation'], 'findings': findings,
                'quality_flags': ['incomplete_prediction'] if scores['missing_prediction_pixels'] else []}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'unavailable', 'reason': '独立温度评价未完成：'+str(exc)}
