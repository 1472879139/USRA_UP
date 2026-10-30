"""两种水体候选方法与无答案输入诊断；零阈值基线实现保持原样。"""
import csv
import json
from pathlib import Path

import numpy as np
import rasterio

from .artifact_paths import file_hash
from .water_tool import VERSION, band_index, rgb_preview, save_figure, run_water_baseline

METHODS = {
    "mndwi_zero": {"version": VERSION, "label": "固定零阈值", "requires": ["B3", "B11"]},
    "mndwi_otsu": {"version": "mndwi-otsu-v1", "label": "输入分布阈值", "requires": ["B3", "B11"]},
}
ROUTER_VERSION = "water-router-v1"
# 工程适用性启发式，必须在新场景读取/评分前冻结，不是精度或置信概率。
ROUTING_LIMITS = {"minimum_pixels": 1024, "minimum_separability": .75,
                  "minimum_class_fraction": .01, "maximum_threshold_shift": .05}
# 是否替换默认方法由外部预先冻结的整组验收门槛决定，不运行时读取成绩。
AUTO_ENABLED = False


def read_index(input_path):
    """按实际波段、比例和有效掩膜计算指数；与基线的float32比较口径一致。"""
    with rasterio.open(input_path) as src:
        if src.width * src.height > 4_000_000:
            raise ValueError("水体候选工具只支持不超过400万像元的影像")
        green_id, swir_id = band_index(src.descriptions, "B3"), band_index(src.descriptions, "B11")
        green = src.read(green_id, masked=True).astype("float64")
        swir = src.read(swir_id, masked=True).astype("float64")
        green = green * src.scales[green_id-1] + src.offsets[green_id-1]
        swir = swir * src.scales[swir_id-1] + src.offsets[swir_id-1]
        denominator = green.data + swir.data
        valid = (~np.ma.getmaskarray(green) & ~np.ma.getmaskarray(swir) & np.isfinite(green.data) &
                 np.isfinite(swir.data) & (np.abs(denominator)>1e-12))
        index = np.full(src.shape, -9999, dtype="float32")
        index[valid] = ((green.data[valid]-swir.data[valid])/denominator[valid]).astype("float32")
    return index, valid


def _otsu(values):
    """固定256箱，在样本最小/最大值之间最大化两组间方差；并列时取首箱中心。"""
    counts, edges = np.histogram(values, bins=256)
    centers = (edges[:-1]+edges[1:])/2
    weights = counts.astype("float64")/len(values)
    cumulative = np.cumsum(weights)
    mean = np.cumsum(weights*centers)
    variance = np.zeros(255)
    denominator = cumulative[:-1]*(1-cumulative[:-1])
    good = denominator>1e-15
    variance[good] = (mean[-1]*cumulative[:-1][good]-mean[:-1][good])**2/denominator[good]
    best = int(np.argmax(variance))
    total_variance = float(np.sum(weights*(centers-mean[-1])**2))
    return float(centers[best]), float(variance[best]/total_variance) if total_variance else 0.0


def otsu_diagnostics(index, valid):
    """只使用输入像元，记录可分性与交错抽样阈值差；不能据此保证地物语义。"""
    values = np.asarray(index)[np.asarray(valid, dtype=bool)]
    values = values[np.isfinite(values)].astype("float64")
    result = {"valid_pixels": int(values.size), "threshold": None, "separability": 0.0,
              "minority_fraction": 0.0, "threshold_shift": None, "reason": ""}
    if values.size<ROUTING_LIMITS["minimum_pixels"]:
        result["reason"] = "有效样本不足，保留零阈值基线"
    elif values.min() < -1 or values.max() > 1:
        result["reason"] = "指数超出常规比值范围，不启用输入分布选路"
    elif values.max()-values.min()<1e-8:
        result["reason"] = "输入近似常量，没有可核对的两组分布"
    else:
        threshold, separability = _otsu(values)
        fraction = float((values>threshold).mean())
        subsets = [values[::2], values[1::2]]
        shift = max(abs(_otsu(part)[0]-threshold) if np.ptp(part)>1e-8 else 2.0 for part in subsets)
        result.update(threshold=threshold, separability=separability, minority_fraction=min(fraction, 1-fraction),
                      threshold_shift=float(shift))
    return result


def choose_from_diagnostics(diagnostics):
    """固定启发式选择，不接收路径、参考标签或评分；默认回退不代表准确。"""
    d = diagnostics
    eligible = (d["threshold"] is not None and d["separability"]>=ROUTING_LIMITS["minimum_separability"] and
                d["minority_fraction"]>=ROUTING_LIMITS["minimum_class_fraction"] and
                d["threshold_shift"]<=ROUTING_LIMITS["maximum_threshold_shift"])
    return {"method": "mndwi_otsu" if eligible else "mndwi_zero", "diagnostics": diagnostics,
            "reason": "两组分布与交错抽样检查满足冻结条件，尝试输入分布阈值" if eligible else
                      d.get("reason") or "分布检查证据不足，保留零阈值基线"}


def choose_method(input_path):
    return choose_from_diagnostics(otsu_diagnostics(*read_index(input_path)))


def run_method(method_id, input_path, results_dir):
    """统一输出契约；两种方法均只读输入，写执行器指定目录，不加载评分依赖。"""
    if method_id not in METHODS:
        raise ValueError("未知水体方法")
    if method_id=="mndwi_zero":
        return run_water_baseline(input_path, results_dir)
    index, valid = read_index(input_path)
    diagnostics = otsu_diagnostics(index, valid)
    if diagnostics["threshold"] is None:
        raise ValueError("输入分布阈值方法不可用：" + diagnostics["reason"])
    threshold = diagnostics["threshold"]
    prediction = np.full(index.shape, 255, dtype="uint8")
    prediction[valid] = (index[valid]>threshold).astype("uint8")
    output = Path(results_dir); output.mkdir(parents=True, exist_ok=True)
    count, valid_count = int((prediction==1).sum()), int(valid.sum())
    with rasterio.open(input_path) as src:
        profile = {"driver": "GTiff", "height": src.height, "width": src.width, "count": 1,
                   "crs": src.crs, "transform": src.transform, "compress": "deflate"}
        pixel_area = abs(src.transform.a*src.transform.e-src.transform.b*src.transform.d) if (
            src.crs and src.crs.is_projected and src.crs.linear_units=="metre") else None
        for name, data, dtype, nodata in (("water_mask.tif", prediction, "uint8", 255), ("mndwi.tif", index, "float32", -9999)):
            with rasterio.open(output/name, "w", **profile, dtype=dtype, nodata=nodata) as dest:
                dest.write(data, 1)
        result = {"tool_version": METHODS[method_id]["version"], "formula": "(B3-B11)/(B3+B11)",
                  "threshold": threshold, "threshold_rule": ">", "input_sha256": file_hash(input_path),
                  "bands": {"green": band_index(src.descriptions, "B3"), "swir1": band_index(src.descriptions, "B11")},
                  "water_pixels": count, "valid_pixels": valid_count, "total_pixels": int(prediction.size),
                  "water_fraction": count/valid_count if valid_count else None,
                  "area_m2": count*pixel_area if pixel_area is not None else None, "pixel_area_m2": pixel_area,
                  "crs": str(src.crs), "transform": list(src.transform), "input_diagnostics": diagnostics,
                  "area_basis": "投影坐标为米时统计平面候选面积；否则不提供平方米面积",
                  "scaling_note": "应用TIFF scale/offset；共同DN/10000比例在比值中抵消",
                  "encoding": "0=非水候选；1=水体候选；255=无效",
                  "limitations": ["输入分布阈值只分开两组指数像元，不保证两组就是水体与非水体。",
                                  "没有按参考答案调整阈值；未使用云、阴影或积雪掩膜。",
                                  "结果是候选范围，不能自动作为专业水体调查成果。"]}
        save_figure(output/"preview.png", rgb_preview(src), prediction)
    with (output/"statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        columns = ["water_pixels", "valid_pixels", "total_pixels", "water_fraction", "area_m2"]
        writer.writerow(columns); writer.writerow([result[key] for key in columns])
    (output/"method.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result
