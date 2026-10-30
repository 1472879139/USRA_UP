# 对接开发者：从 GitHub 拉取、配置、运行与模型 API 联调

适用日期：2026-10-30。仓库：[1472879139/USRA_UP](https://github.com/1472879139/USRA_UP)。

## 1. 先看当前完成到哪里

| 部分 | 当前状态 | 由谁维护 |
|---|---|---|
| 本机网页与自主 Agent | 可运行；DeepSeek 可以回答、调用既有工具或请求隔离 Python 执行 | 应用开发者 |
| DA V2 专用能力 | 已在内网4090D部署，统一API可提取候选田块 | 模型维护者 |
| TerraMind Small + LULC | 已在4090D部署，统一API可生成地物候选类别 | 模型维护者 |
| 云端 HTTPS 与内网中转 | 已配置、认证、自动重启；维护者 Windows 已完成认证查询和9份产物下载核验 | 模型维护者 |
| 新统一API接入网页 | **尚未完成**；需要应用开发者接SDK、两组API配置和调度 | 应用开发者 |

因此，现在可以分别运行“最新应用”和“新模型API的SDK/模拟服务”。**当前网页没有“填完两组API就自动调用新服务”的完整入口。** 不要把统一API地址直接填进旧的 `gpu.local.json`：旧 Prithvi/TerraMind 客户端的路径和返回格式不同，会调用失败。

模型 API 封装已经完成；“网页接入完成”是另外一项工作。TerraMind 当前只接入地物分类，并不意味着已经具备任意遥感任务能力。

## 2. 两个人分别拉哪个分支

| 分支 | 内容 | 建议用途 |
|---|---|---|
| `codex/autonomous-task-agent` | 最新应用、DeepSeek自主循环、既有工具、隔离代码执行及界面 | 对方从这里建立自己的应用开发分支 |
| `codex/model-service-handoff` | 统一模型服务、标准库SDK、输入约束、模拟服务和部署脚本 | 你维护服务器；对方查看契约、运行SDK联调 |

`main` 不包含以上全部新功能，先拉下面指定分支。任务分支发布不代表已经审查或合并main。本轮未创建PR、未批准、未合并。

以下“首次拉取”在一个准备存放项目的新父目录执行，目标文件夹不能已经存在。Windows使用PowerShell；Linux可使用等效命令。**模型服务与应用建议放成两个并列目录，不要互相覆盖。**

### 首次拉取最新应用

```powershell
git clone --branch codex/autonomous-task-agent https://github.com/1472879139/USRA_UP.git USRA_UP-app
if ($LASTEXITCODE -ne 0) { throw '克隆失败，请先处理网络或仓库访问问题' }
Set-Location -LiteralPath .\USRA_UP-app
git remote -v
git status --short
git switch -c codex/model-api-client
```

最后一行创建应用开发者自己的分支，不要直接和维护者共同修改同一个任务分支。若已有项目，先读 `AGENTS.md`、`CONTRIBUTING.md` 并检查现有分支和未提交改动，再 fetch、切换相应分支；不要用重新克隆覆盖、reset或clean丢掉自己的工作。

### 再拉模型接口源码到并列目录

回到上述父目录：

```powershell
Set-Location ..
git clone --branch codex/model-service-handoff https://github.com/1472879139/USRA_UP.git USRA_UP-model-api
if ($LASTEXITCODE -ne 0) { throw '模型接口源码克隆失败' }
Set-Location -LiteralPath .\USRA_UP-model-api
```

只用SDK访问远程模型，不需要在本机启动真实GPU服务。应用侧需要的主要文件为 `model_client.py`；接口以 `/openapi.json` 和能力目录为准。服务端契约见[模型服务交接说明](https://github.com/1472879139/USRA_UP/blob/codex/model-service-handoff/docs/model-service-handoff.md)。

## 3. 对方需要准备和填写什么

| 项目 | 从哪里获取 | 在哪里填写/保存 | 什么时候需要 |
|---|---|---|---|
| DeepSeek `base_url` | 自己所用API服务商控制台或维护者提供的配置说明 | 应用目录 `config.json` | 运行真实Agent |
| DeepSeek `api_key` | 自己创建，或由维护者通过私密渠道分配 | 同上 | 运行真实Agent |
| DeepSeek `model` | 同一服务商实际支持的模型名 | 同上；不能假定示例模型名可用 | 运行真实Agent |
| GPU `base_url` 和 `api_key` | 模型维护者提供的 `application-dev.local.json` | 模型接口目录的本机私有文件；以后接入应用配置 | 真实模型API联调 |
| TIFF与元数据 | 自己的数据、维护者单独给的公开样例，或第6节下载 | 本机 `data/`，在网页选中对应影像 | 图像分析；纯解释可不选数据 |
| Docker Linux容器运行时 | 本机安装并启动Docker Desktop/Linux引擎，再构建项目镜像 | 不写入API配置 | 让Agent执行临时生成的Python |
| 参考标签/答案 | 仅独立评价时另行获取 | 独立评价环境，不能交给执行Agent | 不影响普通运行，不是必需品 |

不需要获取：4090D的SSH密码/私钥、云服务器管理员密钥、模型权重、维护者的 `config.json`、历史对话或全部历史结果。

真实密钥不在GitHub中。没有收到GPU连接文件时，仍可先运行模拟服务开发界面。不要将连接文件中的真实地址/密钥复制进示例配置、截图或提交。

## 4. 创建本机应用环境并启动

以下Conda/Python命令在 `USRA_UP-app` 根目录执行。Windows建议使用安装自带的Anaconda/Miniconda Prompt，先激活环境；该终端的CMD可用 `cd /d` 进入项目。前面的Git、后面的文件复制和下载示例采用PowerShell，PowerShell进入目录使用 `Set-Location`。

### 首次创建环境

```powershell
conda env create -f environment.yml
```

若已有 `ursa-up`，先核对是否为这个项目环境，不要重复创建或覆盖。环境先由Conda提供NumPy、GDAL/Rasterio等编译依赖，再装pip层。不要另外执行 `pip install rasterio gdal` 覆盖它们。

在Anaconda/Miniconda Prompt激活环境并检查依赖。后续全部 `python` 命令均在这个已激活的环境运行；切换到另一个项目目录后环境仍然有效。

```powershell
conda activate ursa-up
python -c "import rasterio,numpy,flask; print('应用依赖可导入')"
```

在项目目录复制配置（下面是PowerShell命令）：

```powershell
if (-not (Test-Path -LiteralPath config.json)) {
    Copy-Item -LiteralPath config.example.json -Destination config.json
}
New-Item -ItemType Directory -Force -Path data | Out-Null
```

用编辑器打开 `config.json`，仅按自己服务商实际信息填写 `model`、`api_key`、`base_url`，其余先保留示例。不要把GPU服务地址填入DeepSeek的 `base_url`。`data_path` 默认 `data`，`results_path` 默认 `results`。

回到已激活环境的终端，启动最新自主入口：

```powershell
python main.py --autonomous-agent --port 7864
```

浏览器打开 `http://127.0.0.1:7864`，顶部应显示“自主Agent”。只用 `python main.py` 会启动原来的默认路径。终端持续等待是服务运行状态，停止时按Ctrl+C。

旧Conda（例如维护者Windows的4.5.11）没有 `conda run`，PowerShell也可能不支持直接activate。此时使用Anaconda Prompt，不需要为了启动项目升级GIS依赖或修改系统PATH。如果必须从PowerShell启动，可把下面的安装路径替换为自己真实Conda路径：

```powershell
cmd /d /c 'call "C:\你的Conda安装目录\Scripts\activate.bat" ursa-up && python main.py --autonomous-agent --port 7864'
```

可以不选影像先问“NDVI是什么，简单解释一下”。这会请求真实DeepSeek并按所用服务商计费；离线回归测试不会使用真实密钥收费调用。没有真实API配置时，先做第5节的模拟接口测试。

### 需要临时生成代码时，再准备Docker

启动Docker Desktop并确保使用Linux容器引擎。在应用根目录执行：

```powershell
docker info
python deploy/python_runtime/prepare.py
```

准备脚本会构建 `ursa-python-runtime:1`，首次需要下载基础镜像及依赖。准备完成后正常分析不会自行安装包。未准备Docker时网页仍可打开，但Agent选择 `run_python` 会返回运行时不可用；不会改成直接在宿主机执行。

## 5. 不消耗DeepSeek或GPU资源的模拟联调

在 `USRA_UP-model-api` 根目录、具备前述项目环境后执行一次：

```powershell
python -m model_service.prepare_demo examples/model_api/mock-fixtures
```

它生成小型合成TIFF、metadata和带水印的模拟结果，目标目录必须尚不存在。以后无需重复生成。GitHub不保存这些可重建的影像/产物。

启动模拟服务：

```powershell
python -m model_service serve --mode mock --data-dir .model-service/mock --port 18767
```

另开终端，进入同一个 `USRA_UP-model-api` 目录：

```powershell
python examples/model_api/call_api.py --mock --input examples/model_api/mock-fixtures/input.tif --metadata examples/model_api/mock-fixtures/metadata.json --capability da-v2-field-delineation --output .model-service/demo-da
python examples/model_api/call_api.py --mock --input examples/model_api/mock-fixtures/input.tif --metadata examples/model_api/mock-fixtures/metadata.json --capability terramind-small-landcover --output .model-service/demo-terramind
```

应看到任务ID、成功状态和产物下载核验。图片有 `MOCK - SYNTHETIC DEMO` 水印，结果字段为 `execution_mode=mock`。它不分析上传影像，不能用于评价识别好坏。

只有生成合成fixtures的步骤需要GIS依赖。准备好之后，模拟服务与SDK本身只依赖Python标准库；应用开发者无需先安装CUDA或遥感模型。

## 6. 真实模型联调需要什么数据

### 先确认连接，不执行推理

让维护者通过私密渠道提供 `application-dev.local.json`，放到 `USRA_UP-model-api` 根目录。文件结构为：

```json
{
  "base_url": "https://MODEL_HOST/ursa-model-api",
  "api_key": "由维护者单独分配的模型服务密钥"
}
```

占位内容不能直接请求；实际文件还可能有owner字段，SDK不依赖它。不要提交该文件。在这个目录执行：

```powershell
python -c "import json; from model_client import ModelClient; c=json.load(open('application-dev.local.json',encoding='utf-8')); s=ModelClient(c['base_url'],c['api_key']); print(s.health()); print(s.capabilities())"
```

应显示 `api_version=1.0.0`、`execution_mode=gpu`、`status=ready` 以及两个能力。health不需要认证；能力查询成功才验证了密钥。`configured`表示模型运行时已配置，不代表已经独立验证识别准确率。

如果握手中断，先核对这台电脑的代理/TUN规则。维护者电脑曾在代理调整后恢复，但不要求每台电脑永久关闭TUN。保持HTTPS证书校验，不改成公网明文HTTP。

### 获取一幅已使用的公开样例

可由维护者直接给样例TIFF，也可按以下固定版本下载。该公开样例用于连接/封装回归，不是新的独立精度测试；不需要下载对应ESRI参考产品或人工答案。

在 `USRA_UP-model-api` 根目录：

```powershell
New-Item -ItemType Directory -Force -Path data/model_api_demo | Out-Null
$sampleUrl = 'https://raw.githubusercontent.com/IBM/terramind/53d61da09213c2c3629931b6322f94cce617886a/examples/S2L2A/38D_378R_2_3.tif'
if (Test-Path -LiteralPath data/model_api_demo/image.tif) { throw '目标已存在，请先核对，不要覆盖自己的数据' }
Invoke-WebRequest -Uri $sampleUrl -OutFile data/model_api_demo/image.tif
$sampleHash = (Get-FileHash -LiteralPath data/model_api_demo/image.tif -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sampleHash -ne 'c31f749d89c5bec772204aa1d0436a490b1a14ead7a152b66884fc247055acdc') { throw '样例SHA256不一致，停止联调' }
```

GitHub下载受限时，请维护者单独传输同SHA256文件，不要改权重或输入校验规则绕过。该样例的元数据已放在 `examples/model_api/input-metadata.example.json`，仅适用于这个已核对来源和编码的样例；不能将同一份声明套在任意图片上。

### 分别运行两个真实模型

```powershell
python examples/model_api/call_api.py --connection-file application-dev.local.json --input data/model_api_demo/image.tif --metadata examples/model_api/input-metadata.example.json --capability da-v2-field-delineation --output .model-service/real-da
python examples/model_api/call_api.py --connection-file application-dev.local.json --input data/model_api_demo/image.tif --metadata examples/model_api/input-metadata.example.json --capability terramind-small-landcover --output .model-service/real-terramind
```

每次运行会使用4090D真实推理、占用队列并保存产物；重复测试前先协调资源。SDK自动核对每份下载的SHA256。左右预览图、分类TIFF、统计CSV和来源JSON在输出目录中，DA额外提供边界GeoJSON。没有独立参考样本时，仍需保留“候选、尚未验证准确率”的说明。

### 换成自己的数据时

当前新API只支持：Sentinel-2 L2A、12个明确波段、32～1024像元/边、北向10米像元、单位为米的投影坐标、普通TIFF不超过16MiB。波段集合为 `B1 B2 B3 B4 B5 B6 B7 B8 B8A B9 B11 B12`。metadata必须明确 `dn_10000` 或 `reflectance_0_1` 编码。非空TIFF波段描述需与声明一致。

JPG、普通RGB、SAR、Landsat、LST、去云或变化检测不属于这两个已注册能力的当前支持范围。应用应先追问或调用其他工具，不要静默换模型后宣称完成原任务。

## 7. 对方接下来要开发什么

1. 在应用后端引入SDK，将 GPU 连接配置和DeepSeek配置分别保存；网页提供隐藏密钥输入、连通性检查与能力查询。
2. 为DeepSeek注册查询能力、提交任务、查询/取消、下载工具；保留直接回答和代码兜底，不强制每次都调用模型。
3. 专用能力适合时优先；不匹配时检查通用模型已注册任务；仍不具备条件时走DeepSeek/代码路径或追问。不要把地物分类当作田块边界提取。
4. 按 job_id 展示进度，保存幂等键以防网络重试重复推理；支持取消、失败解释和断网续查。
5. 默认显示通俗结果，专业参数可展开；始终保留候选性质、来源与未完成要求，不把接口成功或模型分数当成准确率。
6. 先模拟验收，再使用独立开发者密钥做小样例真实联调。修改接口前与模型维护者协商；不能单方面改共享字段。

迁移SDK时只需应用后端调用，不需要把模型服务包、PyTorch或权重安装到本机应用中。网页浏览器不直接持有服务密钥或跨域调用GPU服务。

## 8. 模型维护者现在需要做什么

你这边的API封装、云中转和当前两个模型已完成。暂时无需重装环境或重新下载模型。

- 给对方：本文链接、应用分支、模型服务分支，以及私下发送的 `application-dev.local.json`；必要时单独传输样例TIFF及SHA256。
- 保持GPU和云服务器开机/联网。当前API及中转是用户级systemd服务，不依赖Windows一直开着SSH窗口。
- 管理开发者密钥；不发送maintainer密钥、SSH私钥或真实DeepSeek配置。对方可以使用自己的DeepSeek账户。
- 关注磁盘和GPU占用；上次核验GPU主机home分区约剩33GiB、使用97%。API有软件配额但不会自动清理历史输入，归档按运维说明执行。
- 增加模型时先明确任务、输入、输出、固定版本、适用范围和小样例回归；先验证效果再加入能力目录。接口保持v1兼容，任务语义变化先协调。

在GPU终端查看状态：

```bash
systemctl --user status ursa-model-api.service ursa-model-api-relay.service --no-pager
curl --fail http://127.0.0.1:18767/health
```

发布/回滚、凭据与归档见[模型API运维](https://github.com/1472879139/USRA_UP/blob/codex/model-service-handoff/docs/model-api-operations.md)。不要删除旧运行时目录：当前TerraMind适配仍导入已核验的历史GPUModel代码。

## 9. 更新和验证

两位开发者各自提交到自己的任务分支，按 `CONTRIBUTING.md` 走审查流程，不直接推main。本文中的示例不是授权任何代码智能体自动推送、审批或合并。

有新版本时先检查 `git status --short`，工作区有改动先保存/协调，再fetch并选择明确来源；不要用 `reset --hard` 或 `clean` 当作更新步骤。模型端部署与应用端发布独立进行，保持契约兼容。

代码改动要求：

```powershell
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
git diff --check
```

真实模型和DeepSeek调用不属于默认回归。模型API通过不等于遥感语义准确；服务已从GPU网络完成真实推理、从维护者Windows完成认证与已封存结果下载验证，**尚未在对方的另一台真实电脑完成安装和联调**。
