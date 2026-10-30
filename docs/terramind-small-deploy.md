# TerraMind Small基础编码器部署

本批提供独立部署入口：[deploy.py](../deploy/terramind_gpu/deploy.py)。它复用服务器已经验证的`ursa-gpu`环境，下载固定官方Small权重，严格核对当前编码器所需参数，并对既有真实影像执行一次GPU特征计算。没有修改Agent路由、消息、GUI、Prithvi模型或API配置。

## 适用环境

- Ubuntu，已有可用的NVIDIA GPU。
- Python 3.11、torch 2.10.0+cu128、torchvision 0.25.0+cu128、TerraTorch 1.2.11、torchgeo 0.8.1、numpy 2.2.6、rasterio 1.4.4。
- 已有`~/ursa-gpu-runtime/run-python.sh`，用于保留此前验证过的环境和C++运行库加载方式。
- 已有前一批下载的固定India官方影像，见脚本中的`sample_path()`和`SAMPLE_SHA`；脚本不接受随意替换的输入。
- 下载前至少2GiB可用空间。脚本不自动安装、降级或升级包，环境不符时停止。

此环境要求用于服务器外部模型进程；主项目仍按根目录`environment.yml`重建，不将该GPU依赖清单混入Windows客户端环境。

## 上传与执行

保持已经建立的SSH转发。将脚本上传到GPU服务器用户home，命名为`ursa-terramind-small-20261027.py`。例如在项目根目录的Windows PowerShell中，先将`GPU_USER`替换为真实服务器用户名：

```powershell
scp -P 22022 .\deploy\terramind_gpu\deploy.py GPU_USER@127.0.0.1:ursa-terramind-small-20261027.py
ssh -p 22022 GPU_USER@127.0.0.1
```

然后在Ubuntu服务器运行：

```bash
bash ~/ursa-gpu-runtime/run-python.sh ~/ursa-terramind-small-20261027.py
```

脚本先验证依赖、GPU基础运算和既有样本，再下载固定权重。默认使用此前已验证可达的`hf-mirror.com`；可通过单次进程的`HF_ENDPOINT=https://huggingface.co`指定官方站。下载镜像不改变官方版本、文件大小或SHA256检查。下载最多尝试3次，缓存保留以便续传；已验证的文件复用，损坏的已有文件拒绝并保留。

之后独立子进程执行离线模型计算。模型构造使用`pretrained=False`避免隐式下载，再从已校验的官方文件筛出所需编码器参数；缺失或形状不符一律停止，不用随机初始化补齐。其他模态和解码器的未使用参数数量会记录。

TerraMind按自身S2L1C预训练均值和标准差处理原始DN；不沿用Prithvi的0.0001缩放。模型当前仅启用S2L1C，取固定影像中心224×224窗口。验证12层输出均为`1×196×384`且数值有限，保存最后一层`14×14×384`特征，并读回检查。

## 成功与结果

只有打印`TerraMind Small：权重完整加载与真实影像GPU计算通过。`才表示此项真实GPU验证成功。单纯下载成功不作成功交付。

每次验证新建结果目录，默认位于`~/ursa-gpu-runtime/results/terramind_small_日期_时间_随机标识/`；如果外层明确提供`URSA_RESULTS_DIR`，则在该目录下保存独立子目录。原样本和旧任务产物不修改。

产物包括`embedding.npy`、`feature-preview.png`、`deployment-result.json`和内嵌图片的`result.html`。控制台会打印取回HTML的scp命令。报告仅证明基础编码器部署和真实计算；PCA颜色不是地物类别，当前没有任务输出头，也没有新增常驻HTTP服务或Agent调用接口。

本轮没有读取参考答案、调用DeepSeek或其他收费模型。分类、变化检测等能力应另外安装任务头或微调，再分别做独立评价。

## 固定模型与回滚

- 官方模型：[ibm-esa-geospatial/TerraMind-1.0-small](https://huggingface.co/ibm-esa-geospatial/TerraMind-1.0-small)。
- 提交：`960f7549bfe9d7a08946860042f83badd604c779`。
- 权重：`TerraMind_v1_small.pt`，504231662字节。
- SHA256：`755e9cce9483fd61334ef66c79f805406db5151a8b44a685c8fbbe023c684701`。
- 官方用法：[IBM/terramind](https://github.com/IBM/terramind)。

当前是一次性验证进程；退出后没有新服务需要关闭。停止使用本入口即可，不需要删除既有模型、结果或环境。项目原有入口和运行路径不变。

## 开发阶段验证快照与状态

部署测试使用临时小文件、合成数组、模拟下载器和模型参数形状；不下载真实权重、不访问服务器、不调用付费模型。真实CLI帮助入口、网络连接阻断、报告绘制及子进程失败传播有覆盖。开发阶段未执行GPU前向；后续真实运行见下方补记，不能以本地测试替代。

新增20项测试；本任务完整Python回归141项通过、Node界面22项通过，`git diff --check`通过。独立分支基线2115157原有121项Python测试，不能与含其他未提交升级的工作区测试数量直接比较。

分支`codex/terramind-small-deploy`，起点为本地2115157，未fetch；未提交、未推送、未创建PR、未审批、未合并。详见[图文部署记录](fixes/2026-10-27-018-terramind-small-deploy.html)。

## 真实部署验证补记：2026-10-27

用户在Ubuntu / RTX 4090 D上运行部署入口后，取回本次自包含HTML报告。本机已读取报告，并核对模型提交、权重与输入哈希声明、依赖版本、输入范围、特征形状以及内嵌PNG解码；与固定部署约定一致。

- 报告状态：`gpu_feature_smoke_passed`。
- 编码器所需149个参数条目完整加载；其他未启用组件的201项权重未使用，不属于当前编码器缺失。
- 使用已约定India影像中心224×224窗口，50,176个有效像元；本次仅验证S2L1C分支。
- 输出特征14×14×384；单次前向0.079794秒，不含下载、加载及制图，不作为多次重复基准。
- 本进程PyTorch峰值已分配显存151.716MiB，不代表GPU总占用，也不能用于推断训练显存。
- 未安装任务输出头、未做分类精度评价、未接入常驻服务或Agent。

这是用户服务器真实运行报告的回传核对，没有重新执行GPU推理。未取回embedding.npy或远端完整权重进行独立重算；原始报告未修改。本地回传文件、提取图片和核验JSON保留在独立资料目录，不纳入Git。

本次仅补充文档，检查内容、链接和格式；未重复运行Python/Node回归，上方141/22是开发批次快照。
