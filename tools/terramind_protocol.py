"""TerraMind Small远程地物工具的固定输入与返回契约；不读取参考标签。"""
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

PROTOCOL = "ursa-terramind-lulc-v1"
VERSION = "terramind-small-lulc-v1"
REVISION = "960f7549bfe9d7a08946860042f83badd604c779"
CHECKPOINT_SHA = "755e9cce9483fd61334ef66c79f805406db5151a8b44a685c8fbbe023c684701"
TOKENIZER_REVISION = "af6b7c73a36994e25df156e793901969efe88ec9"
TOKENIZER_SHA = "0e318c41490b472897b328256227c049aba8c1671a88c1ef3a1fa5026b3e0fd3"
SEED = 20261028
BAND_NAMES = ["B1","B2","B3","B4","B5","B6","B7","B8","B8A","B9","B11","B12"]
NAMES = ["未判定类别","水体","树木","淹水植被","农作物","建成区","裸地","雪和冰","云","草灌覆盖"]
COLORS = ["#000000","#3171AD","#469C76","#83CA70","#EAE159","#C07CB8","#C19368","#6FB2E4","#F1F1F1","#C66526"]
MAX_INPUT_BYTES = 8 * 1024**2
MAX_RESPONSE_BYTES = 4 * 1024**2


def sha(content):
    return hashlib.sha256(content).hexdigest()


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024**2), b""):
            value.update(block)
    return value.hexdigest()


def bands(values):
    return [re.sub(r"^B0+", "B", str(value or "").upper()) for value in values]


def read_input(path):
    import numpy as np
    import rasterio
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT_BYTES:
        raise ValueError("地物模型输入必须是不超过8MiB的实际TIFF文件。")
    with rasterio.open(path) as src:
        if src.shape != (224,224) or src.count != 12 or bands(src.descriptions) != BAND_NAMES:
            raise ValueError("当前地物模型需要224×224、明确标注的Sentinel-2 L2A十二光谱波段。")
        if src.crs is None or src.scales != (1.,)*12 or src.offsets != (0.,)*12:
            raise ValueError("地物输入需要空间参考和未额外缩放的原始DN。")
        value = src.read(masked=True).astype("float32")
        valid = (~np.ma.getmaskarray(value)).all(axis=0) & np.isfinite(value.data).all(axis=0)
        if not valid.any():
            raise ValueError("输入没有有效像元，不能生成地物结果。")
        raw = value.filled(0)
        if (raw[:,valid] < 0).any() or float(raw[:,valid].max()) <= 2:
            raise ValueError("输入数值不符合声明的原始反射率DN；不能重复缩放或把0～1数据冒充DN。")
        return raw, valid, src.profile.copy()


def inspect_input(path):
    path = Path(path)
    sidecar = path.parent/"metadata.json"
    if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 64*1024:
        raise ValueError("地物输入缺少metadata.json来源、产品级别和编码声明。")
    data = json.loads(sidecar.read_text(encoding="utf-8-sig"))
    if (not isinstance(data,dict) or data.get("sensor") != "Sentinel-2" or data.get("product_level") != "L2A"
            or data.get("encoding") != "DN/10000" or bands(data.get("bands",[])) != BAND_NAMES):
        raise ValueError("地物模型仅适配明确声明的Sentinel-2 L2A、十二波段及DN/10000编码。")
    _,valid,_ = read_input(path)
    return {"sha256":file_sha(path),"metadata_sha256":file_sha(sidecar),"valid_pixels":int(valid.sum())}


def encode_mask(mask,profile):
    import numpy as np
    from rasterio.io import MemoryFile
    if mask.shape != (profile["height"],profile["width"]) or mask.dtype != np.dtype("uint8") or not np.isin(mask,list(range(10))+[255]).all():
        raise ValueError("地物分类尺寸或编码不符合0～9、255契约。")
    grid = {key:profile[key] for key in ["height","width","crs","transform"]}
    with MemoryFile() as memory:
        with memory.open(driver="GTiff",count=1,dtype="uint8",nodata=255,compress="deflate",**grid) as dst:
            dst.write(mask,1)
        return memory.read()


def decode_mask(content,profile,valid):
    import numpy as np
    from rasterio.io import MemoryFile
    with MemoryFile(content) as memory,memory.open() as src:
        if (src.count != 1 or src.dtypes != ("uint8",) or src.nodata != 255 or src.shape != valid.shape
                or src.crs != profile["crs"] or not np.allclose(tuple(src.transform),tuple(profile["transform"]),rtol=0,atol=1e-12)):
            raise ValueError("地物返回分类的编码、空间网格或坐标系不符。")
        mask = src.read(1)
    if not np.isin(mask,list(range(10))+[255]).all() or (mask[~valid] != 255).any() or (mask[valid] == 255).any():
        raise ValueError("返回地物分类含未知编码或改变了输入有效范围。")
    return mask


def package_response(mask,profile,request_id,input_sha,model_info):
    content = encode_mask(mask,profile)
    manifest = {"protocol":PROTOCOL,"request_id":request_id,"input_sha256":input_sha,
                "model_revision":REVISION,"checkpoint_sha256":CHECKPOINT_SHA,"tokenizer_revision":TOKENIZER_REVISION,
                "tokenizer_sha256":TOKENIZER_SHA,"seed":SEED,"precision":"float32","strict_loading":True,
                "random_fallback_keys":0,"accuracy_evaluated":False,"ground_truth_accessed":False,
                "model_info":model_info,"files":{"classification.tif":sha(content)}}
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False,allow_nan=False))
        archive.writestr("classification.tif",content)
    return output.getvalue()


def unpack_response(content,request_id,input_sha,profile,valid):
    try:
        if len(content)>MAX_RESPONSE_BYTES:
            raise ValueError("地物返回包过大。")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            items=archive.infolist()
            if len(items)!=2 or set(archive.namelist())!={"manifest.json","classification.tif"} or sum(x.file_size for x in items)>MAX_RESPONSE_BYTES:
                raise ValueError("地物返回包成员、重复项或展开体积不符。")
            manifest=json.loads(archive.read("manifest.json"))
            expected={"protocol":PROTOCOL,"request_id":request_id,"input_sha256":input_sha,
                      "model_revision":REVISION,"checkpoint_sha256":CHECKPOINT_SHA,"tokenizer_revision":TOKENIZER_REVISION,
                      "tokenizer_sha256":TOKENIZER_SHA,"seed":SEED,"precision":"float32","strict_loading":True,
                      "random_fallback_keys":0,"accuracy_evaluated":False,"ground_truth_accessed":False}
            if not isinstance(manifest,dict) or any(manifest.get(key)!=value for key,value in expected.items()):
                raise ValueError("地物返回包不属于本次输入、请求或锁定模型。")
            prediction=archive.read("classification.tif")
            if manifest.get("files")!={"classification.tif":sha(prediction)}:
                raise ValueError("地物返回分类哈希不符。")
        return decode_mask(prediction,profile,valid),manifest
    except (zipfile.BadZipFile,KeyError,TypeError,UnicodeDecodeError,OSError,RuntimeError) as exc:
        raise ValueError("地物返回包损坏或不符合本次协议。") from exc


def statistics(mask,valid):
    return [{"code":i,"name":name,"pixels":int(((mask==i)&valid).sum()),
             "percent":float(((mask==i)&valid).sum()*100/valid.sum())} for i,name in enumerate(NAMES)]
