# 两人开发交接：模型 API v1

首次从GitHub协作请先看[开发者拉取与运行说明](developer-setup.md)。下文ZIP快照是推送前保留的本地交付方式；最新应用和模型服务分别通过任务分支交付，未合并main。

目标是每台电脑运行自己的应用，在网页填写 DeepSeek 与 GPU 模型服务两组 API。模型维护者更新服务器上的模型；应用开发者通过稳定接口开发页面和调度，不需要在自己电脑安装模型或 CUDA。

本任务分支 `codex/model-service-handoff` 新增独立服务和 SDK，**没有把主网页替换为新接口**。当前主网页仍走原有路径。应用端的两组配置、工具注册和兜底策略属于下一位开发者的工作，不能把服务已上线说成网页已接好。

## 1. 分工与验收

| 负责人 | 负责模块 | 交接后的验收 |
|---|---|---|
| 你：模型维护者 | `model_service/`、服务器环境、权重、输入约束、推理适配、HTTPS、中转、密钥、版本和回归 | 用 SDK 完成上传、推理、状态查询、下载，所有产物 SHA256 一致 |
| 另一位：应用开发者 | 网页、DeepSeek 配置与调度、数据准备、用户追问、结果解释、兜底代码路径 | 模拟服务下可完成页面全流程，再更换地址和私有密钥做真实联调 |
| 双方约定 | 本文、`openapi.json`、能力 ID、状态和结果字段 | 不静默修改字段或任务含义；破坏性变更新开 API 版本 |

建议应用开发者新开 `codex/model-api-client` 分支。模型维护者继续当前模型服务分支。共享接口变更先形成说明，双方更新消费者和测试后再合并。

交接包中的应用源码快照是旧自主 Agent 工作树的**白名单源码副本**，附逐文件 SHA256 和基线说明，包含尚未提交的开发内容；它不是 GitHub 已发布版本，也不含真实配置和数据。不要直接覆盖其他人的已有工作区。首次接手可在新目录阅读、运行或逐项移植。

## 2. DeepSeek 怎样使用服务

```text
用户需求 + 数据事实
    → DeepSeek 判断任务与输入是否具备条件
    → 查询 /v1/capabilities
    → 选择满足目标的专用能力
    → 不匹配时检查通用模型的已注册能力
    → 仍不匹配时由应用的 DeepSeek/代码执行路径处理或追问
    → 核验产物 → 给出通俗说明，专业信息可展开
```

“专用 → 通用 → DeepSeek”是能力选择顺序，不是每次把三个模型全部运行。DA V2 提取候选田块，TerraMind 当前只注册地物分类，两者任务含义不同。田块模型报输入错误时，不能把同一坏数据静默交给 TerraMind 后宣称已完成田块提取。

服务不接收自然语言自由代码、不调用 DeepSeek、不掌握 DeepSeek 密钥，也不自动换模型。是否回退、用户是否接受目标变化、重试预算及最后解释，由应用端判断并记录。DeepSeek 不应把模型置信分数或接口成功状态翻译成准确率。

## 3. 当前真实能力和输入范围

| 能力 ID | 层级 | 实际输出 |
|---|---|---|
| `da-v2-field-delineation` | 专用 | 候选田块分类栅格、WGS84 边界 GeoJSON、面积统计、预览、来源记录 |
| `terramind-small-landcover` | 通用模型的已接入任务 | 10 类地物候选分类、面积统计、预览、来源记录；无逐田块边界 |

首版有意限定为已经完成波段堆叠的 Sentinel-2 L2A TIFF：

- 12 波段：`B1 B2 B3 B4 B5 B6 B7 B8 B8A B9 B11 B12`，顺序由 metadata 明示，可规范重排。
- 每边 32～1024 像元，北向、10 米像元、已知且单位为米的投影坐标，上传不超过 16 MiB，普通 TIFF。
- encoding 必须明确为 `dn_10000` 或 `reflectance_0_1`；TIFF 自带 scale=1、offset=0。不得靠大模型猜波段或数值单位。
- TIFF 的非空波段描述必须与声明一致；服务检查结构、有效区和数值，**不能独立证明调用者声明的产品来源真实**。
- 普通 JPG、三波段 RGB、未堆叠原始产品、Landsat、SAR、LST、去云、变化检测均不在当前 API 的已注册范围。应用应先追问或走其他工具，不能伪装支持。

分块在原分辨率执行：DA 256/重叠64，并按官方适配放大到512；TerraMind 224/重叠32，多数票拼接。分类保留输入网格，无效值255。面积为投影像元面积之和，属于该投影下的平面面积。

## 4. 最小接口流程

真实服务的地址和密钥放在私有连接文件中，单独交付；公开源码和开发包不含真实凭据。应用开发包默认使用本机模拟地址 `http://127.0.0.1:18767`。

| 请求 | 用途 |
|---|---|
| `GET /health` | 无认证存活探针与执行模式；不证明 CUDA 或模型精度 |
| `GET /v1/capabilities` | 查询能力、输入约束、模型固定版本、层级、输出类型 |
| `POST /v1/inputs` | Bearer 认证；原始 TIFF 字节，`Content-Type: image/tiff`；返回 input_id、SHA256 |
| `POST /v1/jobs` | JSON：capability_id、input_id、metadata；必填 `Idempotency-Key`，返回202；相同提交复用返回200 |
| `GET /v1/jobs/{job_id}` | 查状态、错误或完整结果 |
| `POST /v1/jobs/{job_id}/cancel` | 请求体 `{}`；取消排队/运行任务，已终结任务保持原态 |
| `GET /v1/artifacts/{artifact_id}` | 下载本身份产物，逐文件 SHA256 核验 |
| `GET /openapi.json` | 同版本完整 OpenAPI 契约 |

状态：`queued → running → succeeded/failed`；取消可经过 `cancelling → cancelled`。服务重启会把中断运行标记 `SERVER_RESTARTED`，保留排队任务；已成功结果可继续查询。客户端等待超时不等于服务失败，应凭 job_id 续查或显式取消。

上传只做格式和体积初筛；**拿到 input_id 不代表通过空间/像元预检**。不合规影像的 job 会失败为 `INPUT_UNSUPPORTED`。相同身份与幂等键重复提交不会重跑；不同请求复用同键返回409。应用应先保存幂等键，再发起请求，在网络重试中复用它。

纯标准库 SDK 用法：

```python
import json
import uuid
from model_client import ModelClient

connection = json.load(open("gpu-connection.local.json", encoding="utf-8"))
client = ModelClient(connection["base_url"], connection["api_key"])
metadata = json.load(open("metadata.json", encoding="utf-8"))
uploaded = client.upload("input.tif")
retry_key = uuid.uuid4().hex  # 发请求前持久保存；重试复用
job = client.submit("da-v2-field-delineation", uploaded["input_id"], metadata, retry_key)
final = client.wait(job["job_id"])
if final["status"] == "succeeded":
    for artifact in final["result"]["artifacts"]:
        client.download(artifact, "output")
else:
    print(final["error"])
```

SDK 默认校验 HTTPS 证书，不跟随重定向，不使用系统代理；远程明文 HTTP 被拒绝。网页应由**本机应用后端**调用 SDK，不能把模型服务密钥和 DeepSeek 密钥写进公开前端脚本。服务没有开放浏览器跨域 CORS。

## 5. 返回结果的含义

- `execution_mode=gpu` 表示真实执行；`mock` 仅用于协议开发，固定返回带水印的合成结果，不分析上传影像。
- `quality_status=unverified_candidate` 与 `accuracy_evaluated=false` 必须保留给用户。接口完成不代表识别已独立验准。
- 统计 `pixels/area_m2/fraction` 基于最终分类栅格；fraction 以有效像元为分母，255 不参与。TerraMind 的0是未判定，不是255无数据。
- DA GeoJSON 的 `model_score` 是检测器分数，不是准确率；矢量面积与栅格计数面积可能不同。候选田块不自动等同于已核实农业用途。
- `preview.png` 左边是 RGB，右边是候选结果；正式地图图例应由应用结合 `classes` 生成。
- `model_run` 含模型/权重版本、实现哈希、分块参数、GPU、耗时与显存记录；不返回宿主文件路径或认证信息。
- capabilities 的 `availability=configured` 只表示运行时文件存在；仍可能发生 GPU 不可用、显存不足或推理失败。

## 6. 应用方下一阶段清单

1. 网页配置中增加 GPU base_url/API key，与 DeepSeek 配置分开保存；提供连通性与能力查询按钮，密钥输入隐藏。
2. 用 `model_client.py` 封装 `list_model_capabilities`、`submit_model_job`、`get_model_job`、`cancel_model_job`、`download_model_artifact` 工具，供 DeepSeek 选择。
3. 维护数据事实：来源、波段、单位、时间、坐标等。格式适配属于独立工具，不让自然语言声明替代实际预检。
4. 按 job_id 更新进度；DeepSeek 调用状态和 GPU 模型状态分别展示，支持取消与断网后续查。
5. 结果默认显示图、通俗说明、候选性质与下载；参数和来源放在可展开的专业详情。
6. 兜底前检查任务语义是否仍一致；保留原始需求、工具选择、错误、重试/回退理由和产物来源。不把空输出、旧任务产物或模型自评当成功。
7. 模拟验收通过后，使用单独分配的 application-dev 凭据联调。真实大批量任务先和维护者确认资源。

## 7. 错误处理与边界

| 错误 | 应用行为 |
|---|---|
| 401 `UNAUTHORIZED` | 检查/更换本机配置，不无限重试 |
| 400 `CAPABILITY_UNSUPPORTED` / `INPUT_UNSUPPORTED` | 解释缺失能力/输入条件；修正条件或明确变更方案 |
| 409 `IDEMPOTENCY_CONFLICT` | 相同键只能复用完全相同的请求 |
| 429 `QUEUE_FULL` | 等待后按同幂等键重试；保留任务状态 |
| 429 `STORAGE_LIMIT` | 联系维护者归档，不通过无限上传绕过 |
| `GPU_UNAVAILABLE` / `GPU_OUT_OF_MEMORY` / `JOB_TIMEOUT` | 展示真实错误，有限重试或交给维护者；不声称已分析 |
| `ARTIFACT_CHANGED` / `RESULT_CONTRACT_FAILED` | 停止交付该产物，按任务 ID 排查 |

该服务适用于受信任的小团队。单 worker 串行约束只管理此 API 的任务，不控制旧服务或其他人的 GPU 进程。每身份最多4个活动任务、全局最多16个排队；128个已上传输入/身份，4 GiB 软件存储阈值。阈值不是操作系统硬配额，运行中产物可能暂时超出。输入和产物不会自动删除，维护者需归档后切换新数据目录。

进程隔离、网络禁用钩子和 API 认证不等于操作系统级多租户沙箱。本轮不扩展给不受信任公网用户。模型许可证随能力目录提供，继续分发模型或对外开放前应核对上游授权要求。

运维见[模型 API 运维说明](model-api-operations.md)，修改记录见[FIX-026 图文幻灯片](fixes/2026-10-30-026-model-api-handoff.html)。
