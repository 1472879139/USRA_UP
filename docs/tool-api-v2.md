# 异构工具 API v2 与适配器接入

v2 已于 2026-11-07 部署到 4090，并完成公网 HTTPS 与 MCP 只读验收，API 版本为 c76c3d3938213533，MCP 为 012ed3eac3b58187。原 v1 调用方式、身份、工具状态与任务数据保留。生产只注册两个已有模型，两个模拟示例仅供本机测试；本轮未发起真实模型推理。详见[部署与回滚记录](tool-v2-deployment.md)。

## 1. 工具如何描述输入和输出

一个工具由 ToolSpec 和 ToolAdapter 组成。ToolSpec 定义稳定 ID、工具版本、MCP 名称、命名输入槽、参数 Schema、JSON 输出 Schema 和文件产物类型；ToolAdapter.execute 负责实际执行。

|部分|含义|
|---|---|
|input_slots|按名称描述输入，kind=file 或 json|
|input_schema|完整 inputs 对象的 JSON Schema，用于校验和生成表单|
|parameters_schema|独立参数对象 Schema，不再共用 Sentinel-2 metadata 字段|
|output_schema|该工具 outputs 的 Schema，可为对象、数组或其他 JSON 类型|
|artifact_types|允许的产物角色与媒体类型，文件与 JSON 输出分开返回|
|tool_version / contract_revision|版本和契约摘要，提交时保存快照|
|job_schema|该工具异步回执及结果的完整 Schema|

文件槽只接受如下引用，不接受客户端服务器路径、任意 URL 或文件 base64：

```json
{"file_id": "服务返回的32位十六进制ID"}
```

所有输入槽最多 16 个。每个文件最多 16MiB，工具可进一步收紧；上传媒体类型须被已启用工具声明。请求 JSON 最多 64KiB，完成结果中的 outputs 最多 1MiB，文件产物合计最多 64MiB、最多 16 个。更大的数据应通过文件产物返回。

## 2. 两个模型与两个模拟示例

|工具|命名输入|parameters|JSON 输出及文件|
|---|---|---|---|
|da-v2-field-delineation|image：TIFF 文件|原 Sentinel-2 元数据|原候选结果信息；栅格、GeoJSON、统计、预览、来源记录|
|terramind-small-landcover|image：TIFF 文件|原 Sentinel-2 元数据|原候选结果信息；分类栅格、统计、预览、来源记录|
|demo-json-summary|values：JSON 数值数组|可选 scale|count/sum/mean 与演示标记；JSON、CSV|
|demo-csv-compare|before、after：两个 CSV 文件|column|两期合计及差值与演示标记；JSON、CSV|

两个模型通过 LegacyModelAdapter 复用原后端，并未改变波段、网格、权重或模型含义。模拟示例不加载 GPU、权重或付费模型，输出明确带 synthetic_demo=true。常规服务不自动注册示例；CLI 的 --v2-examples 只允许 mock 模式。

## 3. 本机立即体验

使用当前项目的独立 Python3.11/3.12 环境：

```powershell
.venv/Scripts/python.exe -m pip install --require-hashes -r requirements-tools-v2.lock
.venv/Scripts/python.exe examples/tool_v2/local_demo.py --check --port 0
```

该命令只启动本机临时模拟 API，自动完成 JSON 和双 CSV 两个任务，下载并校验四个文件，然后退出。结果优先写入 --results-dir、其次 URSA_RESULTS_DIR，否则创建 results/v2_demo_随机标识。已有非空目录会拒绝覆盖。

如需给前端持续联调，运行：

```powershell
.venv/Scripts/python.exe examples/tool_v2/local_demo.py --port 19867
```

终端会显示回环 API 地址和临时 client.local.json 路径，不打印密钥。后端从文件中读取 api_base_url 与 api_key；退出后临时输入和任务会被清理。每次启动都是独立演示，旧的演示 job_id 不能当作新任务；持久服务使用下面的 serve 启动方式。

当前 MCP .venv 已包含相应依赖。requirements-tools-v2.lock 只包含固定版本的 JSON Schema 库及依赖，不包含 Torch、Rasterio、GDAL。不要把该依赖层安装进已有模型或 GIS 环境；原 v1 启动不需要导入 v2 可选依赖。

## 4. HTTP 接口

以下路径相对于模型 API 基础地址，全部使用该身份的 API Bearer 密钥；MCP 令牌不能用于文件上传：

|方法与路径|用途|
|---|---|
|GET /v2/tools?limit=20&offset=0|分页目录、独立 Schema、启停和运行可用性|
|GET /v2/tools/{tool_id}|单工具完整契约|
|POST /v2/files|原始文件字节上传，Content-Type 使用槽声明的媒体类型|
|POST /v2/jobs|提交 tool_id、inputs、parameters，幂等键放 Idempotency-Key 请求头|
|GET /v2/jobs/{job_id}|查询本身份任务与 outputs/产物|
|POST /v2/jobs/{job_id}/cancel|正文 {}，请求取消|
|GET /v2/artifacts/{artifact_id}|认证下载，核对 size_bytes 和 sha256|
|GET /v2/openapi.json|v2 HTTP 外层契约|

JSON 工具提交示例：

```json
{
  "tool_id": "demo-json-summary",
  "inputs": {"values": [1, 2, 3]},
  "parameters": {"scale": 2}
}
```

双文件工具提交示例，file_id 必须使用两次实际上传得到的 ID：

```json
{
  "tool_id": "demo-csv-compare",
  "inputs": {
    "before": {"file_id": "00000000000000000000000000000001"},
    "after": {"file_id": "00000000000000000000000000000002"}
  },
  "parameters": {"column": "value"}
}
```

Python 后端使用根目录的 tool_client_v2.py：

```python
import uuid
from tool_client_v2 import ToolClientV2

client = ToolClientV2(connection["api_base_url"], connection["api_key"])
contract = client.tool_contract("demo-json-summary")
key = uuid.uuid4().hex
# 发请求前持久化 key、inputs 和 parameters；超时后复用同一组值。
job = client.submit("demo-json-summary", {"values": [1, 2, 3]}, {}, key)
completed = client.wait(job["job_id"], timeout=600, interval=2)
if completed["status"] == "succeeded":
    outputs = completed["result"]["outputs"]
    for artifact in completed["result"]["artifacts"]:
        client.download(artifact, results_dir)
```

目录发生变化时 registry_revision 更新。前端获取完整一轮分页期间若 revision 改变，应重新获取。应用后端仍需维护自己的业务用户与 job_id 对应关系，网页只调用自己的后端。

## 5. MCP 配置

MCP 服务的私有 server.local.json 增加：

```json
{"enable_v2": true}
```

仅在对应模型 API 已启用 v2 时打开。默认 false 保持原工具列表和 v1 行为；MCP 发布包独立，不依赖模型服务源码。安装器支持 --enable-v2 / --no-enable-v2；不传时保留原配置。

新增通用工具为 ursa_v2_get_tool_contract、ursa_v2_get_job、ursa_v2_cancel_job、ursa_v2_get_artifacts。各适配器自己的提交工具由目录生成，名称必须以 ursa_v2_ 开头，与原工具互不冲突。

提交 JSON 模拟工具的 MCP 参数：

```json
{
  "inputs": {"values": [1, 2, 3]},
  "parameters": {"scale": 2},
  "idempotency_key": "example-idempotency-key-00000001"
}
```

工具名为 ursa_v2_demo_json_summary。上面的键仅为说明，实际调用前生成并持久化唯一 ASCII 提交键。文件仍走 HTTP /v2/files；MCP 不传文件正文。参数按独立 Schema 校验，成功回执中 result.outputs 也按该工具输出 Schema 校验。本地 Schema 引用会按嵌入位置转换，不访问远程 Schema 地址。

## 6. 为已有服务启用 v2

本轮不执行服务器部署。持久模型 API 启动时在原参数后增加：

```text
--enable-v2 --v2-registry-file /绝对路径/v2-tools.local.json
```

先用独立 v2 环境初始化同一个文件：

```powershell
.venv/Scripts/python.exe -m model_service.tools --api-version v2 --registry-file .model-service/v2-tools.local.json init
.venv/Scripts/python.exe -m model_service.tools --api-version v2 --registry-file .model-service/v2-tools.local.json list
```

模拟服务增加 --v2-examples 时，维护命令也加 --examples，确保使用同一份已安装工具集合。v1、v2 必须使用不同的状态文件；两边可以独立启停，不会把某版的工具状态误应用到另一版。服务和维护命令须使用同一个绝对路径。

GPU 机器可让 API 控制进程使用独立 v2 Python 环境，GPUBackend 继续启动 runtime.local.json 中既有的模型解释器。不要直接在模型 Conda 环境中补装依赖。本轮原安装脚本仍按默认 v1 启动；启用 v2 时须另外核验服务解释器和上述启动参数，再切换版本。MCP 也须同步启用 enable_v2。

## 7. 注册自己的适配器

可直接参考[可运行的文本模板](../examples/tool_v2/adapter_template.py)和内置 v2_adapters.py：

1. 定义 ToolSpec，声明输入、必填槽、参数、输出、产物类型及版本。
2. 实现 ToolAdapter.execute(context, inputs, parameters)，文件输入会转换为 FileInput，JSON 输入保持原值。
3. 将文件写入 context.results_dir，即本次执行器提供的结果目录；返回 AdapterResult(outputs, artifacts)。不要引用其他任务的输出。
4. 长循环定期调用 context.check_active()。它检查取消、服务停止和超时；进程内适配器必须协作检查。不能协作中断的模型需使用有终止/超时控制的子进程，既有模型桥接沿用原 GPUBackend 的进程控制。
5. 通过 build_registry(..., extra_adapters=[YourAdapter()]) 或 registry.register() 在维护者启动代码中登记；不修改 HTTP 分发或 MCP 公共逻辑，不接受网页上传 Python 路径或 Shell 命令来注册。

契约在登记时复制并校验，提交时保存快照。新代码变更输入或输出语义时必须更新版本；服务重启后，旧排队任务若快照与已安装契约不同，明确失败为 TOOL_CONTRACT_CHANGED，不悄悄换用新语义。

## 8. 数据与兼容

v1 数据继续保存在原 data 目录。v2 的输入、任务和索引位于 data/v2；v1 数据库和表不做迁移。两种任务由同一个串行工作线程交替调度，避免同时占用 GPU；各版本保留自己的有界队列及身份配额，磁盘使用计入共同 data 目录。

每个文件最多上传 128 份/身份/协议版本；每身份最多 4 个活动 v2 任务、服务最多 16 个排队 v2 任务。v1 队列原限制保留。取消、重启失败、软移除后查询和幂等回执与 v1 保持同类语义。

停用/移出只拒绝新任务，已接收的任务继续。已成功结果按原 job_id 和身份获取。成功产物按白名单角色、媒体类型、受控路径、大小和 SHA256 登记；空文件清单和纯 JSON 结果有效，失败不会伪装成功。

本轮完成了线上升级及只读验收，没有发起真实模型推理或付费 LLM 调用。真实推理仍需单独验收；模拟演示结果不能作为遥感识别结果。
