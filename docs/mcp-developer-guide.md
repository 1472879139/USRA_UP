# USRA MCP 开发者接入指南

适用对象：需要在网页、应用后端或智能体中调用远程遥感模型的开发者。

服务首版于 2026-11-06 上线，当前 API/MCP 版本及 2026-11-07 验收记录见下文。模型运行在服务方的 RTX 4090 D 上，云服务器提供 HTTPS 入口。调用方只需接入客户端，无需安装 CUDA、模型权重，也无需连接服务器 SSH。

[统一工具注册与契约查询](tool-registry.md)及[异构工具 v2](tool-api-v2.md)已于 2026-11-07 上线。原有调用流程保留；新接入可使用 /v2/tools 获取输入与输出 Schema，通过 /v2/files 上传命名文件输入，再提交 /v2/jobs，或调用 ursa_v2_ 开头的 MCP 工具。现有私有接入配置继续有效，版本及验收见[上线记录](tool-v2-deployment.md)。

## 1. 接入前需要拿到的材料

请向服务维护者获取以下三项：

1. 本文。
2. `MCP接入包_不含密钥.zip`，解压后包含客户端示例、依赖锁文件和接口说明。
3. **单独交付的** `MCP接入配置_私密.json`，其中有真实认证凭据。

私有配置结构如下，示例中的令牌均为占位内容：

```json
{
  "mcp_url": "https://8.137.114.198/ursa-mcp/mcp",
  "transport": "streamable-http",
  "mcp_token": "由服务方单独提供的MCP令牌",
  "api_base_url": "https://8.137.114.198/ursa-model-api",
  "api_key": "由服务方单独提供的文件API密钥",
  "owner": "your-application",
  "auth_mode": "controlled_bearer"
}
```

运行时读取配置中的地址，避免将 IP 写死在业务代码里。地址或凭据发生调整时，以维护者重新交付的配置为准。

|配置项|用途|
|---|---|
|`mcp_url`|MCP 客户端连接地址，完整使用，不再拼接工具名|
|`mcp_token`|调用 MCP 工具时的 Bearer 令牌|
|`api_base_url`|上传影像、下载结果及查询 OpenAPI 的基础地址|
|`api_key`|模型 HTTP API 的 Bearer 密钥，与 MCP 令牌不同|
|`owner`|服务方分配的调用身份标识；修改此字段不能切换服务身份|

**两类凭据的请求头：**

```http
Authorization: Bearer <mcp_token>
```

以上用于 MCP；模型 HTTP API 使用：

```http
Authorization: Bearer <api_key>
```

当前采用受控 Bearer 认证，未提供 OAuth 登录。MCP 令牌、模型 API 密钥与调用方自己的 DeepSeek 等大模型 API 密钥是不同的凭据。

## 2. 在你的系统中如何放置

```text
浏览器前端
    ↓ 调用你自己的应用后端
应用后端：保管凭据、连接MCP、上传和下载文件、保存任务记录
    ↓ HTTPS
云端入口 → 4090上的MCP服务 → 模型API与GPU任务队列
```

前端负责工具选择、文件选择、参数表单、任务状态与结果展示。应用后端负责认证、MCP 调用、文件传输、重试和业务用户权限。

凭据放在后端私有配置或密钥管理设施中，不编译进网页 JavaScript，不放入 URL、浏览器持久存储或 Git。服务当前没有为任意网页配置直接跨域访问；网页通过自己的后端使用服务。

一份服务凭据对应一个服务身份。如果你的应用有多个登录用户，应用后端仍需检查“这个用户能否查看这个 job_id 或文件”。需要在模型服务侧隔离不同调用方时，请向维护者申请独立凭据，不能依靠传入 `owner` 实现隔离。

## 3. 首次接入：先做只读自检

以下文件路径均相对于解压后的接入包根目录。调用现有服务时，无需在本机启动 `ursa_mcp.server`。

### 3.1 准备客户端环境

推荐使用 **Python 3.11 或 3.12**，最低要求为 3.11。先检查 `python --version`；若默认版本过低，请换用正确解释器的完整路径。

Windows PowerShell：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-mcp.lock
.\.venv\Scripts\python.exe examples/mcp/call_mcp.py --connection-file "C:\私有目录\MCP接入配置_私密.json"
```

Linux/macOS：

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-mcp.lock
.venv/bin/python examples/mcp/call_mcp.py --connection-file "/私有目录/MCP接入配置_私密.json"
```

若已有同名环境，请选一个新的独立环境目录，保留原环境。已有 GIS/Conda 项目无需为使用此客户端重新安装 GDAL、Rasterio 或 Torch。

上面的示例默认只连接和查询，**不会提交 GPU 任务**。完整权限配置的预期输出类似：

```json
{
  "connected": true,
  "execution_mode": "gpu",
  "tools": [
    "ursa_get_service_info",
    "ursa_get_tool_contract",
    "ursa_get_job",
    "ursa_cancel_job",
    "ursa_get_artifacts",
    "ursa_delineate_fields",
    "ursa_classify_landcover"
  ]
}
```

工具数量可能随身份权限和模型可用性变化，前端应按实际目录展示。

### 3.2 可单独查看的接口

|用途|地址|认证|
|---|---|---|
|MCP 健康检查|`https://8.137.114.198/ursa-mcp/health`|不需要|
|模型 API 健康检查|`https://8.137.114.198/ursa-model-api/health`|不需要|
|模型能力目录|`api_base_url + /v1/capabilities`|`api_key`|
|模型 API 契约|`api_base_url + /openapi.json`|`api_key`|

健康检查仅证明入口状态，不能证明任意输入都能推理成功。浏览器直接打开 MCP 调用地址可能返回 401；实际工具调用由带认证的 MCP 客户端完成。

## 4. 当前有哪些工具

|MCP 工具名|用途|参数|
|---|---|---|
|`ursa_get_service_info`|获取执行模式、能力目录、上传约定|`{}`|
|`ursa_get_tool_contract`|获取指定能力的输入和结果契约|`tool_id`|
|`ursa_classify_landcover`|提交 TerraMind 地物候选分类任务|`input_id`、`metadata`、`idempotency_key`|
|`ursa_delineate_fields`|提交 DA V2 候选田块任务|同上|
|`ursa_get_job`|查询本身份任务|`job_id`|
|`ursa_cancel_job`|取消本身份任务|`job_id`|
|`ursa_get_artifacts`|获取成功任务的产物清单及下载地址|`job_id`|

**MCP 工具名与能力 ID 的对应关系：**

|模型任务|调用的 MCP 工具名|查询契约或使用 REST 时的能力 ID|
|---|---|---|
|地物候选分类|`ursa_classify_landcover`|`terramind-small-landcover`|
|候选田块提取|`ursa_delineate_fields`|`da-v2-field-delineation`|

例如，查询 TerraMind 输入输出要求时调用：

```json
{
  "tool_id": "terramind-small-landcover"
}
```

不要将能力 ID 当成 MCP 工具名。`tools/list`、`tools/call` 是 MCP 协议方法，由 SDK 发送到同一个 `mcp_url`，不将这些方法名拼接成 REST 路径。

## 5. 前端如何获得输入格式和预期输出

应用后端连接 MCP 后，先读取 `tools/list`，再对选中的模型调用 `ursa_get_tool_contract`。

|数据来源|应用如何使用|
|---|---|
|工具的 `name/title/description`|工具选择项、中文标题和说明|
|工具的 `inputSchema`|调用参数字段、必填项、类型和校验规则|
|工具的 `outputSchema`|该次 MCP 调用返回对象的结构|
|契约中的 `capability.input`|文件格式、波段、网格、大小等条件|
|契约中的 `metadata_schema`|影像元数据的字段及允许值|
|契约中的 `result_schema`|模型完成后 `job.result` 的结构|
|契约中的 `capability.outputs/classes/limitations`|预期产物、类别含义和使用限制|

模型提交工具返回的是 **Job 回执**，完成结果放在成功任务的 `result` 中；提交回执不代表推理已经完成。

MCP 提供契约，前端团队需要实现对应表单和展示组件。当前两项模型共享相同的 TIFF 输入约束，前端可以先提供文件选择与元数据确认；后续异构工具应按新增契约扩展。

## 6. 当前接受的影像

|条件|当前要求|
|---|---|
|格式|TIFF/GeoTIFF，上传媒体类型 `image/tiff`|
|来源声明|Sentinel-2、L2A|
|波段|共 12 个，包含 B1、B2、B3、B4、B5、B6、B7、B8、B8A、B9、B11、B12|
|空间条件|10 米像元，north-up，已知的米制投影坐标系|
|影像尺寸|宽、高均为 32–1024 像素|
|文件大小|非空且不超过 16 MiB，即 16,777,216 字节|
|数值编码|显式声明 `dn_10000` 或 `reflectance_0_1`，应与数据一致|
|波段 scale/offset|TIFF 波段 scale=1、offset=0；按服务契约预处理|

将下面内容以 UTF-8 保存为 `metadata.json`，并按实际数据核对：

```json
{
  "sensor": "Sentinel-2",
  "product_level": "L2A",
  "encoding": "dn_10000",
  "bands": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12"]
}
```

`bands` 应逐一对应 TIFF 实际波段顺序及描述。示例不能直接套到顺序不同的数据上。普通 RGB 照片、单波段影像、无坐标影像或未经适配的原始产品，不能仅修改元数据就作为合格输入。

上传成功只表示文件已接收，像元级和结构预检还可能在任务中失败。传感器与产品来源由调用者真实声明，服务不能独立证明来源真实性。

## 7. 完整调用流程

### 第一步：上传影像

调用模型 HTTP API：

```http
POST {api_base_url}/v1/inputs
Authorization: Bearer <api_key>
Content-Type: image/tiff

<TIFF文件原始字节>
```

正文为文件字节，不是 multipart 表单或 base64 JSON。浏览器可以把文件上传到应用后端的 multipart 接口，再由应用后端转换为上述请求。

返回示意，ID 和哈希须使用实际响应值：

```json
{
  "input_id": "00000000000000000000000000000001",
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "size_bytes": 1206010
}
```

保存 `input_id`、`sha256`、文件大小，并核对本机文件哈希。上传和后续 MCP 调用必须使用服务方配套交付的同身份凭据。

接入包的 `examples/mcp/model_client.py` 已提供 `ModelClient.upload(path)`。

### 第二步：通过 MCP 提交模型任务

调用 `ursa_classify_landcover`，参数如下；DA V2 使用相同参数结构，工具名改为 `ursa_delineate_fields`：

```json
{
  "input_id": "00000000000000000000000000000001",
  "metadata": {
    "sensor": "Sentinel-2",
    "product_level": "L2A",
    "encoding": "dn_10000",
    "bands": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12"]
  },
  "idempotency_key": "example-request-0000000000000001"
}
```

上面的 `idempotency_key` 仅为演示值，不用于真实任务。实际需使用 **16–128 位 ASCII 字母、数字、下划线或连字符**。推荐由应用后端在调用前生成 `uuid.uuid4().hex`，并连同输入 ID 和参数持久保存。

提交成功后保存服务返回的 `job_id`。输入 ID、任务 ID、产物 ID 均使用服务实际返回的 32 位十六进制值，不使用本文示意 ID。

### 第三步：查询到终态

调用 `ursa_get_job`：

```json
{
  "job_id": "00000000000000000000000000000002"
}
```

|`job.status`|前端显示建议|是否完成|
|---|---|---|
|`queued`|排队中|否|
|`running`|模型执行中|否|
|`cancelling`|正在取消|否|
|`succeeded`|成功，可读取结果|是|
|`failed`|失败，展示 `job.error`|是|
|`cancelled`|已取消|是|

建议每 2 秒查询一次，遇到限流或网络错误后延长间隔。当前接口没有承诺百分比进度，按实际状态展示。

应用应保存“业务用户、项目、服务身份、input_id、job_id、提交键、参数及最后状态”的对应关系。页面刷新后按原 job_id 恢复查询，不再次提交模型任务。

### 第四步：读取并下载结果

只有 `job.status == "succeeded"` 时，读取 `job.result` 或调用 `ursa_get_artifacts`。主要字段如下：

|字段|含义|
|---|---|
|`execution_mode`|`gpu` 为真实模型执行，`mock` 为模拟模式|
|`input_sha256`|本任务使用的输入身份|
|`width/height/crs/transform`|输出网格与坐标信息|
|`statistics`|类别统计，含 `code/name/pixels/area_m2/fraction`|
|`model_run`|实际模型、权重版本、设备和推理来源信息|
|`quality_status`|当前真实模型结果为 `unverified_candidate`|
|`accuracy_evaluated`|当前为 `false`，没有独立精度评价|
|`artifacts`|本任务产物的权威清单|

TerraMind 预期返回分类 GeoTIFF、统计 CSV、预览 PNG、来源 JSON；DA V2 另有候选边界 GeoJSON。按响应中的 `name/media_type/role` 展示，不按固定文件名推测下载地址。

产物清单包含 `artifact_id/name/size_bytes/sha256/media_type/role/download_path`；`ursa_get_artifacts` 还提供 `download_url`。下载请求使用模型 API 的 `api_key`：

```http
GET {api_base_url}{download_path}
Authorization: Bearer <api_key>
```

下载后核对字节数和 SHA256。已有 `ModelClient.download(artifact, directory)` 会检查路径、大小、哈希，并保留内容不同的已有文件。

浏览器预览或下载时，由应用后端携带服务密钥读取文件，再按本应用用户权限返回。结果是候选，不在界面上标注未经评价的准确率。

### 第五步：需要取消时显式调用

调用 `ursa_cancel_job`，参数为 `{"job_id":"实际任务ID"}`，随后继续查询至终态。关闭网页、断开 MCP 或客户端等待超时都不会自动取消 GPU 任务。取消已完成任务不会删除其产物。

## 8. 使用已提供的完整示例

在接入包根目录使用前面创建的虚拟环境解释器。以下展示 Windows PowerShell 命令；Linux/macOS 使用 `.venv/bin/python`。

提交 TerraMind 新任务，会实际占用服务方 GPU：

```powershell
.\.venv\Scripts\python.exe examples/mcp/call_mcp.py --connection-file "C:\私有目录\MCP接入配置_私密.json" --image "C:\输入\image.tif" --metadata "metadata.json" --tool ursa_classify_landcover --results-dir "本次结果/terramind"
```

提交 DA V2 新任务：

```powershell
.\.venv\Scripts\python.exe examples/mcp/call_mcp.py --connection-file "C:\私有目录\MCP接入配置_私密.json" --image "C:\输入\image.tif" --metadata "metadata.json" --tool ursa_delineate_fields --results-dir "本次结果/fields"
```

恢复查询已知任务并下载校验：

```powershell
.\.venv\Scripts\python.exe examples/mcp/call_mcp.py --connection-file "C:\私有目录\MCP接入配置_私密.json" --job-id "替换为实际job_id" --results-dir "本次结果/恢复查询"
```

示例脚本会保存 `submission.json`、`job.json` 和 `task_<job_id>` 结果目录。新任务使用新目录；同次提交失败或超时重试复用原目录，脚本会复用提交键。若影像或元数据改变，应创建新的业务任务和目录。

结果目录依次使用 `--results-dir`、`URSA_RESULTS_DIR`，未指定时创建随机目录。本示例最多等待 600 秒；超过等待时间仍可按 job_id 查询，不能据此判断服务端已停止。

## 9. 在自有后端中接入 MCP

以下是 Python SDK 2.3.0 的最小只读示例，不启动推理。请在接入包根目录、已安装锁文件依赖的环境中运行：

```python
import asyncio
import json
from pathlib import Path
import ssl

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    config = json.loads(
        Path("MCP接入配置_私密.json").read_text(encoding="utf-8-sig")
    )
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + config["mcp_token"]},
        timeout=30,
        trust_env=False,
        verify=ssl.create_default_context(),
    ) as http:
        transport = streamable_http_client(config["mcp_url"], http_client=http)
        async with Client(transport, read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            print([tool.name for tool in tools.tools])
            response = await client.call_tool(
                "ursa_get_tool_contract",
                {"tool_id": "terramind-small-landcover"},
            )
            if response.is_error:
                raise RuntimeError(response.content[0].text)
            contract = response.structured_content
            print(contract["tool_id"], contract["capability"]["input"])

asyncio.run(main())
```

任意一次 `client.call_tool(...)` 后先检查 `response.is_error`，再读取 `response.structured_content`。`is_error=false` 表示该次工具调用成功；查询到的 Job 仍可能是 `running` 或 `failed`，还须检查 `job.status`。

生产环境由后端管理连接生命周期，避免每次浏览器轮询都重复创建 MCP 连接。原始错误和响应可能包含业务数据，日志仅记录阶段、错误码、业务任务号和 job_id，过滤凭据。

Codex 的连接模板位于 `examples/mcp/codex.example.toml`：填写实际 MCP 地址，通过 `URSA_MCP_TOKEN` 环境变量提供令牌。合并配置时保留已有其他服务；相应客户端进程需要能读取该环境变量。其他客户端也需支持 Streamable HTTP 和 Bearer 请求头。

## 10. 应用后端路由建议

以下是**前端团队需要自行实现的应用路由建议**，不是公网模型服务已有地址：

|你们的应用路由|后端执行的操作|
|---|---|
|`GET /app-api/tools`|MCP 工具发现与契约查询，返回前端需要的表单字段|
|`POST /app-api/inputs`|接收浏览器文件，调用模型 API 上传并保存归属|
|`POST /app-api/jobs`|核验用户权限，保存提交键和参数，调用所选 MCP 模型工具|
|`GET /app-api/jobs/{job_id}`|核验归属，查询并返回任务状态|
|`POST /app-api/jobs/{job_id}/cancel`|核验归属，调用取消工具|
|`GET /app-api/jobs/{job_id}/files/{artifact_id}`|核验归属，从清单定位文件，认证下载、校验后返回|

若应用采用固定按钮调用而不需要智能体工具发现，也可按模型 OpenAPI 使用现有 REST 任务接口。两种入口共用服务端任务与身份；文件仍由模型 HTTP API 上传和下载。

## 11. HTTP API 速查

下表路径都拼在 `api_base_url` 之后，不重复添加 `/ursa-model-api`：

|方法与路径|用途|认证/额外要求|
|---|---|---|
|`GET /health`|模型 API 状态|无需认证|
|`GET /v1/capabilities`|模型目录和文件约束|`api_key`|
|`GET /openapi.json`|完整 HTTP 请求/响应契约|`api_key`|
|`POST /v1/inputs`|上传 TIFF 原始字节|`api_key`、`Content-Type: image/tiff`|
|`POST /v1/jobs`|直接通过 REST 提交任务|`api_key`、`Idempotency-Key` 请求头|
|`GET /v1/jobs/{job_id}`|查询本身份任务|`api_key`|
|`POST /v1/jobs/{job_id}/cancel`|取消本身份任务|`api_key`、JSON `{}`|
|`GET /v1/artifacts/{artifact_id}`|下载本身份产物|`api_key`|

REST 提交正文与 MCP 参数的区别：REST 使用 `capability_id/input_id/metadata`，幂等键放在 HTTP 请求头 `Idempotency-Key`；MCP 由工具名确定模型，幂等键使用参数 `idempotency_key`。

## 12. 重试和错误处理

|情况|处理方式|
|---|---|
|HTTP 401|检查连接文件、令牌类型和是否被撤销；不反复提交模型任务|
|HTTP 403、Host/Origin 拒绝|核对入口和调用方式；浏览器改走应用后端，配置问题联系维护者|
|HTTP 429|按 `Retry-After` 等待，并降低集中轮询频率|
|`INVALID_ARGUMENTS`|按实际 `inputSchema` 修正参数，不添加 owner、服务器路径或任意 URL|
|`TOOL_UNAVAILABLE`|刷新工具目录，确认身份权限及能力是否停用|
|`NOT_FOUND`|核对 ID 和调用身份；输入、任务和产物不跨身份读取|
|`IDEMPOTENCY_CONFLICT`|同一提交键对应了不同正文；查清业务记录后再决定恢复原请求或创建新任务|
|`JOB_NOT_SUCCEEDED`|先查任务状态及错误，成功后再获取产物|
|`UPSTREAM_UNAVAILABLE`|模型 API 暂时不可达；查询可重试，提交保留原键和正文|
|`RESULT_INVALID`、`ARTIFACT_INVALID`、哈希失败|不展示为可信成功结果，保留 job_id 和错误码联系维护者|
|`job.status=failed`|展示 `job.error`；修正数据或参数后作为新业务任务提交|

MCP 工具错误通常在 `is_error/content` 中返回，不能只看 HTTP 200。网络连接失败则可能由 SDK 直接抛出异常。

提交超时后，服务端可能已经接收任务：已知 job_id 时继续查询；未知时用**原 input_id、原参数和原提交键**重试。不要换新键盲目重跑，也不要在提交重试中重新上传并换掉 input_id。

当前部署按服务身份限制每分钟 120 次 MCP 请求，握手和工具发现也计入请求。多用户、多标签页的请求会合并计数，建议由应用后端集中轮询并缓存状态。MCP 请求正文限制 64 KiB；TIFF 文件通过单独上传接口传输。

## 13. 开发者交接验收清单

- 私有配置可被应用后端读取，网页和日志中看不到密钥。
- 只读自检成功，能取得当前身份的工具列表与两项模型契约。
- 使用本次符合要求的小型 TIFF，上传后核对输入大小和 SHA256。
- 通过 MCP 提交新任务，保存 input_id、job_id、参数及提交键。
- 查询终态；成功时下载并核对全部产物大小和 SHA256。
- 重复点击提交使用同一业务请求和幂等键，不创建重复 GPU 任务。
- 页面刷新后恢复原任务；取消操作显式调用服务并查询终态。
- 失败、限流和断线时不伪装成功，不自动更换模型。
- 展示候选结果和未做精度评价的标记。

服务方于 2026-11-06 已通过公网完成两模型的新任务及 9 个文件哈希核验，并用官方 Python SDK、Inspector 的两种协议模式做过验收。接入方仍应在自己的真实电脑、网络和应用后端完成上述检查。

## 14. 当前范围与问题反馈

当前支持两项既有模型、逐工具输入输出契约、异步任务、身份隔离和产物获取。OAuth 登录、任意异构模型/算法的动态安装、注册/删除及完整生命周期管理属于后续范围；不要根据建设方案中的未来示例提前调用不存在的工具。

本接入包不包含真实影像或模型权重。首次样例请向维护者获取已确认符合契约的公开数据；本服务不会独立证明影像来源或分类精度。

联系维护者时提供：发生时间和时区、调用阶段、MCP 工具名、错误码、job_id（如有）、文件大小与 SHA256，以及已脱敏的参数说明。不要发送令牌、私钥、完整认证请求头或未获准分享的业务影像。

维护者的部署与回滚说明位于接入包 `docs/mcp-service.md`；应用开发者日常接入不需要操作这些部署脚本。
