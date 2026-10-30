"""Small地物类别生成：固定五例、不读取参考标签、封存后独立评价。"""
import argparse
import base64
import csv
from datetime import datetime, timezone
import getpass
import html
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import deploy

TOKENIZER_ID = "ibm-esa-geospatial/TerraMind-1.0-Tokenizer-LULC"
TOKENIZER_REVISION = "af6b7c73a36994e25df156e793901969efe88ec9"
TOKENIZER_FILE = "TerraMind_Tokenizer_LULC.pt"
TOKENIZER_SIZE = 735693536
TOKENIZER_SHA = "0e318c41490b472897b328256227c049aba8c1671a88c1ef3a1fa5026b3e0fd3"
DIFFUSERS_SHA = "114194eb61498aff06243ade750fca6fbc179ca9df68923bb175b70030bed495"
NAMES = ["无类别", "水体", "树木", "淹水植被", "农作物", "建成区", "裸地", "雪和冰", "云", "草灌覆盖"]
COLORS = ["#000000", "#3171AD", "#469C76", "#83CA70", "#EAE159", "#C07CB8", "#C19368", "#6FB2E4", "#F1F1F1", "#C66526"]
HERE = Path(__file__).resolve().parent


def load_protocol():
    protocol = json.loads((HERE / "landcover-manifest.json").read_text(encoding="utf-8"))
    if protocol["schema"] != "ursa.terramind.landcover.v1":
        raise RuntimeError("输入协议版本不符。")
    return protocol


def tokenizer_path():
    return deploy.ROOT / "models" / ("terramind-lulc-" + TOKENIZER_REVISION) / TOKENIZER_FILE


def checked_tokenizer(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != TOKENIZER_SIZE:
        raise RuntimeError("LULC解码器文件缺失或体积不符。")
    if deploy.digest_file(path) != TOKENIZER_SHA:
        raise RuntimeError("LULC解码器SHA256不符；未覆盖已有文件。")
    return path


def ensure_generation_dependency():
    """保留已有diffusers；仅缺失时安装固定版本，不解析安装其他依赖。"""
    try:
        installed = version("diffusers")
    except PackageNotFoundError:
        installed = None
    if installed is None:
        print("补充生成依赖diffusers==0.30.0（约2.6MB），不升级其他包……", flush=True)
        with tempfile.TemporaryDirectory(prefix="ursa-diffusers-") as folder:
            requirements = Path(folder) / "requirements.txt"
            requirements.write_text("diffusers==0.30.0 --hash=sha256:" + DIFFUSERS_SHA + "\n", encoding="utf-8")
            subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                            "--no-deps", "--only-binary=:all:", "--require-hashes",
                            "-r", str(requirements)], check=True)
        installed = "0.30.0"
    else:
        print(f"检测到diffusers {installed}：保留已有版本，将检查地物解码所需接口。", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    return installed


def check_generation_capability():
    """只检查当前任务需要的真实导入接口；完整权重与GPU推理仍另行核验。"""
    try:
        installed = version("diffusers")
    except PackageNotFoundError as exc:
        raise RuntimeError("diffusers缺失，请先运行不带参数的准备入口。") from exc
    try:
        with deploy.offline_network():
            tokenizer = import_module("terratorch.models.backbones.terramind.tokenizer.tokenizer_register")
            vqvae = import_module("terratorch.models.backbones.terramind.tokenizer.vqvae")
            generation = import_module("terratorch.models.backbones.terramind.model.terramind_generation")
            if not tokenizer.vqvae_available:
                raise RuntimeError(f"TerraTorch记录的解码模块导入失败：{tokenizer.import_error}")
            if not callable(getattr(tokenizer, "terramind_v1_tokenizer_lulc", None)):
                raise RuntimeError("官方LULC tokenizer入口不可用。")
            if not callable(getattr(getattr(vqvae, "VQVAE", None), "decode_tokens", None)):
                raise RuntimeError("VQVAE.decode_tokens接口不可用。")
            if not callable(getattr(getattr(generation, "TerraMindGeneration", None), "forward", None)):
                raise RuntimeError("TerraMind生成接口不可用。")
    except Exception as exc:
        raise RuntimeError(f"diffusers {installed}地物解码导入预检失败：{type(exc).__name__}: {exc}。"
                           "已保留现有环境，未降级；尚未开始模型推理。") from exc
    print(f"diffusers {installed}：地物解码导入预检通过；完整权重和真实推理仍须后续核验。", flush=True)
    return {"diffusers": installed, "status": "required_imports_passed",
            "scope": "LULC tokenizer与生成入口导入；不是所有版本或其他任务的兼容性证明"}


def download_tokenizer(downloader=None, sleep=time.sleep):
    target = tokenizer_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return checked_tokenizer(target)
    if shutil.disk_usage(target.parent).free < 2 * 1024**3:
        raise RuntimeError("剩余磁盘不足2GiB，停止下载。")
    endpoint = deploy.configure_download_environment()
    if downloader is None:
        from huggingface_hub import hf_hub_download
        downloader = hf_hub_download
    for attempt in range(1, 4):
        try:
            print(f"下载官方LULC解码器（约736MB），第{attempt}/3次：{endpoint}", flush=True)
            result = Path(downloader(repo_id=TOKENIZER_ID, revision=TOKENIZER_REVISION, filename=TOKENIZER_FILE,
                                     local_dir=str(target.parent), endpoint=endpoint, token=False))
            if result.resolve() != target.resolve():
                raise RuntimeError("下载器返回了非指定路径。")
            return checked_tokenizer(target)
        except (OSError, TimeoutError) as exc:
            if attempt == 3:
                raise RuntimeError("下载未完成，保留缓存，可重新执行原命令。") from exc
            sleep(3)


def read_input(item):
    import numpy as np
    import rasterio
    path = HERE / "inputs" / (item["id"] + ".tif")
    if not path.is_file() or path.is_symlink() or deploy.digest_file(path) != item["sha256"]:
        raise RuntimeError("输入影像缺失或SHA256不符。")
    with rasterio.open(path) as src:
        if (src.count, src.height, src.width) != (12, 224, 224) or src.crs is None:
            raise RuntimeError("要求官方S2L2A十二波段224×224影像及空间参考。")
        raw = src.read().astype("float32")
        valid = (src.read_masks() > 0).all(axis=0) & np.isfinite(raw).all(axis=0)
        if not valid.any() or (raw[:, valid] < 0).any() or float(raw[:, valid].max()) <= 2:
            raise RuntimeError("输入无有效像元或不是预期原始DN。")
        grid = {"crs": src.crs.to_string(), "transform": list(src.transform)[:6],
                "height": src.height, "width": src.width}
        profile = {"driver": "GTiff", "height": src.height, "width": src.width, "count": 1,
                   "crs": src.crs, "transform": src.transform, "dtype": "uint8", "nodata": 255,
                   "compress": "deflate"}
    # 固定官方输入均完整；不静默填补缺失波段或空间空洞。
    if not valid.all():
        raise RuntimeError("官方完整输入出现无效像元，停止该样本；不改变测试范围。")
    return raw, valid, grid, profile


def select_generation_state(backbone, tokenizer, required):
    """严格核对主模型、共享embedding别名与独立tokenizer；不填随机参数。"""
    if not isinstance(backbone, dict) or not isinstance(tokenizer, dict) or not required:
        raise RuntimeError("生成权重不是有效参数字典。")
    selected, bad = {}, []
    for key, expected in required.items():
        if key.startswith("tokenizer.tok_lulc@224."):
            source, source_key = tokenizer, key[len("tokenizer.tok_lulc@224."):]
        elif key.startswith("sampler.model."):
            source, source_key = backbone, key[len("sampler.model."):]
        elif key.startswith(("encoder_embeddings.", "decoder_embeddings.")):
            source, source_key = backbone, key
        else:
            bad.append(key)
            continue
        value = source.get(source_key)
        if getattr(value, "shape", None) != expected.shape:
            bad.append(key)
        else:
            selected[key] = value
    if bad:
        raise RuntimeError(f"生成网络权重不完整：{len(bad)}项缺失或形状不符，示例{bad[:5]}；不使用随机参数补齐。")
    return selected


def create_model():
    import torch
    from terratorch.registry import FULL_MODEL_REGISTRY
    model = FULL_MODEL_REGISTRY.build("terramind_v1_small_generate", pretrained=False,
                                     modalities=["S2L2A"], output_modalities=["LULC"],
                                     standardize=True, img_size=224, decoding_steps=1, timesteps=10)
    if set(model.tokenizer) != {"tok_lulc@224"}:
        raise RuntimeError("加载了非预期的解码器，停止。")
    backbone = torch.load(deploy.checked_checkpoint(deploy.checkpoint_path()), map_location="cpu", weights_only=True)
    if "state_dict" in backbone:
        backbone = backbone["state_dict"]
    tokenizer = torch.load(checked_tokenizer(tokenizer_path()), map_location="cpu", weights_only=True)
    selected = select_generation_state(backbone, tokenizer, model.state_dict())
    counts = {"all_loaded_state_keys": len(selected),
              "tokenizer_state_keys": sum(key.startswith("tokenizer.") for key in selected),
              "random_fallback_keys": 0}
    model.load_state_dict(selected, strict=True)
    return model.eval().to("cuda"), counts


def decode_labels(logits):
    import numpy as np
    value = np.asarray(logits)
    if value.shape != (1, 10, 224, 224) or not np.isfinite(value).all():
        raise RuntimeError("LULC输出形状或数值不符。")
    return value[0].argmax(axis=0).astype("uint8")


def write_products(folder, raw, labels, valid, profile):
    import numpy as np
    import rasterio
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=False)
    prediction = labels.copy()
    prediction[~valid] = 255
    target = folder / "landcover.tif"
    with rasterio.open(target, "w", **profile) as dst:
        dst.write(prediction, 1)
        dst.update_tags(class_schema="TerraMind-v1-ESRI-0-to-9", quality="unscored_candidate")
    with rasterio.open(target) as src:
        if not np.array_equal(src.read(1), prediction) or src.crs != profile["crs"] or src.transform != profile["transform"]:
            raise RuntimeError("类别GeoTIFF回读核验失败。")
    stats = [{"code": c, "name": name, "pixels": int(((labels == c) & valid).sum()),
              "fraction_of_input": float(((labels == c) & valid).sum() / valid.sum())}
             for c, name in enumerate(NAMES)]
    with (folder / "class_statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(stats[0]))
        writer.writeheader()
        writer.writerows(stats)
    rgb = raw[[3, 2, 1]].transpose(1, 2, 0)
    lo, hi = np.percentile(rgb, [2, 98], axis=(0, 1))
    rgb = (np.clip((rgb - lo) / np.maximum(hi - lo, 1), 0, 1) ** 0.7 * 255).astype("uint8")
    palette = np.array([[int(color[i:i+2], 16) for i in (1, 3, 5)] for color in COLORS], dtype="uint8")
    combined = np.concatenate([rgb, palette[labels]], axis=1)
    Image.fromarray(combined).resize((896, 448), Image.Resampling.NEAREST).save(folder / "preview.png")
    return stats


def write_report(root, summary):
    cards = []
    for case in summary["cases"]:
        label = html.escape(case["id"])
        if case["status"] != "completed":
            cards.append(f'<article><h2>{label}：未完成</h2><p>{html.escape(case["error"])}</p></article>')
            continue
        preview = base64.b64encode((root / case["id"] / "preview.png").read_bytes()).decode()
        rows = "".join(f'<tr><td>{row["name"]}</td><td>{row["fraction_of_input"]:.1%}</td></tr>' for row in case["statistics"] if row["pixels"])
        cards.append(f'<article><h2>{label}</h2><p>左：原始影像RGB预览　右：候选地物类别</p>'
                     f'<img alt="影像与候选类别图" src="data:image/png;base64,{preview}"><table>{rows}</table></article>')
    legend = "".join(f'<span><i style="background:{color}"></i>{i} {name}</span>' for i, (name, color) in enumerate(zip(NAMES, COLORS)))
    details = html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TerraMind Small地物类别候选结果</title><style>
body{{font:17px/1.7 "Microsoft YaHei",sans-serif;background:#edf2f4;color:#152b39;margin:0}}main{{max-width:1050px;padding:30px;margin:auto}}
article,header,details{{background:white;border-radius:15px;padding:25px;margin:20px 0}}h1{{font-size:32px}}img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #ddd;padding:5px}}.legend{{display:flex;gap:15px;flex-wrap:wrap}}i{{display:inline-block;width:16px;height:16px;border:1px solid #bbb;margin-right:5px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}}@media(max-width:600px){{main{{padding:12px}}h1{{font-size:25px}}article,header,details{{padding:15px}}}}</style>
<main><header><p>独立运行 · 尚未评分</p><h1>已生成 {summary["completed"]}/{len(summary["cases"])} 幅候选类别图</h1>
<p>这些颜色是模型推测的地物类别。当前未读取参考图，不能据此承诺识别正确。</p>
<p>五幅图来自官方示例；可能与训练材料有关。后续与ESRI产品比较只能作为探索性一致性评价，不能代替人工真值或泛化测试。</p>
<div class="legend">{legend}</div></header>{"".join(cards)}
<details><summary>展开专业信息、输入与权重记录</summary><pre>{details}</pre></details></main></html>'''
    (root / "result.html").write_text(doc, encoding="utf-8")


def seal_results(root, summary):
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(root, summary)
    files = {path.relative_to(root).as_posix(): deploy.digest_file(path)
             for path in root.rglob("*") if path.is_file()}
    (root / "seal.json").write_text(json.dumps({"schema": "ursa.seal.v1", "files": files}, indent=2), encoding="utf-8")
    archive = root / (root.name + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path != archive:
                target.write(path, path.relative_to(root).as_posix())
    return archive


def run():
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    import torch
    installed = deploy.check_environment()
    capability = check_generation_capability()
    protocol = load_protocol()
    output_root = Path(os.environ.get("URSA_RESULTS_DIR", str(deploy.ROOT / "results")))
    output_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="terramind_lulc_" + datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=output_root))
    summary = {"schema": protocol["schema"], "started_utc": datetime.now(timezone.utc).isoformat(),
               "protocol_sha256": deploy.digest_file(HERE / "landcover-manifest.json"),
               "package_sha256": os.environ.get("URSA_PACKAGE_SHA256"), "versions": {**installed, "diffusers": capability["diffusers"]},
               "generation_dependency": capability,
               "checkpoint_sha256": deploy.CHECKPOINT_SHA, "tokenizer_sha256": TOKENIZER_SHA,
               "prompt": protocol["prompt"], "settings": protocol["settings"], "ground_truth_accessed": False,
               "paid_api_calls": 0, "quality": "unscored_candidate", "cases": [], "completed": 0}
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.use_deterministic_algorithms(True)
    with deploy.offline_network():
        try:
            print("创建Small完整生成网络并严格核对主权重与LULC解码器……", flush=True)
            model, summary["weight_loading"] = create_model()
            print("完整权重核验与加载：通过；未使用随机参数补齐。", flush=True)
            summary["gpu"] = torch.cuda.get_device_name(0)
        except Exception as exc:
            summary["fatal_error"] = f"{type(exc).__name__}: {exc}"
            for item in protocol["samples"]:
                summary["cases"].append({"id": item["id"], "status": "failed", "error": summary["fatal_error"]})
        else:
            for index, item in enumerate(protocol["samples"]):
                case = {"id": item["id"], "input_sha256": item["sha256"], "seed": protocol["settings"]["seed"] + index}
                print(f'[{index+1}/{len(protocol["samples"])}] {item["id"]}', flush=True)
                try:
                    raw, valid, grid, profile = read_input(item)
                    random.seed(case["seed"])
                    torch.manual_seed(case["seed"])
                    torch.cuda.manual_seed_all(case["seed"])
                    tensor = torch.from_numpy(raw.copy()).unsqueeze(0).to("cuda")
                    torch.cuda.reset_peak_memory_stats()
                    torch.cuda.synchronize()
                    started = time.perf_counter()
                    with torch.inference_mode():
                        output = model({"S2L2A": tensor}, verbose=False)
                    torch.cuda.synchronize()
                    seconds = time.perf_counter() - started
                    logits = output["LULC"].float().cpu().numpy()
                    labels = decode_labels(logits)
                    case.update(status="completed", grid=grid, valid_input_pixels=int(valid.sum()),
                                forward_seconds=seconds, peak_allocated_gpu_mib=torch.cuda.max_memory_allocated()/1024**2,
                                statistics=write_products(root / item["id"], raw, labels, valid, profile))
                    # 文件哈希不变才封存该样本。
                    read_input(item)
                    summary["completed"] += 1
                    print("  候选类别图已生成，尚未评分。", flush=True)
                    del tensor, output, logits
                except Exception as exc:
                    case.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    print("  未完成：" + case["error"], flush=True)
                summary["cases"].append(case)
    archive = seal_results(root, summary)
    print(f'本轮封存：{summary["completed"]}/{len(protocol["samples"])}完成；失败均保留，不读取答案。', flush=True)
    print("结果包：" + str(archive), flush=True)
    print("在Windows PowerShell取回：", flush=True)
    print(f'scp -P 22022 "{getpass.getuser()}@127.0.0.1:{archive.as_posix()}" "$env:USERPROFILE\\Desktop\\{archive.name}"', flush=True)
    if summary["completed"] != len(protocol["samples"]):
        raise RuntimeError("本轮存在失败，请取回结果包用于诊断；不能视为完整通过。")


def prepare():
    deploy.configure_download_environment()
    # 先核对现有依赖、权重、输入，再补充唯一生成依赖。
    deploy.check_environment()
    deploy.checked_checkpoint(deploy.checkpoint_path())
    for item in load_protocol()["samples"]:
        read_input(item)
    ensure_generation_dependency()
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
           "PYTHONIOENCODING": "utf-8"}
    # 独立进程避免父进程在补装依赖前缓存的导入失败；下载前先确认接口可用。
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--check-generation"], env=env, check=True)
    download_tokenizer()
    print("准备完成。进入独立离线进程，开始地物类别生成……", flush=True)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--run"], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description="TerraMind Small固定五例地物生成；无参考标签。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", action="store_true", help="只离线运行已准备的模型和输入")
    modes.add_argument("--check-generation", action="store_true", help="只离线检查地物解码依赖，不安装或下载")
    args = parser.parse_args()
    try:
        if args.check_generation:
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
            check_generation_capability()
        else:
            run() if args.run else prepare()
    except Exception as exc:
        print("地物生成未完整完成：" + str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
