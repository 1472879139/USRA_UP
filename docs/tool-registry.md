# 统一工具注册、契约与启停管理

本注册层及[异构工具 v2](tool-api-v2.md)已于 2026-11-07 部署并完成公网只读验收，当前模型 API 为 c76c3d3938213533，MCP 为 012ed3eac3b58187。本文描述 v1 工具目录；v2 使用独立输入契约与状态文件，详见[上线记录](tool-v2-deployment.md)。

## 1. 这一轮解决什么

模型 API、MCP 和前端契约查询共用已安装工具目录。每个工具有稳定的能力 ID、MCP 名称、输入条件、独立的 metadata JSON Schema、预期结果 Schema、产物角色和启停状态。

内置工具保持原 ID 和调用方式：

|能力 ID|MCP 工具名|主要输出|
|---|---|---|
|da-v2-field-delineation|ursa_delineate_fields|候选田块栅格、边界、统计、预览、来源记录|
|terramind-small-landcover|ursa_classify_landcover|候选地物分类、统计、预览、来源记录|

本轮保留 v1 的单个 TIFF 上传和异步任务结构。不同工具可以声明和校验不同 metadata；现有两个模型仍要求原有 12 波段 Sentinel-2 L2A。任意文件类型、多输入槽、纯 JSON 算法、模型自动安装/卸载仍需后续接口和执行器扩展，不能仅修改目录就宣称支持。

## 2. 给前端提供什么

以下路径相对于模型 API 的基础地址，使用原有模型 API Bearer 密钥：

|接口|用途|
|---|---|
|GET /v1/tools?limit=20&offset=0|分页查询工具和完整契约，limit 为 1～50|
|GET /v1/tools/{tool_id}|查询单个未移出工具的契约，包括停用工具|
|GET /v1/capabilities|保留旧目录，增加启停状态、MCP 名称和逐工具 Schema|
|GET /openapi.json|包含新增查询路由，任务请求按已注册工具列出 metadata 分支|

列表响应包含 tools、total、has_more、next_offset 和 registry_revision。同一份目录未变化时 revision 稳定；状态或可用性改变时 revision 更新。翻页期间 revision 改变，应用应重新获取目录。

工具契约的字段：

|字段|用途|
|---|---|
|tool_id / mcp_name|稳定的能力 ID 与实际 MCP 名称|
|capability.input|文件、波段、空间网格和编码约束|
|metadata_schema|该工具自己的参数表单及校验要求|
|result_schema|完成结果的结构、能力 ID 与允许的产物角色|
|job_schema|公共异步任务回执和状态|
|capability.enabled / state|注册层启停状态，不能单独当作 GPU 健康检查|
|capability.availability|执行器报告的可用性，停用时为 unavailable|

Python 应用后端可直接使用 model_client.py：

```python
client.tools(limit=20, offset=0)
client.tool_contract("terramind-small-landcover")
```

网页继续通过自己的应用后端调用，后端保管凭据。应用应按实际 Schema 和 input 条件生成表单；目录里存在工具不代表输入一定合规或模型精度已验证。

## 3. 初始化和维护状态

在模型服务项目根目录执行。以下命令只操作指定的本地文件，不连接公网，也不运行模型：

```powershell
python -m model_service.tools --registry-file .model-service/tools.local.json init
python -m model_service.tools --registry-file .model-service/tools.local.json list
python -m model_service.tools --registry-file .model-service/tools.local.json show terramind-small-landcover
python -m model_service.tools --registry-file .model-service/tools.local.json disable terramind-small-landcover
python -m model_service.tools --registry-file .model-service/tools.local.json enable terramind-small-landcover
python -m model_service.tools --registry-file .model-service/tools.local.json remove terramind-small-landcover
python -m model_service.tools --registry-file .model-service/tools.local.json restore terramind-small-landcover
```

restore 后仍处于停用状态，确认适配器和环境可用后再 enable。list 会显示已移出工具，便于恢复；对外目录隐藏它们。show 不探测运行环境，runtime_probed=false 时不能将其 availability 当成线上状态。

init 不覆盖已有文件。写入采用维护锁和原子替换；已有锁时拒绝写入，避免两个维护进程丢失彼此修改。若维护程序异常退出留下锁，维护者先确认没有其他写入者，再人工处理对应 .lock 文件。

服务启动时，在原有 serve 命令后追加：

```text
--tool-registry-file .model-service/tools.local.json
```

服务和维护命令必须指向同一个文件，运行目录不同应使用绝对路径。指定的文件缺失或无效时拒绝新任务与目录读取，不会恢复成“全部启用”。未传该参数的旧启动命令保持兼容，使用内置工具的默认启用状态。

状态文件只包含 version 和 tools，不能填 Python 路径、Shell 命令、权重地址或任意 URL。每次目录查询和新任务准入重新读取，正常启停无需重启。

## 4. 启停怎样影响任务

|动作|对外目录与新任务|已接收任务|
|---|---|---|
|disable|保留契约，标记停用，拒绝新提交|排队/运行任务继续，查询、取消和下载保留|
|enable|恢复准入，仍须执行器可用|不重新执行旧任务|
|remove|从目录隐藏，拒绝新提交|不删除历史任务、产物或模型文件|
|restore|回到目录但保持停用|不重新执行旧任务|

同身份、同参数、同幂等键的已接收请求，在停用或移出后仍返回原回执，不重复执行。冲突参数仍返回 IDEMPOTENCY_CONFLICT。MCP 在停用后隐藏提交工具，但保留历史任务工具；已有回执可直接用 job_id 查询，通过 HTTP API 重试提交仍须使用原幂等键。

## 5. 怎样增加一个模型

1. 先在执行后端实现并验证适配器，包括输入预检、可用性判断、超时、取消、版本记录及受控产物输出。
2. 创建 ToolDefinition，声明能力元数据、唯一 mcp_name、input_contract、metadata_schema 和配套 metadata_validator。
3. 在服务启动前调用 ToolRegistry.register，再将注册表传给 Service。GPUBackend 也必须报告和实际执行该能力；仅登记未安装的工具不能通过任务准入，更不会被静默路由到另一个模型。
4. 使用临时目录、合成输入和模拟适配器验证 Schema、队列、身份、幂等和产物，再做经授权的真实模型验收。
5. 打包模型 API 和 MCP 的新发布版本，经核验后部署。

注册来自维护者代码，不提供公网执行代码或安装模型的管理接口。添加定义不需要修改前端固定字段，也不自动赋予浏览器管理员权限。破坏 v1 输入或结果结构的工具应扩展新接口版本。

## 6. 部署与验证

模型服务打包脚本会包含 registry.py 和 tools.py。GPU 安装脚本首次创建 tools.local.json，并让模型 API 使用该文件；升级保留原有状态，不会重新启用已停用工具。本轮没有执行安装器或修改线上单元。

项目要求的离线检查：

```powershell
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
git diff --check
```

MCP 专用环境另运行：

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p "test_mcp*.py" -q
```

新增回归覆盖不同 metadata 契约、分页和认证、启停即时生效、软移除后的任务继续、下载哈希、幂等回执、无效状态文件拒绝准入和维护锁冲突。
