"""复用服务器已验收权重的TerraMind常驻运行时；不安装依赖、不下载或读取标签。"""
import os
import random
import time
import terramind_protocol as protocol
import deploy
import landcover


class GPUModel:
    def __init__(self):
        os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG",":4096:8")
        import torch
        self.torch=torch
        installed=deploy.check_environment()
        dependency=landcover.check_generation_capability()
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        torch.use_deterministic_algorithms(True)
        with deploy.offline_network():
            self.model,loading=landcover.create_model()
        if loading!={"all_loaded_state_keys":600,"tokenizer_state_keys":310,"random_fallback_keys":0}:
            raise ValueError("生成网络状态条目与已验收Small结构不符。")
        from terratorch.models.backbones.terramind.model.terramind_register import v1_pretraining_mean
        self.mean=v1_pretraining_mean["untok_sen2l2a@224"]
        self.info={"gpu":torch.cuda.get_device_name(0),"versions":{**installed,"diffusers":dependency["diffusers"]},
                   "weight_loading":loading,"seed":protocol.SEED,"precision":"float32",
                   "input":"Sentinel-2 L2A / raw DN / 12 spectral bands / 224x224",
                   "limitation":"生成式地物候选，不是已证明精度的任务分割头；不读取参考答案"}

    def predict(self,raw,valid):
        import numpy as np
        torch=self.torch
        value=raw.copy()
        for channel,mean in enumerate(self.mean):
            value[channel,~valid]=mean
        random.seed(protocol.SEED)
        torch.manual_seed(protocol.SEED)
        torch.cuda.manual_seed_all(protocol.SEED)
        tensor=torch.from_numpy(np.ascontiguousarray(value)).unsqueeze(0).to("cuda")
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started=time.perf_counter()
        with deploy.offline_network(),torch.inference_mode():
            output=self.model({"S2L2A":tensor},verbose=False)
        torch.cuda.synchronize()
        elapsed=time.perf_counter()-started
        prediction=landcover.decode_labels(output["LULC"].float().cpu().numpy())
        prediction[~valid]=255
        return prediction,{"forward_seconds":elapsed,"peak_allocated_gpu_mib":torch.cuda.max_memory_allocated()/1024**2}
