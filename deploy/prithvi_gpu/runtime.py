"""复用已验收的完整任务权重；与首轮批测保持同一预处理与前向方式。"""
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
import time

import prithvi_protocol as protocol


class GPUModel:
    def __init__(self):
        import numpy as np
        import torch
        import yaml
        from terratorch.tasks import SemanticSegmentationTask
        from terratorch.utils import remove_unexpected_prefix
        from terratorch.datamodules.sen1floods11 import MEANS, STDS
        if version("terratorch") != "1.2.11" or not torch.cuda.is_available():
            raise RuntimeError("需要已验证的TerraTorch 1.2.11和可用CUDA。")
        root = Path.home()/"ursa-gpu-runtime/models"/("prithvi-flood-"+protocol.REVISION)
        checkpoint = root/"Prithvi-EO-V2-300M-TL-Sen1Floods11.pt"
        if protocol.file_sha(checkpoint) != protocol.CHECKPOINT_SHA or protocol.file_sha(root/"config.yaml") != protocol.CONFIG_SHA:
            raise ValueError("已安装权重或配置与固定官方版本不一致。")
        config = yaml.safe_load((root/"config.yaml").read_text(encoding="utf-8"))
        if config["model"]["class_path"] != "terratorch.tasks.SemanticSegmentationTask":
            raise ValueError("模型任务类与官方配置不一致。")
        args = deepcopy(config["model"]["init_args"])
        bands = ["BLUE", "GREEN", "RED", "NIR_NARROW", "SWIR_1", "SWIR_2"]
        if args["model_args"]["backbone_bands"] != bands or args["model_args"]["num_classes"] != 2:
            raise ValueError("模型波段或类别不符合固定配置。")
        args["model_args"]["backbone_pretrained"] = False
        torch.manual_seed(0)
        task = SemanticSegmentationTask(**args)
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
        weights = remove_unexpected_prefix(weights.get("state_dict", weights))
        weights = {key.removeprefix("model."): value for key, value in weights.items()}
        task.model.load_state_dict(weights, strict=True)
        del weights
        self.model = task.model.eval().to("cuda")
        self.means = np.asarray([MEANS[band] for band in bands], dtype="float32").reshape(6, 1, 1)
        self.stds = np.asarray([STDS[band] for band in bands], dtype="float32").reshape(6, 1, 1)
        self.info = {"model_id": "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-TL-Sen1Floods11",
                     "revision": protocol.REVISION, "gpu": torch.cuda.get_device_name(0), "precision": "float16 autocast",
                     "bands": bands, "means": self.means.ravel().tolist(), "stds": self.stds.ravel().tolist(),
                     "versions": {name: version(name) for name in ["torch", "torchvision", "terratorch", "torchgeo", "numpy", "rasterio"]}}

    def predict(self, raw, valid):
        import numpy as np
        import torch
        normalized = (raw.astype("float32")*np.float32(.0001)-self.means)/self.stds
        tensor = torch.from_numpy(np.ascontiguousarray(normalized)).unsqueeze(0).to("cuda")
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.float16):
            logits = self.model(tensor).output
        torch.cuda.synchronize()
        elapsed = time.perf_counter()-started
        if tuple(logits.shape) != (1, 2, *valid.shape) or not torch.isfinite(logits).all().item():
            raise ValueError("模型输出形状不符或包含非有限值。")
        return logits.argmax(dim=1)[0].cpu().numpy(), {"forward_seconds": elapsed,
                "peak_allocated_gpu_mib": torch.cuda.max_memory_allocated()/1024**2,
                "timing_note": "仅前向耗时，不含加载、预处理、传输或制图；不是端到端响应时间。"}
