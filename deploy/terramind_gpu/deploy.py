"""部署固定版本TerraMind Small，并用已下载真实影像验证GPU特征计算。"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import html
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

MODEL_ID = "ibm-esa-geospatial/TerraMind-1.0-small"
REVISION = "960f7549bfe9d7a08946860042f83badd604c779"
FILENAME = "TerraMind_v1_small.pt"
CHECKPOINT_SHA = "755e9cce9483fd61334ef66c79f805406db5151a8b44a685c8fbbe023c684701"
CHECKPOINT_SIZE = 504231662
SAMPLE_REVISION = "91ce9d38086a80b078a192b374df758b8855b732"
SAMPLE_SHA = "ee898621b387a731503a01960397599f209b45c268d4e088689928c704dbe968"
EXPECTED = {"torch": "2.10.0+cu128", "torchvision": "0.25.0+cu128", "terratorch": "1.2.11",
            "torchgeo": "0.8.1", "numpy": "2.2.6", "rasterio": "1.4.4"}
MODALITIES = ["S2L1C"]
ROOT = Path.home() / "ursa-gpu-runtime"


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_checkpoint(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != CHECKPOINT_SIZE:
        raise RuntimeError("Small权重缺失或体积不符；停止，未加载模型。")
    if digest_file(path) != CHECKPOINT_SHA:
        raise RuntimeError("Small权重SHA256不符；停止，未加载模型，也未覆盖已有文件。")
    return path


def select_complete_state(checkpoint, required):
    """只筛选当前原始输入编码器需要的参数；缺失和形状不符均拒绝。"""
    if not isinstance(checkpoint, dict) or not required:
        raise RuntimeError("官方权重内容不是参数字典。")
    missing = sorted(set(required) - set(checkpoint))
    mismatched = [key for key in required if key in checkpoint
                  and getattr(checkpoint[key], "shape", None) != required[key].shape]
    if missing or mismatched:
        raise RuntimeError(f"编码器权重不完整：缺失{len(missing)}项、形状不符{len(mismatched)}项；"
                           f"示例：{(missing + mismatched)[:5]}。不使用随机参数补齐。")
    return {key: checkpoint[key] for key in required}


@contextmanager
def offline_network():
    """权重加载和影像计算期间禁止新建网络连接。"""
    def blocked(*args, **kwargs):
        raise RuntimeError("离线模型计算阶段禁止联网。")
    original = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = blocked
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = original


def check_environment():
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("请通过既有ursa-gpu-runtime/run-python.sh运行，需要Python 3.11。")
    installed = {name: version(name) for name in EXPECTED}
    if installed != EXPECTED:
        raise RuntimeError("依赖与服务器已验证的ursa-gpu版本不同；停止，不自动安装或升级。")
    import torch
    import rasterio
    from terratorch.registry import BACKBONE_REGISTRY
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA不可用，停止后续下载和计算。")
    value = torch.ones((64, 64), device="cuda")
    if (value @ value)[0, 0].item() != 64.0:
        raise RuntimeError("GPU基础计算检查未通过。")
    del value
    print("依赖与GPU基础计算：通过", flush=True)
    return installed


def checkpoint_path():
    return ROOT / "models" / ("terramind-small-" + REVISION) / FILENAME


def configure_download_environment():
    endpoint = os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    if endpoint not in ("https://huggingface.co", "https://hf-mirror.com"):
        raise RuntimeError("HF_ENDPOINT只允许官方站或此前使用的镜像站。")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    return endpoint


def download_checkpoint(target, downloader=None, sleep=time.sleep):
    if target.exists():
        return checked_checkpoint(target)
    if shutil.disk_usage(target.parent).free < 2 * 1024**3:
        raise RuntimeError("剩余磁盘不足2GiB；停止下载，请先确认存储空间。")
    endpoint = configure_download_environment()
    if downloader is None:
        from huggingface_hub import hf_hub_download
        downloader = hf_hub_download
    for attempt in range(1, 4):
        try:
            print(f"下载TerraMind Small官方固定权重（约504MB），第{attempt}/3次；地址：{endpoint}", flush=True)
            path = Path(downloader(repo_id=MODEL_ID, filename=FILENAME, revision=REVISION,
                                   local_dir=str(target.parent), token=False, endpoint=endpoint))
            if path.resolve() != target.resolve():
                raise RuntimeError("下载器返回位置与指定模型目录不一致。")
            return checked_checkpoint(target)
        except (OSError, TimeoutError) as exc:
            if attempt == 3:
                raise RuntimeError("权重下载未完成；保留下载缓存，可重复执行原命令续传。") from exc
            print("网络暂时失败，将重试；已下载内容交由下载器复用。", flush=True)
            sleep(3)


def check_sample(path):
    if not path.is_file() or path.is_symlink() or digest_file(path) != SAMPLE_SHA:
        raise RuntimeError("既有India官方影像缺失或SHA256不符；停止，不替换成模拟影像。")
    return path


def sample_path():
    return ROOT / "models" / ("prithvi-flood-" + SAMPLE_REVISION) / "examples/India_900498_S2Hand.tif"


def prepare():
    # Hugging Face在导入时读取部分环境变量，须先配置，再导入TerraTorch。
    configure_download_environment()
    installed = check_environment()
    sample = check_sample(sample_path())
    target = checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    download_checkpoint(target)
    manifest = {"model_id": MODEL_ID, "revision": REVISION, "checkpoint_sha256": CHECKPOINT_SHA,
                "bytes": CHECKPOINT_SIZE, "versions": installed,
                "sample_sha256": digest_file(sample), "status": "下载已核对，GPU模型验证尚未执行"}
    (target.parent / "download-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("权重下载与SHA256核对：通过。启动独立离线进程验证实际模型计算……", flush=True)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "PYTHONIOENCODING": "utf-8"}
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--smoke"], env=env, check=True)


def smoke():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    import numpy as np
    import rasterio
    import torch
    from rasterio.windows import Window
    from terratorch.registry import BACKBONE_REGISTRY
    from terratorch.models.backbones.terramind.model.terramind_register import v1_pretraining_mean, v1_pretraining_std
    installed = check_environment()
    target, sample = checked_checkpoint(checkpoint_path()), check_sample(sample_path())
    input_before = digest_file(sample)
    with rasterio.open(sample) as src:
        if src.count != 13 or src.width < 224 or src.height < 224:
            raise RuntimeError("已知官方S2L1C影像结构不符。")
        row, column = (src.height - 224)//2, (src.width - 224)//2
        window = Window(column, row, 224, 224)
        raw = src.read(window=window).astype("float32")
        valid = (src.read_masks(window=window) > 0).all(axis=0) & np.isfinite(raw).all(axis=0)
        grid = {"window_rows": [row, row+224], "window_columns": [column, column+224],
                "crs": str(src.crs), "transform": list(src.window_transform(window))}
    if not valid.any() or (raw[:, valid] < 0).any():
        raise RuntimeError("固定检查窗口无可用像元或编码不符。")
    modality_key = "untok_sen2l1c@224"
    mean = np.asarray(v1_pretraining_mean[modality_key], dtype="float32").reshape(13, 1, 1)
    std = np.asarray(v1_pretraining_std[modality_key], dtype="float32").reshape(13, 1, 1)
    # TerraMind预训练统计基于原始DN；不复用Prithvi的0.0001缩放。
    normalized = (raw - mean) / std
    normalized[:, ~valid] = 0
    print("创建Small原始输入编码器，核对全部所需参数……", flush=True)
    with offline_network():
        model = BACKBONE_REGISTRY.build("terramind_v1_small", pretrained=False, modalities=MODALITIES, img_size=224)
        weights = torch.load(target, map_location="cpu", weights_only=True)
        if "state_dict" in weights:
            weights = weights["state_dict"]
        selected = select_complete_state(weights, model.state_dict())
        selected_count, extra_count = len(selected), len(weights)-len(selected)
        model.load_state_dict(selected, strict=True)
        del weights, selected
        model = model.eval().to("cuda")
        tensor = torch.from_numpy(np.ascontiguousarray(normalized)).unsqueeze(0).to("cuda")
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.float16):
            layers = model({"S2L1C": tensor})
        torch.cuda.synchronize()
        elapsed = time.perf_counter()-started
        if not isinstance(layers, list) or len(layers) != 12:
            raise RuntimeError("Small编码器返回的层数不符。")
        if any(tuple(layer.shape) != (1, 196, 384) or not torch.isfinite(layer).all().item() for layer in layers):
            raise RuntimeError("真实影像特征的形状或数值未通过核验。")
        features = layers[-1][0].float().cpu().numpy().reshape(14, 14, 384)
        if float(features.std()) < 1e-6:
            raise RuntimeError("模型返回退化特征，停止交付。")
        info = {"gpu": torch.cuda.get_device_name(0), "forward_seconds": elapsed,
                "peak_allocated_gpu_mib": torch.cuda.max_memory_allocated()/1024**2,
                "precision": "float16 autocast", "loaded_encoder_keys": selected_count,
                "unused_checkpoint_keys": extra_count,
                "unused_note": "仅使用原始输入编码器；官方完整文件中其他解码器等参数未使用。全部所需编码器参数均从权重加载。"}
    if digest_file(sample) != input_before:
        raise RuntimeError("验证期间输入发生变化，停止交付。")
    output_root = Path(os.environ.get("URSA_RESULTS_DIR", str(ROOT / "results")))
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="terramind_small_"+datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=output_root))
    np.save(output / "embedding.npy", features, allow_pickle=False)
    reloaded = np.load(output / "embedding.npy", allow_pickle=False)
    if not np.array_equal(reloaded, features):
        raise RuntimeError("保存后的特征不一致。")
    summary = {"status": "gpu_feature_smoke_passed", "completed_at": datetime.now(timezone.utc).isoformat(),
               "model_id": MODEL_ID, "revision": REVISION, "checkpoint_sha256": CHECKPOINT_SHA,
               "source_sha256": input_before, "input": "India_900498_S2Hand.tif", "input_modality": "S2L1C",
               "configured_modalities": MODALITIES, "tested_modalities": ["S2L1C"],
               "grid": grid, "valid_pixels": int(valid.sum()), "feature_shape": list(features.shape),
               "normalization": {"unit": "DN/10000编码的原始DN值，未乘0.0001", "means": mean.ravel().tolist(), "stds": std.ravel().tolist()},
               "weights_complete": True, "network_during_model_forward": "blocked", "versions": installed,
               "runtime": info, "task_head_installed": False, "accuracy_evaluated": False,
               "ground_truth_read": False, "paid_api_calls": 0,
               "scope": "基础编码器部署与真实GPU计算验证；特征可视化不是地物分类，不代表分割或其他任务已经接入。"}
    write_report(output, raw, valid, features, summary)
    summary["artifacts_sha256"] = {path.name: digest_file(path) for path in output.iterdir() if path.is_file()}
    (output / "deployment-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("TerraMind Small：权重完整加载与真实影像GPU计算通过。", flush=True)
    print("本次安装的是基础编码器；尚未安装分类、变化检测等任务输出模块。", flush=True)
    print("报告路径：" + str(output / "result.html"), flush=True)
    print("在Windows PowerShell取回报告：", flush=True)
    print(f'scp -P 22022 "{getpass.getuser()}@127.0.0.1:{output.as_posix()}/result.html" "$env:USERPROFILE\\Desktop\\ursa-terramind-small-result.html"', flush=True)
    print('Invoke-Item "$env:USERPROFILE\\Desktop\\ursa-terramind-small-result.html"', flush=True)


def write_report(output, raw, valid, features, summary):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rgb = np.moveaxis(raw[[3, 2, 1]], 0, -1).copy()
    for channel in range(3):
        lo, hi = np.percentile(rgb[..., channel][valid], [2, 98])
        rgb[..., channel] = np.clip((rgb[..., channel]-lo)/max(float(hi-lo), 1), 0, 1)
    rgb[~valid] = 0
    flat = features.reshape(-1, features.shape[-1])
    centered = flat-flat.mean(axis=0, keepdims=True)
    u, s, _ = np.linalg.svd(centered, full_matrices=False)
    projection = (u[:, :3]*s[:3]).reshape(14, 14, 3)
    for channel in range(3):
        lo, hi = np.percentile(projection[..., channel], [2, 98])
        projection[..., channel] = np.clip((projection[..., channel]-lo)/max(float(hi-lo), 1e-6), 0, 1)
    fig, axes = plt.subplots(1, 2, figsize=(9, 4.6))
    for axis, title, value in zip(axes, ["Input crop (display stretch)", "Feature PCA (not land-cover classes)"], [rgb, projection]):
        axis.imshow(value, interpolation="nearest")
        axis.set_title(title, fontsize=11)
        axis.axis("off")
    fig.tight_layout()
    image = output / "feature-preview.png"
    fig.savefig(image, dpi=130, facecolor="white")
    plt.close(fig)
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    body = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TerraMind Small部署验证</title><style>
    *{box-sizing:border-box}body{background:#f3f4ee;color:#19343c;font:17px/1.8 "Microsoft YaHei",sans-serif;margin:0}main{max-width:1120px;margin:auto;padding:24px}.slide{background:#fffefb;border:1px solid #d4ded8;padding:30px;border-radius:12px;margin-bottom:25px}h1{font-size:34px;line-height:1.4}.note{background:#fff0db;border-left:4px solid #89521c;padding:16px}img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}@media(max-width:600px){main{padding:12px}.slide{padding:20px}h1{font-size:27px}}@media print{.slide{break-before:page}}</style><main>
    <section class="slide"><h1>Small基础模型已在GPU上完成真实影像计算</h1><p>官方固定权重通过SHA256校验，当前编码器所需参数完整加载；计算阶段已禁止网络连接。使用此前已下载的India官方影像中心224×224窗口，输出14×14×384特征。没有读取参考答案或调用付费模型。</p><p class="note">这证明基础模型可以加载和计算。当前没有安装任务输出模块，不能把下面的彩色特征图当成地物分类，也不能据此评价准确率。</p></section>
    <section class="slide"><h2>左：真实影像窗口；右：特征的三个主成分</h2><img alt="真实影像与非语义特征可视化" src="data:image/png;base64,''' + encoded + '''"><p>特征图每个位置对应输入中的一个16×16块，颜色仅展示数值差异，没有水体、建筑或植被类别含义。本次只验证S2L1C输入分支，其他模态尚未启用或测试。</p></section>
    <section class="slide"><h2>验证记录与后续工作</h2><p>后续需要为地物分类、火烧迹地或变化检测安装或训练对应输出模块，再分别用独立标注评价。当前Prithvi服务、环境包和系统API配置未修改。</p><details><summary>展开模型、输入及耗时</summary><pre>''' + html.escape(json.dumps(summary, ensure_ascii=False, indent=2)) + "</pre></details></section></main></html>"
    (output / "result.html").write_text(body, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="TerraMind Small固定版本部署；默认下载后执行离线GPU验证。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="仅检查依赖及GPU，不下载权重")
    modes.add_argument("--smoke", action="store_true", help="仅使用已安装文件执行离线GPU验证")
    args = parser.parse_args(argv)
    if args.check:
        check_environment()
    elif args.smoke:
        smoke()
    else:
        prepare()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("TerraMind部署未完成：" + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)
