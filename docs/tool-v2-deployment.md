# 工具 API v2 与 MCP 上线记录

2026-11-07 在既有 4090 服务上完成升级。公网 HTTPS、API v1/v2 目录、MCP 发现、契约及旧任务下载已只读验收。本轮提交新任务 0 次、真实模型推理 0 次、付费模型调用 0 次；旧任务结果只用于兼容性检查。

## 1. 接入地址与身份

- API 基础地址：`https://8.137.114.198/ursa-model-api`
- MCP 完整地址：`https://8.137.114.198/ursa-mcp/mcp`
- 传输：Streamable HTTP，使用原有 MCP Bearer 令牌。
- 文件、任务及产物 HTTP 接口使用原有 API Bearer 密钥。

地址与凭据以维护者私下交付的接入配置为准。新前端先认证查询 `GET /v2/tools?limit=20&offset=0`，按工具的 `input_schema` 和 `parameters_schema` 构建输入，按 `output_schema`、`artifact_types` 和 `job_schema` 处理结果。文件先上传到 `/v2/files`，再将返回的 `file_id` 放入对应命名输入槽。

完整调用示例、适配器模板和字段说明见[工具 API v2](tool-api-v2.md)。私有令牌由应用后端保管，不放进公开网页。

## 2. 实际版本与环境

|服务|发布 ID|发布包 SHA256|
|---|---|---|
|API|c76c3d3938213533|be714dcafb7b5239f210bffbb0c30ab0407b7975e4fdc647ef6caab7ea5cf627|
|MCP|012ed3eac3b58187|213b32fe52d7a1810f2136661b534f2762d2c23a93b2564eed3e5716a1f7e88a|

API 根目录：`/home/yy/ursa-gpu-runtime/model-api-20261030`。

MCP 根目录：`/home/yy/ursa-mcp-service`。

两个控制服务分别使用自己发布目录下的 `venv/bin/python`，由现有 Python 3.11 创建。API 只安装 `requirements-tools-v2.lock`，MCP 安装 `requirements-mcp.lock`，均核验固定版本和哈希；没有向原模型或 GIS 环境安装包。API 单元保留原 GPU 包装器所需的 `LD_PRELOAD`，模型执行仍由原 `runtime.local.json` 中的模型解释器负责。

API 服务启动保留原 v1 参数，并增加：

```text
--enable-v2 --v2-registry-file /home/yy/ursa-gpu-runtime/model-api-20261030/v2-tools.local.json
```

MCP 的 `private/server.local.json` 增加 `"enable_v2": true`。现有 API 密钥、MCP 身份、模型运行配置、v1 工具状态及两个隧道单元在升级前后摘要一致。Nginx 路由、公钥及 SSH 权限未变更。

注意：`deploy/model_api/install_remote.py` 是原 v1 安装入口，默认仍使用原 GPU 包装器启动 v1。不要直接用它覆盖当前 v2 服务单元。后续发版应先准备新的独立环境和发布目录，保留上述参数、身份与数据，在任务空闲时切换，失败恢复上版单元。

## 3. 工具与数据

生产 v1/v2 各有两个工具：

- `da-v2-field-delineation`
- `terramind-small-landcover`

MCP 共发现 13 个工具：原 v1 的 7 个，加 v2 的契约查询、任务查询、取消、产物查询和两个模型提交入口。生产没有注册 `demo-json-summary` 或 `demo-csv-compare`。

v1 数据继续位于 API 根目录的 `data`，没有迁移数据库；v2 使用 `data/v2`。两个版本共享同一个串行调度线程。状态文件分别为 `tools.local.json` 与 `v2-tools.local.json`，启停互相独立。

以 yy 登录 4090 后，用当前 API 环境维护 v2 工具：

```bash
cd /home/yy/ursa-gpu-runtime/model-api-20261030/releases/c76c3d3938213533
./venv/bin/python -m model_service.tools --api-version v2 --registry-file /home/yy/ursa-gpu-runtime/model-api-20261030/v2-tools.local.json list
```

将末尾的 `list` 替换为 `disable TOOL_ID`、`enable TOOL_ID`、`remove TOOL_ID`、`restore TOOL_ID` 可维护状态。软移除只影响新任务和目录，不卸载权重，也不删除历史结果；v1 需使用其独立状态文件另外管理。

## 4. 验收结果

- 四个用户服务均为 active：`ursa-model-api.service`、`ursa-model-api-relay.service`、`ursa-mcp.service`、`ursa-mcp-relay.service`。
- 公网证书使用标准 TLS 校验通过，没有跳过验证。
- API v1/v2 目录各返回两个工具，全部相关 JSON Schema 合法；匿名目录请求均返回 401。
- MCP 官方客户端发现 13 个工具，逐个 Schema 校验通过；v2 契约摘要与 HTTP 目录一致。
- 旧 v1 成功任务可通过 HTTP/MCP 查询，四个既有产物的大小和 SHA256 均匹配。
- 本地完整 Python 回归 206 项成功结束，其中 43 项因可选依赖缺失跳过；专用环境另跑 MCP 30 项、v2 17 项全部通过。前端轮询 22 项通过，`git diff --check` 通过。

新版本真实模型推理尚未执行，不能用旧任务或模拟结果代替新版本推理验收。

## 5. 检查与回滚

以 yy 登录 4090 后可只读检查：

```bash
systemctl --user is-active ursa-model-api.service ursa-model-api-relay.service ursa-mcp.service ursa-mcp-relay.service
curl --fail http://127.0.0.1:18767/health
curl --fail http://127.0.0.1:18768/health
```

本次备份目录：

```text
/home/yy/ursa-gpu-runtime/model-api-20261030/backups/v2-1794045739251443250
```

其中保存旧单元、配置、部署描述及一致性 SQLite 备份，目录包含私密信息，不上传 Git。旧 API 版本为 `3d147edb13f6567e`，旧 MCP 为 `0e3dc3baa5722b9c`，发布目录和环境均保留。

需要回滚时先确认两版没有 queued/running/cancelling 任务，再停止两个控制服务，恢复备份里的五个文件并重新启动：

```bash
systemctl --user stop ursa-mcp.service ursa-model-api.service
python3 - <<'PY'
from pathlib import Path
import shutil
backup = Path('/home/yy/ursa-gpu-runtime/model-api-20261030/backups/v2-1794045739251443250')
files = {
    'api_unit': Path.home() / '.config/systemd/user/ursa-model-api.service',
    'mcp_unit': Path.home() / '.config/systemd/user/ursa-mcp.service',
    'mcp_config': Path.home() / 'ursa-mcp-service/private/server.local.json',
    'api_deployment': Path.home() / 'ursa-gpu-runtime/model-api-20261030/deployment.json',
    'mcp_deployment': Path.home() / 'ursa-mcp-service/deployment.json',
}
for name, target in files.items():
    shutil.copy2(backup / name, target)
PY
systemctl --user daemon-reload
systemctl --user start ursa-model-api.service ursa-mcp.service
```

回滚不会删除 v2 数据或新发布目录，也不应直接用旧数据库覆盖升级后的任务数据。回滚后重新检查两处 health、v1 目录、MCP 与旧任务读取；若需要处理数据故障，应另行备份当前数据后恢复。
