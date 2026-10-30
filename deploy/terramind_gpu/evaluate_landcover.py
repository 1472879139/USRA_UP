"""交付后独立评分；此文件和参考图不进入GPU执行包。"""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import tempfile
import zipfile

import deploy
import landcover

HERE = Path(__file__).resolve().parent


def metrics_from_matrix(matrix):
    import numpy as np
    matrix = np.asarray(matrix, dtype="int64")
    if matrix.shape != (10, 11) or (matrix < 0).any() or matrix[0].any():
        raise RuntimeError("混淆矩阵结构不符。")
    rows = []
    for code in range(1, 10):
        tp = int(matrix[code, code])
        fn = int(matrix[code].sum()) - tp
        fp = int(matrix[:, code].sum()) - tp
        union = tp + fn + fp
        rows.append({"code": code, "name": landcover.NAMES[code], "reference_pixels": tp + fn,
                     "iou": tp / union if union else None,
                     "f1": 2 * tp / (2 * tp + fn + fp) if 2 * tp + fn + fp else None,
                     "precision": tp / (tp + fp) if tp + fp else None,
                     "recall": tp / (tp + fn) if tp + fn else None})
    total = int(matrix.sum())
    ious = [row["iou"] for row in rows if row["iou"] is not None]
    return {"evaluation_pixels": total,
            "pixel_agreement": float(np.trace(matrix[:, :10]) / total) if total else None,
            "mean_iou": sum(ious) / len(ious) if ious else None,
            "classes_in_macro": [row["code"] for row in rows if row["iou"] is not None],
            "abstention_pixels": int(matrix[:, [0, 10]].sum()), "per_class": rows}


def confusion(reference, prediction, valid):
    import numpy as np
    if reference.shape != prediction.shape or valid.shape != reference.shape:
        raise RuntimeError("类别图尺寸不一致。")
    if not np.isin(reference[valid], range(10)).all():
        raise RuntimeError("参考类别不是TerraMind v1的连续0～9编码；禁止猜测重映射。")
    if not np.isin(prediction, list(range(10)) + [255]).all():
        raise RuntimeError("预测包含协议外类别。")
    mask = valid & (reference != 0)
    columns = prediction[mask].astype("int64")
    columns[columns == 255] = 10
    return np.bincount(reference[mask].astype("int64") * 11 + columns, minlength=110).reshape(10, 11)


def load_archive(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len({item.filename for item in infos}) != len(infos) or len(infos) > 100:
            raise RuntimeError("结果包条目重复或数量异常。")
        if sum(item.file_size for item in infos) > 64 * 1024**2:
            raise RuntimeError("结果包超过64MiB核验限制。")
        files = {item.filename: archive.read(item) for item in infos}
    seal = json.loads(files["seal.json"])
    if seal["schema"] != "ursa.seal.v1" or set(seal["files"]) != set(files) - {"seal.json"}:
        raise RuntimeError("封存清单与结果包不一致。")
    for name, expected in seal["files"].items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise RuntimeError("结果包文件SHA256不符：" + name)
    return files, json.loads(files["summary.json"])


def check_run(summary, protocol, package):
    if (summary["schema"] != protocol["schema"] or summary["settings"] != protocol["settings"]
            or summary["protocol_sha256"] != deploy.digest_file(HERE / "landcover-manifest.json")
            or summary["package_sha256"] != deploy.digest_file(package)
            or summary["checkpoint_sha256"] != deploy.CHECKPOINT_SHA
            or summary["tokenizer_sha256"] != landcover.TOKENIZER_SHA
            or summary["ground_truth_accessed"] is not False or summary["paid_api_calls"] != 0):
        raise RuntimeError("运行来源、参数或隔离声明不符，不能当作本轮固定测试评分。")
    expected = [item["id"] for item in protocol["samples"]]
    if [item["id"] for item in summary["cases"]] != expected:
        raise RuntimeError("样本有缺失、重复或顺序改变。")
    if summary["completed"] != sum(case["status"] == "completed" for case in summary["cases"]):
        raise RuntimeError("完成数量与样本状态不符。")
    for index, (case, item) in enumerate(zip(summary["cases"], protocol["samples"])):
        if case["status"] not in ("completed", "failed"):
            raise RuntimeError("未知完成状态。")
        if case["status"] == "completed" and (case.get("input_sha256") != item["sha256"]
                or case.get("seed") != protocol["settings"]["seed"] + index):
            raise RuntimeError("已完成样本的输入或随机种子不符。")


def score(archive, inputs, references, package, output):
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    files, summary = load_archive(archive)
    protocol = json.loads((HERE / "landcover-manifest.json").read_text(encoding="utf-8"))
    evaluation = json.loads((HERE / "evaluation-manifest.json").read_text(encoding="utf-8"))
    check_run(summary, protocol, package)
    reference_specs = {item["id"]: item for item in evaluation["samples"]}
    total = np.zeros((10, 11), dtype="int64")
    results = []
    for item, case in zip(protocol["samples"], summary["cases"]):
        name = item["id"]
        image, reference = Path(inputs) / (name + ".tif"), Path(references) / (name + ".tif")
        if deploy.digest_file(image) != item["sha256"] or deploy.digest_file(reference) != reference_specs[name]["sha256"]:
            raise RuntimeError("评分材料哈希不符：" + name)
        with rasterio.open(image) as src, rasterio.open(reference) as ref:
            if (src.height, src.width, src.crs, src.transform) != (ref.height, ref.width, ref.crs, ref.transform) or ref.count != 1:
                raise RuntimeError("参考图和输入网格不符：" + name)
            valid = (src.read_masks() > 0).all(axis=0) & (ref.read_masks(1) > 0)
            truth = ref.read(1)
            prediction = np.full(truth.shape, 255, dtype="uint8")
            error = case.get("error")
            status = case["status"]
            if status == "completed":
                try:
                    with MemoryFile(files[name + "/landcover.tif"]) as memory, memory.open() as pred:
                        if (pred.count, pred.height, pred.width, pred.crs, pred.transform, pred.dtypes[0], pred.nodata) != (
                                1, src.height, src.width, src.crs, src.transform, "uint8", 255):
                            raise RuntimeError("预测网格、编码或NoData不符。")
                        prediction = pred.read(1)
                        if not np.isin(prediction, list(range(10)) + [255]).all():
                            raise RuntimeError("预测含非法类别。")
                except Exception as exc:
                    status, error = "invalid_artifact", str(exc)
                    prediction[:] = 255
            matrix = confusion(truth, prediction, valid)
            total += matrix
            results.append({"id": name, "status": status, "error": error,
                            "ignored_reference_zero_or_masked": int(truth.size - matrix.sum()),
                            **metrics_from_matrix(matrix), "confusion": matrix.tolist()})
    report = {"schema": protocol["schema"], "scored_utc": datetime.now(timezone.utc).isoformat(),
              "archive_sha256": deploy.digest_file(archive), "package_sha256": deploy.digest_file(package),
              "reference": evaluation["reference"], "scope": "探索性ESRI产品一致性；不能宣称人工真值精度或独立泛化",
              "policy": "参考0和原始无效像元不计分；预测0与255不排除；失败整例按未决策惩罚，所有五例进入总体",
              "total_cases": len(results), "valid_artifact_cases": sum(case["status"] == "completed" for case in results),
              "aggregate_including_failures": metrics_from_matrix(total), "confusion": total.tolist(), "cases": results}
    Path(output).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="landcover_evaluation_", dir=output))
    (root / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    def pct(value):
        return "不可计算" if value is None else f"{value:.2%}"
    rows = "".join(f'<tr><td>{html.escape(case["id"])}</td><td>{html.escape(case["status"])}</td>'
                   f'<td>{pct(case["pixel_agreement"])}</td><td>{pct(case["mean_iou"])}</td></tr>' for case in results)
    aggregate = report["aggregate_including_failures"]
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TerraMind独立评价</title><style>body{{font:17px/1.7 sans-serif;max-width:1050px;margin:auto;padding:25px;color:#163447}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #ccd;padding:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.scroll{{overflow-x:auto}}</style>
<h1>地物类别图与参考产品有多一致？</h1><p>产物核验通过 {report["valid_artifact_cases"]}/{report["total_cases"]}；
全部样本像素一致率 {pct(aggregate["pixel_agreement"])}；类别平均交并比 {pct(aggregate["mean_iou"])}。</p>
<p>像素一致率表示与参考图类别相同的比例。平均交并比按类别衡量覆盖重叠程度，越接近100%越一致。
这些数值衡量与ESRI产品的一致性；参考产品自身也可能有错，官方示例还可能与训练数据重叠。</p>
<p>{report["policy"]}</p><div class="scroll"><table><tr><th>样本</th><th>状态</th><th>像素一致率</th><th>平均交并比</th></tr>{rows}</table></div>
<details><summary>展开逐类指标、混淆矩阵与来源</summary><pre>{html.escape(json.dumps(report,ensure_ascii=False,indent=2))}</pre></details></html>'''
    (root / "evaluation.html").write_text(doc, encoding="utf-8")
    return root, report


def main():
    parser = argparse.ArgumentParser(description="封存后独立评价TerraMind地物输出")
    for name in ("archive", "inputs", "references", "package", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root, report = score(args.archive, args.inputs, args.references, args.package, args.output)
    print("独立评价：" + str(root / "evaluation.html"))
    print("包含失败样本的总体指标：" + json.dumps(report["aggregate_including_failures"], ensure_ascii=False))


if __name__ == "__main__":
    main()
