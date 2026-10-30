"""固定水体候选基线，不接受或读取参考答案。"""

import csv
import json
import re
from pathlib import Path

import numpy as np
import rasterio
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.font_manager import FontProperties

from .artifact_paths import file_hash

VERSION = "mndwi-zero-v1"


def band_index(names, wanted):
    matches = [i + 1 for i, name in enumerate(names)
               if re.sub(r"^B0+", "B", (name or "").strip().upper()) == wanted]
    if len(matches) != 1:
        raise ValueError(f"需要唯一且明确的波段描述 {wanted}，不能猜测波段位置")
    return matches[0]


def rgb_preview(source):
    channels = []
    for name in ("B4", "B3", "B2"):
        index = band_index(source.descriptions, name)
        band = source.read(index, masked=True).astype("float32")
        good = ~np.ma.getmaskarray(band) & np.isfinite(band.data)
        low, high = np.percentile(band.data[good], [2, 98]) if good.any() else (0, 1)
        channel = np.clip((band.filled(low) - low) / max(float(high - low), 1e-6), 0, 1)
        channel[~good] = .5
        channels.append(channel)
    return np.stack(channels, axis=-1)


def save_figure(output, rgb, prediction):
    font = FontProperties(family=["Microsoft YaHei", "DejaVu Sans"])
    fig = Figure(figsize=(12, 5), dpi=120, layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    axes[0].imshow(rgb)
    axes[0].set_title("输入影像（仅为显示拉伸）", fontproperties=font)
    color = np.empty((*prediction.shape, 3), dtype="float32")
    color[:] = [.92, .94, .96]
    color[prediction == 1] = [.05, .4, .9]
    color[prediction == 255] = [.95, .65, .1]
    axes[1].imshow(color)
    axes[1].set_title("水体候选：蓝色；无效像元：橙色", fontproperties=font)
    for ax in axes:
        ax.set_axis_off()
    fig.savefig(output)


def run_water_baseline(input_path, results_dir):
    """results_dir由执行器提供的URSA_RESULTS_DIR指定；本函数不设置全局环境。"""
    output = Path(results_dir)
    output.mkdir(parents=True, exist_ok=True)
    with rasterio.open(input_path) as src:
        # 当前为小型验收样本工具，大场景应另做分块实现并独立验证。
        if src.width * src.height > 4_000_000:
            raise ValueError("当前验收基线只支持不超过400万像元的小型案例")
        green_index = band_index(src.descriptions, "B3")
        swir_index = band_index(src.descriptions, "B11")
        green = src.read(green_index, masked=True).astype("float64")
        swir = src.read(swir_index, masked=True).astype("float64")
        green = green * src.scales[green_index - 1] + src.offsets[green_index - 1]
        swir = swir * src.scales[swir_index - 1] + src.offsets[swir_index - 1]
        denominator = green.data + swir.data
        valid = (~np.ma.getmaskarray(green) & ~np.ma.getmaskarray(swir) &
                 np.isfinite(green.data) & np.isfinite(swir.data) & (np.abs(denominator) > 1e-12))
        index = np.full(src.shape, -9999, dtype="float32")
        index[valid] = ((green.data[valid] - swir.data[valid]) / denominator[valid]).astype("float32")
        prediction = np.full(src.shape, 255, dtype="uint8")
        prediction[valid] = (index[valid] > 0).astype("uint8")
        profile = {"driver": "GTiff", "height": src.height, "width": src.width,
                   "count": 1, "crs": src.crs, "transform": src.transform, "compress": "deflate"}
        for name, data, dtype, nodata in (("water_mask.tif", prediction, "uint8", 255),
                                         ("mndwi.tif", index, "float32", -9999)):
            with rasterio.open(output / name, "w", **profile, dtype=dtype, nodata=nodata) as dest:
                dest.write(data, 1)
        count = int((prediction == 1).sum())
        valid_count = int(valid.sum())
        area = None
        pixel_area = None
        if src.crs and src.crs.is_projected and src.crs.linear_units == "metre":
            pixel_area = abs(src.transform.a * src.transform.e - src.transform.b * src.transform.d)
            area = count * pixel_area
        result = {"tool_version": VERSION, "formula": "(B3-B11)/(B3+B11)", "threshold": 0,
                  "threshold_rule": ">", "input_sha256": file_hash(input_path),
                  "bands": {"green": green_index, "swir1": swir_index},
                  "water_pixels": count, "valid_pixels": valid_count, "total_pixels": int(prediction.size),
                  "water_fraction": count / valid_count if valid_count else None,
                  "area_m2": area, "pixel_area_m2": pixel_area,
                  "crs": str(src.crs), "transform": list(src.transform),
                  "area_basis": "投影坐标为米时统计平面候选面积；否则不提供平方米面积",
                  "scaling_note": "应用TIFF scale/offset；冻结ESWD输入的公共DN/10000比例在比值中抵消",
                  "encoding": "0=非水候选；1=水体候选；255=无效",
                  "limitations": ["固定MNDWI>0传统基线，未使用云、阴影或积雪掩膜。",
                                  "没有按参考答案调阈值；不是WatNet作者模型预测。",
                                  "结果是水体候选，不能自动作为专业水体调查成果。"]}
        save_figure(output / "preview.png", rgb_preview(src), prediction)
    with (output / "statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["water_pixels", "valid_pixels", "total_pixels", "water_fraction", "area_m2"])
        writer.writerow([result[key] for key in ("water_pixels", "valid_pixels", "total_pixels", "water_fraction", "area_m2")])
    (output / "method.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result
