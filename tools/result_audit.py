"""Deterministic artifact/numeric checks, not a land-cover accuracy estimator."""

import csv
from decimal import Decimal
import math
from pathlib import Path
import re

import numpy as np


def snapshot_result_files(results_dir) -> dict:
    root = Path(results_dir).resolve()
    snapshot = {}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    path.resolve().relative_to(root)
                    stat = path.stat()
                    snapshot[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
                except (OSError, ValueError):
                    continue
    return snapshot


def _read_statistics(path):
    aliases = {
        "class_name": ("类别", "class", "class_name"),
        "pixels": ("像元数", "pixel_count", "pixels"),
        "percent": ("占比_%", "占比(%)", "占比", "percentage", "percent"),
        "area_km2": ("面积_km2", "面积(km²)", "area_km2"),
        "scheme": ("方案", "scheme"),
        "class_id": ("类别编号", "class_id", "label"),
    }
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        keys = {key: next((name for name in names if name in fields), None)
                for key, names in aliases.items()}
        if not all(keys[key] for key in ("class_name", "pixels", "percent")):
            return []
        rows = []
        for index, raw in enumerate(reader):
            if index >= 10000:
                raise ValueError("分类统计行数超出核验上限")
            name = raw[keys["class_name"]]
            if name.strip().lower() in ("合计", "总计", "total"):
                continue
            row = {"class_name": name.strip(),
                   "pixels": float(raw[keys["pixels"]].replace(",", "")),
                   "percent": float(raw[keys["percent"]].rstrip("%")),
                   "scheme": raw.get(keys["scheme"], "默认") or "默认",
                   "area_km2": float(raw[keys["area_km2"]]) if keys["area_km2"] else None}
            numeric = [row["pixels"], row["percent"]]
            if row["area_km2"] is not None:
                numeric.append(row["area_km2"])
            if not all(math.isfinite(value) and value >= 0 for value in numeric):
                raise ValueError("分类像元/面积/占比包含负数或非有限数")
            if row["pixels"] != int(row["pixels"]):
                raise ValueError("像元数不是整数")
            row["pixels"] = int(row["pixels"])
            row["area_tolerance"] = (max(1e-9, 0.5 * 10.0 ** Decimal(raw[keys["area_km2"]]).as_tuple().exponent)
                                     if keys["area_km2"] else 1e-9)
            row["class_id"] = None
            if keys["class_id"]:
                class_id = float(raw[keys["class_id"]])
                if not math.isfinite(class_id) or class_id != int(class_id):
                    raise ValueError("类别编号不是有限整数")
                row["class_id"] = int(class_id)
            rows.append(row)
        return rows


def _read_label_counts(path):
    import rasterio

    with rasterio.open(path) as src:
        if src.count != 1 or not np.issubdtype(np.dtype(src.dtypes[0]), np.integer):
            return None
        counts = {}
        for _, window in src.block_windows(1):
            data = src.read(1, window=window, masked=True)
            values, numbers = np.unique(data.compressed(), return_counts=True)
            for value, number in zip(values.tolist(), numbers.tolist()):
                counts[value] = counts.get(value, 0) + number
        pixel_area = None
        if src.crs and src.crs.is_projected and src.crs.linear_units in ("metre", "meter"):
            transform = src.transform
            pixel_area = abs(transform.a * transform.e - transform.b * transform.d) / 1e6
        return {"counts": counts, "pixel_area_km2": pixel_area}


def audit_execution_results(results_dir, files_generated, requirement="", *, task_subdir=None) -> dict:
    root = Path(results_dir).resolve()
    task_root = (root / task_subdir).resolve() if task_subdir else root
    task_root.relative_to(root)
    audit = {"status": "pass", "errors": [], "warnings": [], "statistics": [], "files": []}
    errors, warnings = audit["errors"], audit["warnings"]
    paths = []
    for name in dict.fromkeys(files_generated or []):
        path = (root / name).resolve()
        try:
            path.relative_to(root)
            try:
                path.relative_to(task_root)
            except ValueError:
                raise ValueError("不在本次任务目录中")
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("缺失或为空")
            paths.append(path)
            audit["files"].append(path.relative_to(root).as_posix())
        except (ValueError, OSError) as error:
            errors.append(f"本次产物不可用：{name}（{error}）")
    if not paths:
        errors.append("没有本次新增或更新的有效产物，历史文件不能作为成功证据。")

    format_events = [(match.end(), match.group(1).lower()) for match in
                     re.finditer(r"(?<![A-Za-z])(jpg|jpeg|png)(?![A-Za-z])", requirement, re.IGNORECASE)]
    format_events.extend((match.end(), None) for match in re.finditer(
        r"(?:不需要|无需|不要|取消|不输出|不生成|不保存).{0,6}(?:图片|图像|图表|成图|JPG|JPEG|PNG)|(?:只要|仅需|仅要)\s*(?:CSV|统计表)",
        requirement, re.IGNORECASE,
    ))
    requested = sorted(format_events, key=lambda event: event[0])[-1][1] if format_events else None
    if requested:
        extensions = {".jpg", ".jpeg"} if requested in ("jpg", "jpeg") else {".png"}
        if not any(path.suffix.lower() in extensions for path in paths):
            errors.append(f"本次没有生成用户要求的 {requested.upper()} 图片。")

    rasters = []
    for path in paths:
        try:
            if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                from PIL import Image
                with Image.open(path) as image:
                    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}[path.suffix.lower()]
                    if image.format != expected:
                        raise ValueError(f"实际格式 {image.format} 与扩展名不一致")
                    image.load()
            elif path.suffix.lower() == ".csv" and path.stat().st_size <= 10 * 1024 * 1024:
                rows = _read_statistics(path)
                for row in rows:
                    row["source"] = path.relative_to(root).as_posix()
                audit["statistics"].extend(rows)
            elif (path.suffix.lower() in (".tif", ".tiff")
                  and not path.name.lower().startswith("intermediate")
                  and re.search(r"landcover|classification|classified|class_map", path.name, re.I)):
                labels = _read_label_counts(path)
                if labels:
                    rasters.append(labels)
        except Exception as error:
            errors.append(f"读取产物失败：{path.name}（{error}）")

    groups = {}
    for row in audit["statistics"]:
        groups.setdefault((row["source"], row["scheme"]), []).append(row)
    for (source, scheme), rows in groups.items():
        label = f"{source}/{scheme}"
        total = sum(row["pixels"] for row in rows)
        if total <= 0:
            errors.append(f"{label}：分类有效像元数为零。")
            continue
        if len({row["class_name"] for row in rows}) != len(rows):
            errors.append(f"{label}：存在重复类别，无法可靠核验。")
        ids = [row["class_id"] for row in rows if row["class_id"] is not None]
        if len(ids) != len(set(ids)):
            errors.append(f"{label}：存在重复类别编号，无法核验名称对应关系。")
        if abs(sum(row["percent"] for row in rows) - 100) > 0.2:
            errors.append(f"{label}：各类别占比之和不等于 100%。")
        if any(abs(row["percent"] - row["pixels"] * 100 / total) > 0.1 for row in rows):
            errors.append(f"{label}：占比与像元数不一致。")
        candidates = [raster for raster in rasters if
                      sorted(raster["counts"].values()) == sorted(row["pixels"] for row in rows if row["pixels"])]
        if rasters and not candidates:
            errors.append(f"{label}：CSV 像元数与本次分类栅格不一致。")
        if candidates:
            if all(row["class_id"] is not None for row in rows):
                candidates = [raster for raster in candidates if all(
                    raster["counts"].get(row["class_id"], 0) == row["pixels"] for row in rows)]
                if not candidates:
                    errors.append(f"{label}：类别编号对应的像元数与分类栅格不一致。")
            else:
                warnings.append(f"{label}：缺少类别编号，仅核对像元数量分布，编号到名称的对应尚未核实。")
            areas = [raster["pixel_area_km2"] for raster in candidates if raster["pixel_area_km2"] is not None]
            for row in rows:
                if row["area_km2"] is not None and areas and not any(
                    math.isclose(row["area_km2"], row["pixels"] * area, rel_tol=1e-4, abs_tol=row["area_tolerance"])
                    for area in areas
                ):
                    errors.append(f"{label}/{row['class_name']}：面积与栅格像元面积不一致。")
        else:
            warnings.append(f"{label}：没有匹配的分类栅格可交叉核验面积与像元数。")

    classification = bool(groups or rasters or re.search(r"分类|地物|land.?cover", requirement, re.I))
    if classification:
        warnings.append("系统尚未核实独立样本验证证据：聚类和启发式簇命名作为候选分类，类别语义待复核。")
        warnings.append("统计范围是实际参与计算的像元；尚未证明与完整行政区范围一致。")
        if not groups:
            warnings.append("本次没有可识别的分类统计表，无法自动复核各类面积和占比。")
    audit["status"] = "failed" if errors else "needs_review" if warnings else "pass"
    lines = ["[本次产物机器核验] " + {"pass": "数值/产物检查通过", "needs_review": "结果待复核", "failed": "核验失败"}[audit["status"]]]
    if audit["files"]:
        lines.append("本次产物：" + "、".join("results/" + name for name in audit["files"]))
    for (source, scheme), rows in groups.items():
        lines.append(f"统计表 {source} / {scheme}：")
        for row in rows:
            area_text = f"{row['area_km2']} km²" if row["area_km2"] is not None else "未提供"
            lines.append(f"- {row['class_name']}：{row['pixels']} 像元，面积 {area_text}，占比 {row['percent']:.4f}%")
        highest = max(row["percent"] for row in rows)
        winners = "、".join(row["class_name"] for row in rows if row["percent"] == highest)
        lines.append(f"全部类别中，{winners}占比最高（{highest:.4f}%）。")
    lines.extend("错误：" + error for error in errors)
    lines.extend("待复核：" + warning for warning in warnings)
    audit["facts"] = "\n".join(lines)
    return audit


def report_conflicts(summary: str, audit: dict) -> list:
    """Catch directly testable ranking contradictions; this is not a full semantic judge."""
    rows = audit.get("statistics", [])
    schemes = {}
    for row in rows:
        schemes.setdefault((row.get("source", ""), row.get("scheme", "默认")), []).append(row)
    conflicts = []
    for group in schemes.values():
        highest = max(row["percent"] for row in group)
        for row in group:
            pattern = re.escape(row["class_name"]) + r"(?:类)?(?:的)?[\s*：:]*(?:占比|比例)[\s*]*(?:为|是)?[\s*]*最高"
            if row["percent"] < highest and re.search(pattern, summary):
                conflicts.append(f"{row['class_name']}不是全部类别中占比最高者")
    return conflicts
