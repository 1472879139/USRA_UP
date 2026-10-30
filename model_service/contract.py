"""双方共享的模型API契约；新增模型不得静默修改任务含义。"""
from copy import deepcopy
import hashlib
import json

API_VERSION = "1.0.0"
MAX_INPUT_BYTES = 16 * 1024 ** 2
MAX_JSON_BYTES = 32 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 ** 2
MAX_PIXELS = 1024 * 1024
MAX_TILES = 64
BANDS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12"]
MOCK_TOKEN = "mock-development-token-only-for-localhost"
TERMINAL = {"succeeded", "failed", "cancelled"}
CAPABILITIES = [
    {"id": "da-v2-field-delineation", "task": "field_delineation", "tier": "specialist",
     "display_name": "DA V2候选田块提取", "model": "Delineate Anything V2",
     "model_revision": "369d0b4c44cf9bec2bd3a27bc81810cadd2c963e",
     "weights_sha256": "46700b8a279b07922953a11adaeb5e658d9a2384b6334c8e0a3090886218915a",
     "outputs": ["classification", "field_boundaries", "statistics", "preview", "provenance"],
     "classes": {"0": "其他区域", "1": "候选田块", "255": "无效"},
     "limitations": ["候选田块不等同于已经核实的农业用途", "model_score是检测器分数，不是独立验证的准确率", "小于影像分辨率的边界无法可靠恢复"],
     "license": "AGPL-3.0"},
    {"id": "terramind-small-landcover", "task": "landcover_classification", "tier": "general",
     "display_name": "TerraMind Small地物候选分类", "model": "TerraMind Small + LULC decoder",
     "model_revision": "960f7549bfe9d7a08946860042f83badd604c779",
     "weights_sha256": "755e9cce9483fd61334ef66c79f805406db5151a8b44a685c8fbbe023c684701",
     "outputs": ["classification", "statistics", "preview", "provenance"],
     "classes": {str(i): name for i, name in enumerate([
         "未判定", "水体", "树木", "淹水植被", "农作物", "建成区", "裸地", "雪冰", "云", "草灌覆盖"])} | {"255": "无效"},
     "limitations": ["生成式地物候选，不是任意遥感任务的通用求解器", "不提供逐田块边界或未经验证的准确率"],
     "license": "Apache-2.0"},
]
INPUT_CONTRACT = {
    "media_type": "image/tiff", "sensor": "Sentinel-2", "product_level": "L2A", "band_count": 12,
    "required_bands": BANDS, "encodings": ["dn_10000", "reflectance_0_1"],
    "max_upload_bytes": MAX_INPUT_BYTES, "max_pixels": MAX_PIXELS, "max_tiles": MAX_TILES,
    "min_side": 32, "max_side": 1024, "pixel_resolution_m": 10, "north_up": True,
    "crs": "已知、单位为米的投影坐标；首版不支持无坐标影像和经纬度面积计算",
    "scale_offset": "TIFF各波段scale=1、offset=0；编码由metadata显式声明",
    "output_grid": "分类保留原图范围、尺寸和坐标；无效值255",
    "source_verification": "传感器和产品来源由调用方显式声明；服务核对结构与数值，无法独立证明来源真实性",
}


class APIError(Exception):
    def __init__(self, status, code, message, retryable=False):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON字段重复")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if not isinstance(value, dict):
            raise ValueError("需要JSON对象")
        return value
    except (ValueError, UnicodeError, TypeError) as exc:
        raise APIError(400, "INVALID_JSON", "请求必须是无重复字段、无非有限数值的JSON对象") from exc


def validate_metadata(data):
    if not isinstance(data, dict) or set(data) - {"sensor", "product_level", "encoding", "bands"}:
        raise APIError(400, "INVALID_METADATA", "metadata只接受sensor、product_level、encoding、bands")
    if data.get("sensor") != "Sentinel-2" or data.get("product_level") != "L2A":
        raise APIError(400, "INPUT_UNSUPPORTED", "首版只支持明确声明的Sentinel-2 L2A影像")
    if data.get("encoding") not in INPUT_CONTRACT["encodings"]:
        raise APIError(400, "INPUT_UNSUPPORTED", "需要显式声明dn_10000或reflectance_0_1编码")
    bands = data.get("bands")
    if not isinstance(bands, list) or len(bands) != 12 or any(not isinstance(x, str) for x in bands):
        raise APIError(400, "INPUT_UNSUPPORTED", "需要12个明确的波段名称")
    normalized = ["B" + x.upper()[1:].lstrip("0") if x.upper().startswith("B") else x.upper() for x in bands]
    if len(set(normalized)) != 12 or set(normalized) != set(BANDS):
        raise APIError(400, "INPUT_UNSUPPORTED", "波段必须包含B1/B2/B3/B4/B5/B6/B7/B8/B8A/B9/B11/B12")
    return {**data, "bands": normalized}


def validate_job(data):
    if set(data) != {"capability_id", "input_id", "metadata"}:
        raise APIError(400, "INVALID_REQUEST", "提交字段必须是capability_id、input_id、metadata")
    if not isinstance(data["capability_id"], str) or data["capability_id"] not in {item["id"] for item in CAPABILITIES}:
        raise APIError(400, "CAPABILITY_UNSUPPORTED", "请求的能力尚未注册，未自动换用其他模型")
    if not isinstance(data["input_id"], str) or len(data["input_id"]) != 32 or any(x not in "0123456789abcdef" for x in data["input_id"]):
        raise APIError(400, "INVALID_INPUT_ID", "input_id格式错误")
    return {**data, "metadata": validate_metadata(data["metadata"])}


def catalog(mode, availability=None):
    items = deepcopy(CAPABILITIES)
    for item in items:
        item["input"] = deepcopy(INPUT_CONTRACT)
        item["execution_mode"] = mode
        item["availability"] = (availability or {}).get(item["id"], "ready" if mode == "mock" else "unavailable")
        item["quality_status"] = "synthetic_demo" if mode == "mock" else "unverified_candidate"
    return {"api_version": API_VERSION, "execution_mode": mode, "capabilities": items,
            "fallback_owner": "application_agent", "implicit_model_fallback": False}
