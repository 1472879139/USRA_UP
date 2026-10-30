"""Prithvi 水体工具的固定输入契约、通信格式和无答案检查。"""
import hashlib
import io
import json
from pathlib import Path
import zipfile

PROTOCOL = "ursa-prithvi-water-v1"
REVISION = "91ce9d38086a80b078a192b374df758b8855b732"
CHECKPOINT_SHA = "76eed77d8bd543ae441308b80e8408502243a871cb8a338ddc8220ed96dfc270"
CONFIG_SHA = "ca2fa45885b85afac9400c57f7ca3b711526cbad3af285f8b67de74ba2db404a"
INDICES = [2, 3, 4, 9, 12, 13]
BAND_NAMES = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B10", "B11", "B12"]
MAX_INPUT_BYTES = 16 * 1024**2
MAX_RESPONSE_BYTES = 8 * 1024**2


class EmptyInputError(ValueError):
    """格式符合要求，但整幅没有可分析像元。"""


def sha(content):
    return hashlib.sha256(content).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4*1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_input(path):
    """只支持已经验证过的 512×512、13 波段 Sentinel-2 L1C DN 编码。"""
    import numpy as np
    import rasterio
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT_BYTES:
        raise ValueError("输入必须为不超过16MB的实际TIFF文件。")
    with rasterio.open(path) as src:
        if src.shape != (512, 512) or src.count != 13 or list(src.descriptions) != BAND_NAMES:
            raise ValueError("当前模型要求512×512且明确标注B1～B12（含B8A）的13波段影像。")
        if src.crs is None or any(value != 1 for value in src.scales) or any(value != 0 for value in src.offsets):
            raise ValueError("输入需要真实坐标系和未额外缩放的DN编码。")
        selected = src.read(INDICES, masked=True).astype("float32")
        valid = (~np.ma.getmaskarray(selected)).all(axis=0) & np.isfinite(selected.data).all(axis=0)
        if not valid.any():
            raise EmptyInputError("输入没有有效像元，不能生成水体结果。")
        raw = np.asarray(selected.filled(0), dtype="float32")
        if (raw[:, valid] < 0).any() or ((raw[1] + raw[4] <= 0) & valid).any():
            raise ValueError("有效区域存在负DN或不可计算的水体指数分母。")
        raw[~np.isfinite(raw)] = 0
        return raw, valid, src.profile.copy()


def inspect_input(path):
    """来源和产品级别由本地显式元数据声明，不能靠文件名猜测。"""
    path = Path(path)
    sidecar = path.parent / "metadata.json"
    if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 64*1024:
        raise ValueError("缺少可核对的metadata.json来源、产品级别和编码声明。")
    metadata = json.loads(sidecar.read_text(encoding="utf-8-sig"))
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in {
        "sensor": "Sentinel-2", "product_level": "L1C", "encoding": "DN/10000", "bands": BAND_NAMES,
    }.items()):
        raise ValueError("远程模型目前只适配明确声明的Sentinel-2 L1C、DN/10000数据。")
    _, valid, _ = read_input(path)
    return {"sha256": file_sha(path), "metadata_sha256": file_sha(sidecar), "valid_pixels": int(valid.sum())}


def baseline(raw, valid):
    import numpy as np
    index = np.divide(raw[1]-raw[4], raw[1]+raw[4], out=np.zeros_like(raw[1]), where=raw[1]+raw[4]>0)
    mask = (index > 0).astype("uint8")
    mask[~valid] = 255
    return mask


def encode_mask(mask, profile):
    import numpy as np
    from rasterio.io import MemoryFile
    if mask.dtype != np.dtype("uint8") or not np.isin(mask, [0, 1, 255]).all():
        raise ValueError("分类编码不符合0、1、255契约。")
    if mask.shape != (profile["height"], profile["width"]):
        raise ValueError("分类尺寸必须与源网格一致，不能由写文件过程隐式重采样。")
    grid = {key: profile[key] for key in ["height", "width", "crs", "transform"]}
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", count=1, dtype="uint8", nodata=255, compress="deflate", **grid) as dst:
            dst.write(mask, 1)
        return memory.read()


def decode_mask(content, profile, valid):
    import numpy as np
    from rasterio.io import MemoryFile
    with MemoryFile(content) as memory, memory.open() as src:
        if (src.count != 1 or src.dtypes != ("uint8",) or src.nodata != 255 or
                src.shape != valid.shape or src.crs != profile["crs"] or
                not np.allclose(tuple(src.transform), tuple(profile["transform"]), rtol=0, atol=1e-12)):
            raise ValueError("返回分类的尺寸、坐标、网格或编码不符合本次输入。")
        mask = src.read(1)
    if not np.isin(mask, [0, 1, 255]).all() or (mask[~valid] != 255).any() or (mask[valid] == 255).any():
        raise ValueError("返回分类存在未知类别或改变了输入有效范围。")
    return mask


def package_response(model_mask, index_mask, profile, request_id, input_sha, model_info):
    files = {"model_mask.tif": encode_mask(model_mask, profile), "baseline_mask.tif": encode_mask(index_mask, profile)}
    manifest = {"protocol": PROTOCOL, "request_id": request_id, "input_sha256": input_sha,
                "model_revision": REVISION, "checkpoint_sha256": CHECKPOINT_SHA, "config_sha256": CONFIG_SHA,
                "checkpoint_load_strict": True, "accuracy_evaluated": False, "model_info": model_info,
                "files": {name: sha(content) for name, content in files.items()}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False))
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


def unpack_response(content, request_id, input_sha, raw, valid, profile):
    try:
        return _unpack_response(content, request_id, input_sha, raw, valid, profile)
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeDecodeError, OSError, RuntimeError) as exc:
        raise ValueError("GPU返回包损坏或不符合本次协议，不能交付。") from exc


def _unpack_response(content, request_id, input_sha, raw, valid, profile):
    import numpy as np
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("返回包超过体积限制。")
    names = {"manifest.json", "model_mask.tif", "baseline_mask.tif"}
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        items = bundle.infolist()
        if len(items) != 3 or set(bundle.namelist()) != names or sum(item.file_size for item in items) > MAX_RESPONSE_BYTES:
            raise ValueError("返回包成员、重复项或展开体积异常。")
        manifest = json.loads(bundle.read("manifest.json"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
            raise ValueError("返回包清单结构不符。")
        expected = {"protocol": PROTOCOL, "request_id": request_id, "input_sha256": input_sha,
                    "model_revision": REVISION, "checkpoint_sha256": CHECKPOINT_SHA, "config_sha256": CONFIG_SHA,
                    "checkpoint_load_strict": True, "accuracy_evaluated": False}
        if any(manifest.get(key) != value for key, value in expected.items()) or set(manifest.get("files", {})) != names-{"manifest.json"}:
            raise ValueError("返回包不属于本次请求、输入、固定模型或协议。")
        masks = {}
        for name, expected_hash in manifest["files"].items():
            data = bundle.read(name)
            if sha(data) != expected_hash:
                raise ValueError("返回分类哈希不一致。")
            masks[name] = decode_mask(data, profile, valid)
    if not np.array_equal(masks["baseline_mask.tif"], baseline(raw, valid)):
        raise ValueError("远端指数结果与本地固定公式重算不一致。")
    return masks, manifest


def diagnostics(model, index, valid):
    import numpy as np
    a, b = model == 1, index == 1
    union = (a | b) & valid
    difference = (a != b) & valid
    total, union_count = int(valid.sum()), int(union.sum())
    return {"valid_pixels": total, "model_water_pixels": int((a & valid).sum()),
            "baseline_water_pixels": int((b & valid).sum()), "both_water_pixels": int((a & b & valid).sum()),
            "model_only_pixels": int((a & ~b & valid).sum()), "baseline_only_pixels": int((~a & b & valid).sum()),
            "disagreement_pixels": int(difference.sum()), "water_union_pixels": union_count,
            "disagreement_fraction_of_union": int(difference.sum()) / union_count if union_count else None,
            "model_water_fraction": int((a & valid).sum()) / total,
            "baseline_water_fraction": int((b & valid).sum()) / total,
            "reference_used": False, "accuracy_status": "unavailable",
            "flags": (["methods_disagree"] if difference.any() else []) + (["both_predict_no_water"] if not union_count else []),
            "interpretation": "两种方法的分歧不是精度、误报率、漏检率或可信概率；一致也不保证正确。"}
