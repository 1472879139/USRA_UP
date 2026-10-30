"""服务器端的输入适配、分块推理和统一产物；仅GPU工作进程导入GIS/深度学习依赖。"""
import csv
import hashlib
import json
from pathlib import Path
import time

from .contract import BANDS, CAPABILITIES, MAX_PIXELS, MAX_TILES, sha256
from .backends import BackendError


def read_input(path, metadata):
    import numpy as np
    import rasterio
    with rasterio.open(path) as src:
        if src.count != 12 or min(src.shape) < 32 or max(src.shape) > 1024 or src.width * src.height > MAX_PIXELS:
            raise BackendError("INPUT_UNSUPPORTED", "首版需要12波段、每边32～1024像元的TIFF")
        if src.crs is None or not src.crs.is_projected or src.crs.linear_units != "metre":
            raise BackendError("INPUT_UNSUPPORTED", "首版需要已知、单位为米的投影坐标")
        affine = src.transform
        if affine.b != 0 or affine.d != 0 or affine.a != 10 or affine.e != -10:
            raise BackendError("INPUT_UNSUPPORTED", "首版只验收北向、10米像元的Sentinel-2影像栈")
        if src.scales != (1.,) * 12 or src.offsets != (0.,) * 12:
            raise BackendError("INPUT_UNSUPPORTED", "TIFF含非默认scale/offset，请先明确还原物理编码")
        declared = metadata["bands"]
        for index, description in enumerate(src.descriptions):
            if description:
                normalized = "B" + description.upper()[1:].lstrip("0") if description.upper().startswith("B") else description.upper()
                if normalized != declared[index]:
                    raise BackendError("INPUT_UNSUPPORTED", "TIFF实际波段描述与调用方声明不符")
        value = src.read(masked=True).astype("float32")
        valid = (~np.ma.getmaskarray(value)).all(axis=0) & np.isfinite(value.data).all(axis=0)
        raw = value.filled(0)
        if not valid.any() or not (raw[:, valid] > 0).any():
            raise BackendError("INPUT_UNSUPPORTED", "输入没有可用观测像元")
        values = raw[:, valid]
        upper = 1.0 if metadata["encoding"] == "reflectance_0_1" else 65535.0
        if values.min() < 0 or values.max() > upper:
            raise BackendError("INPUT_UNSUPPORTED", "实际数值超出声明编码范围，未猜测或重复缩放")
        raw = raw[[declared.index(name) for name in BANDS]]
        if metadata["encoding"] == "reflectance_0_1":
            raw *= 10000
        raw[:, ~valid] = 0
        return raw, valid, src.profile.copy()


def positions(length, tile, overlap):
    if length <= tile:
        return [0]
    result = list(range(0, length - tile + 1, tile - overlap))
    if result[-1] != length - tile:
        result.append(length - tile)
    return result


def tiles(raw, valid, size, overlap):
    import numpy as np
    ys, xs = positions(raw.shape[1], size, overlap), positions(raw.shape[2], size, overlap)
    if len(ys) * len(xs) > MAX_TILES:
        raise BackendError("INPUT_UNSUPPORTED", "切块数量超过首版64块限制")
    for y in ys:
        for x in xs:
            height, width = min(size, raw.shape[1] - y), min(size, raw.shape[2] - x)
            block = np.zeros((12, size, size), dtype="float32")
            mask = np.zeros((size, size), dtype=bool)
            block[:, :height, :width] = raw[:, y:y + height, x:x + width]
            mask[:height, :width] = valid[y:y + height, x:x + width]
            yield x, y, width, height, block, mask


def predict_terramind(model, raw, valid):
    import numpy as np
    votes = np.zeros((10, *valid.shape), dtype="uint8")
    count = 0
    for x, y, width, height, block, mask in tiles(raw, valid, 224, 32):
        if not mask.any():
            continue
        prediction, _ = model.predict(block, mask)
        region = votes[:, y:y + height, x:x + width]
        for code in range(10):
            region[code] += ((prediction[:height, :width] == code) & mask[:height, :width]).astype("uint8")
        count += 1
    output = votes.argmax(axis=0).astype("uint8")
    if (votes.sum(axis=0)[valid] == 0).any():
        raise BackendError("RESULT_CONTRACT_FAILED", "地物拼接存在未覆盖的有效像元")
    output[~valid] = 255
    return output, {"tile_size": 224, "overlap": 32, "tile_count": count,
                    "merge": "多数票；平票取较小类别编号"}


def predict_da(model, raw, valid, profile):
    import numpy as np
    import torch
    from torchvision.transforms import InterpolationMode
    from torchvision.transforms.functional import resize
    from rasterio.features import rasterize, shapes
    from rasterio.warp import transform_geom
    from affine import Affine
    from shapely.geometry import Polygon, shape, mapping
    from shapely import make_valid, STRtree
    from shapely.ops import unary_union
    valid_pieces = []
    for geometry, value in shapes(valid.astype("uint8"), mask=valid, transform=profile["transform"]):
        valid_pieces.append(shape(geometry))
        if len(valid_pieces) > 4096:
            raise BackendError("INPUT_UNSUPPORTED", "有效区过于破碎，超过首版掩膜复杂度限制")
    valid_geometry = unary_union(valid_pieces)
    candidates, count, hit_limit = [], 0, False
    for x, y, width, height, block, mask in tiles(raw, valid, 256, 64):
        if not mask.any():
            continue
        tensor = torch.from_numpy(block[[3, 2, 1]]).unsqueeze(0).to("cuda:0")
        tensor = resize((tensor / 3000).clamp(0, 1), [512, 512],
                        interpolation=InterpolationMode.BILINEAR, antialias=True)
        with torch.inference_mode():
            result = model.predict(tensor, conf=0.05, iou=0.3, max_det=1000,
                                   device="cuda:0", half=False, verbose=False)[0]
        count += 1
        hit_limit |= len(result.boxes) >= 1000
        transform = profile["transform"] * Affine.translation(x, y)
        if result.masks is None:
            continue
        for points, confidence in zip(result.masks.xy, result.boxes.conf.cpu().tolist()):
            if len(points) < 3:
                continue
            polygon = make_valid(Polygon([transform * (float(px) / 2, float(py) / 2) for px, py in points]))
            polygon = polygon.intersection(valid_geometry)
            parts = [polygon] if polygon.geom_type == "Polygon" else list(getattr(polygon, "geoms", []))
            parts = [p for p in parts if p.geom_type == "Polygon" and p.area > 0]
            if parts:
                candidates.append((unary_union(parts), float(confidence), (x, y)))
        if len(candidates) > 20000:
            raise BackendError("OUTPUT_LIMIT", "候选田块数量超过首版限制，请缩小影像范围")
    # 跨块去重，只抑制显著重叠/包含的候选，不用答案调边界。
    kept, removed = [], set()
    if candidates:
        tree = STRtree([p for p, _, _ in candidates])
        order = sorted(range(len(candidates)), key=lambda i: (-candidates[i][1], i))
        rank = {index: i for i, index in enumerate(order)}
        for i in order:
            if i in removed:
                continue
            polygon, confidence, tile_id = candidates[i]
            kept.append((polygon, confidence))
            for j in tree.query(polygon):
                j = int(j)
                if rank[j] <= rank[i] or j in removed or candidates[j][2] == tile_id:
                    continue
                other = candidates[j][0]
                intersection = polygon.intersection(other).area
                if intersection <= 0:
                    continue
                union = polygon.area + other.area - intersection
                if intersection / union > 0.3 or intersection / min(polygon.area, other.area) > 0.8:
                    removed.add(j)
    output = np.zeros(valid.shape, dtype="uint8")
    if kept:
        output = rasterize([(mapping(p), 1) for p, _ in kept], out_shape=valid.shape,
                           transform=profile["transform"], dtype="uint8", fill=0, all_touched=False)
    output[~valid] = 255
    features = [{"type": "Feature", "properties": {"field_id": i + 1, "model_score": confidence,
                "vector_area_m2": polygon.area, "quality_status": "unverified_candidate"},
                "geometry": transform_geom(profile["crs"], "EPSG:4326", mapping(polygon))}
                for i, (polygon, confidence) in enumerate(kept)]
    info = {"tile_size": 256, "overlap": 64, "tile_count": count, "resize_factor": 2,
            "confidence_threshold": 0.05, "nms_iou": 0.3, "max_detections_per_tile": 1000,
            "detection_limit_reached": bool(hit_limit), "field_count": len(kept),
            "cross_tile_dedup": "IoU>0.3或交集/较小面积>0.8；置信度优先；面积按最终栅格去重"}
    return output, {"type": "FeatureCollection", "features": features}, info


def write_products(output, prediction, raw, valid, profile, capability_id, provenance, boundaries=None, mock=False):
    import numpy as np
    import rasterio
    from PIL import Image, ImageDraw, ImageFont
    output = Path(output)
    output.mkdir(exist_ok=True)
    capability = next(c for c in CAPABILITIES if c["id"] == capability_id)
    if prediction.shape != valid.shape or prediction.dtype != np.dtype("uint8"):
        raise BackendError("RESULT_CONTRACT_FAILED", "分类形状或数据类型不符")
    if not np.isin(prediction, list(map(int, capability["classes"])) + [255]).all() or (prediction[~valid] != 255).any() or (prediction[valid] == 255).any():
        raise BackendError("RESULT_CONTRACT_FAILED", "分类类别或有效范围不符")
    grid = {key: profile[key] for key in ["height", "width", "crs", "transform"]}
    with rasterio.open(output / "classification.tif", "w", driver="GTiff", count=1, dtype="uint8", nodata=255, compress="deflate", **grid) as dst:
        dst.write(prediction, 1)
    pixel_area = abs(profile["transform"].a * profile["transform"].e - profile["transform"].b * profile["transform"].d)
    rows = [{"code": int(code), "name": name, "pixels": int(((prediction == int(code)) & valid).sum())}
            for code, name in capability["classes"].items() if code != "255"]
    for row in rows:
        row.update(area_m2=row["pixels"] * pixel_area, fraction=row["pixels"] / int(valid.sum()))
    if sum(r["pixels"] for r in rows) != int(valid.sum()):
        raise BackendError("RESULT_CONTRACT_FAILED", "统计像元数量与有效区不一致")
    with (output / "statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["code", "name", "pixels", "area_m2", "fraction"])
        writer.writeheader()
        writer.writerows(rows)
    rgb = np.moveaxis((np.clip(raw[[3, 2, 1]] / 3000, 0, 1) * 255).astype("uint8"), 0, -1)
    rgb[~valid] = 0
    shown = rgb.copy()
    if capability_id.startswith("da-"):
        selected = (prediction == 1) & valid
        shown[selected] = (shown[selected] * 0.55 + np.array([255, 211, 55]) * 0.45).round().astype("uint8")
    else:
        palette = [[0,0,0],[49,113,173],[70,156,118],[131,202,112],[234,225,89],
                   [192,124,184],[193,147,104],[111,178,228],[241,241,241],[198,101,38]]
        for code, color in enumerate(palette):
            shown[(prediction == code) & valid] = color
        shown[~valid] = [110,110,110]
    image = Image.fromarray(np.concatenate([rgb, shown], axis=1))
    image.thumbnail((1024, 1024), Image.Resampling.NEAREST)
    if mock:
        image = image.resize((512, 256), Image.Resampling.NEAREST)
    canvas = Image.new("RGB", (image.width, image.height + 44), "white")
    canvas.paste(image, (0, 44))
    ImageDraw.Draw(canvas).text((8, 12), "MOCK - SYNTHETIC DEMO" if mock else "Candidate result - not accuracy verified", fill="red" if mock else "black")
    canvas.save(output / "preview.png")
    (output / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    files = [{"name": name, "media_type": media, "role": role}
             for name, media, role in [("classification.tif", "image/tiff", "classification"),
                                      ("statistics.csv", "text/csv; charset=utf-8", "statistics"),
                                      ("preview.png", "image/png", "preview"),
                                      ("provenance.json", "application/json", "provenance")]]
    if boundaries is not None:
        (output / "field_boundaries.geojson").write_text(json.dumps(boundaries, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        files.append({"name": "field_boundaries.geojson", "media_type": "application/geo+json", "role": "field_boundaries"})
    for item in files:
        content = (output / item["name"]).read_bytes()
        item.update(sha256=sha256(content), size_bytes=len(content))
    return {"execution_mode": "mock" if mock else "gpu", "quality_status": "synthetic_demo" if mock else "unverified_candidate",
            "accuracy_evaluated": False, "capability_id": capability_id, "model": capability["model"],
            "model_revision": capability["model_revision"], "input_sha256": provenance.get("input_sha256"),
            "width": profile["width"], "height": profile["height"], "crs": str(profile["crs"]),
            "transform": list(profile["transform"]), "valid_pixels": int(valid.sum()), "pixel_area_m2": pixel_area,
            "classes": capability["classes"], "statistics": rows, "model_run": provenance,
            "warnings": ["候选结果尚未经过独立人工精度核验"], "files": files}
