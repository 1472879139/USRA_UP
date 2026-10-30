"""通过私有SSH转发调用GPU模型，核验返回结果并生成无答案对照报告。"""
import csv
import io
import http.client
import json
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import prithvi_protocol as protocol

VERSION = "prithvi-remote-v1"


def settings(project_root):
    path = Path(project_root) / "gpu.local.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("GPU配置必须为小型本地JSON文件。")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    url = value.get("base_url", "http://127.0.0.1:18765")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError("GPU工具仅连接明确端口的127.0.0.1私有SSH转发地址。")
    timeout = value.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError("GPU请求超时应为1～300秒。")
    return {"base_url": url.rstrip("/"), "timeout_seconds": timeout}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(config, content, request_id, input_sha):
    request = urllib.request.Request(config["base_url"] + "/v1/water", data=content, method="POST", headers={
        "Content-Type": "image/tiff", "X-Ursa-Protocol": protocol.PROTOCOL,
        "X-Ursa-Request-ID": request_id, "X-Ursa-Input-SHA256": input_sha,
        "X-Ursa-Sensor": "Sentinel-2", "X-Ursa-Product-Level": "L1C", "X-Ursa-Encoding": "DN/10000",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=config["timeout_seconds"]) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/zip":
                raise ValueError("GPU响应状态或文件类型不符。")
            content = response.read(protocol.MAX_RESPONSE_BYTES+1)
    except urllib.error.HTTPError as exc:
        meanings = {400: "输入未通过远端预检", 413: "影像超过大小限制", 415: "上传格式不符",
                    429: "GPU正在处理其他请求，请稍后重试", 503: "GPU推理或模型不可用"}
        raise RuntimeError(meanings.get(exc.code, "GPU服务返回异常状态") + f"（HTTP {exc.code}）。没有改换算法或生成成功结果。") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
        raise RuntimeError("未连接到GPU服务或请求超时。请检查SSH隧道与GPU服务窗口；本次没有取得可交付结果。") from exc
    if len(content) > protocol.MAX_RESPONSE_BYTES:
        raise ValueError("GPU返回包过大。")
    return content


def findings(facts):
    lines = [f"模型在可分析区域内圈出的候选水体约占 {facts['model_water_fraction']:.2%}；这是范围占比，准确性尚未验证。",
             f"另一种水体指数方法圈出的范围约占 {facts['baseline_water_fraction']:.2%}。"]
    if facts["water_union_pixels"]:
        lines.append(f"两种方法在 {facts['disagreement_pixels']} 个原图像素上判断不同，约占它们合计标出水体范围的 {facts['disagreement_fraction_of_union']:.2%}（重叠位置只算一次）；这些位置需要复核。")
    else:
        lines.append("两种方法均未标出水体；这只能描述输出，不能据此证明影像中一定没有水体。")
    lines.append("单时相结果包含常态水与洪水，尚未单独识别云层；不能据此确定新增洪水或损失。")
    return lines


def save_preview(raw, model, index, valid, path):
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.font_manager import FontProperties
    rgb = raw[[2, 1, 0]].transpose(1, 2, 0).copy()
    for channel in range(3):
        low, high = np.percentile(rgb[..., channel][valid], [2, 98])
        rgb[..., channel] = np.clip((rgb[..., channel]-low)/max(float(high-low), 1e-6), 0, 1)
    rgb[~valid] = .65
    def paint(mask):
        picture = np.full((*mask.shape, 3), [.94, .95, .94], dtype="float32")
        picture[mask == 1] = [0, .5, .85]
        picture[~valid] = [.65, .65, .65]
        return picture
    difference = paint(model)
    difference[(model == 1) & (index == 0) & valid] = [.66, .2, .68]
    difference[(model == 0) & (index == 1) & valid] = [.93, .56, .15]
    fig = Figure(figsize=(11, 9), dpi=120, layout="constrained")
    FigureCanvasAgg(fig)
    font = FontProperties(family=["Microsoft YaHei", "DejaVu Sans"])
    for axis, (caption, picture) in zip(fig.subplots(2, 2).ravel(), [
        ("输入影像（仅展示拉伸）", rgb), ("模型候选水体：蓝色", paint(model)),
        ("水体指数候选：蓝色", paint(index)), ("紫：模型独有；橙：指数独有；蓝：两者均标出", difference),
    ]):
        axis.imshow(picture, interpolation="nearest")
        axis.set_title(caption, fontproperties=font, fontsize=10)
        axis.axis("off")
    fig.suptitle("差异颜色表示方法分歧，不表示已知对错；灰色为输入无效区", fontproperties=font, fontsize=12)
    fig.savefig(path)


def run_remote_water(input_path, results_dir, project_root=None, request_id=None):
    """输入与元数据锁定后发送影像；结果只写执行器提供的URSA_RESULTS_DIR。"""
    from .io_kit import _get_project_root
    root = Path(project_root or _get_project_root()).resolve()
    source = Path(input_path).resolve()
    source.relative_to((root / "data").resolve())
    output = Path(results_dir).resolve()
    output.relative_to((root / "results").resolve())
    config = settings(root)
    if config is None:
        raise ValueError("远程GPU工具尚未启用。")
    identity = protocol.inspect_input(source)
    raw, valid, profile = protocol.read_input(source)
    request_id = request_id or uuid.uuid4().hex
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("远程请求ID格式不符。")
    content = post(config, source.read_bytes(), request_id, identity["sha256"])
    masks, response = protocol.unpack_response(content, request_id, identity["sha256"], raw, valid, profile)
    if protocol.inspect_input(source) != identity:
        raise ValueError("GPU运行期间输入或元数据发生改变，需要重新确认。")
    facts = protocol.diagnostics(masks["model_mask.tif"], masks["baseline_mask.tif"], valid)
    output.mkdir(parents=True, exist_ok=True)
    (output / "remote_response.zip").write_bytes(content)
    artifacts = {}
    for method, key in [("model", "model_mask.tif"), ("baseline", "baseline_mask.tif")]:
        mask = masks[key]
        mask_name = ("final_" if method == "model" else "") + method + "_classification.tif"
        (output / mask_name).write_bytes(protocol.encode_mask(mask, profile))
        import numpy as np
        with (output / (method + "_class_statistics.csv")).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["类别编号", "类别", "像元数", "占比_%"])
            for value, caption in [(0, "非水体候选"), (1, "水体候选")]:
                count = int(((mask == value) & valid).sum())
                writer.writerow([value, caption, count, count*100/int(valid.sum())])
        artifacts[method] = mask_name
    preview = "final_water_comparison.png"
    save_preview(raw, masks["model_mask.tif"], masks["baseline_mask.tif"], valid, output/preview)
    result = {"tool": VERSION, "request_id": request_id, "input_identity": identity,
              "input": source.relative_to(root / "data").as_posix(), "response": response,
              "diagnostics": facts, "findings": findings(facts), "masks": artifacts, "preview": preview}
    result["artifacts_sha256"] = {path.name: protocol.file_sha(path) for path in output.iterdir() if path.is_file() and path.name in [
        *artifacts.values(), "model_class_statistics.csv", "baseline_class_statistics.csv", preview, "remote_response.zip"]}
    (output / "remote_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("[远程水体工具] " + source.name + "：" + "；".join(result["findings"]))
    return result
