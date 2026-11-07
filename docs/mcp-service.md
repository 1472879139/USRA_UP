# USRA MCP 模型服务

本实现为既有模型API v1增加官方MCP协议入口，提供Streamable HTTP和逐工具JSON Schema。模型推理、身份归属、幂等与GPU队列继续由既有API负责；不导入Torch/GDAL，不修改共享Message/Session及现有GUI，不调用付费LLM。

应用开发者请先阅读[开发者接入指南](mcp-developer-guide.md)，从只读自检开始；本文主要供服务维护者部署和排障。

[统一工具注册与启停管理](tool-registry.md)已于 2026-11-07 上线：MCP 优先采用每个工具声明的名称、metadata_schema 和 result_schema，并兼容原 API。[异构工具 v2](tool-api-v2.md)也已于同日上线，提供命名文件/JSON 输入、独立输出结构及 ursa_v2_ 工具。当前生产同时保留 v1 和 v2，API 为 c76c3d3938213533，MCP 为 012ed3eac3b58187；详见[部署与回滚记录](tool-v2-deployment.md)。

## 范围和身份边界

首版使用受控Bearer令牌，适用于可配置认证请求头的Codex、DeepSeek Harness及自研后端。它没有实现OAuth授权服务器、自动登录或标准OAuth发现，不能作为已完成OAuth互操作的服务交付。需要OAuth客户端时应另接授权提供者并校验令牌受众，不能伪造登录发现地址。

每个MCP令牌映射一个独立模型API身份。MCP令牌与API密钥不同，不透传外部令牌；任务与产物的最终归属由API密钥决定。禁止不同调用身份共用维护者内部密钥。服务每个请求重读身份文件，设`enabled=false`可撤销MCP访问；已经单独交付的模型API密钥还需要在API侧另行撤销。MCP的read/submit/cancel权限范围仅约束MCP入口，不能代替模型API本身的权限体系。

默认绑定`127.0.0.1:18768`。云端Nginx通过独立SSH反向隧道转发。公网可使用HTTPS＋IP证书，实际地址以私有交付文件为准；`examples/mcp`中的地址均为占位示例。SDK按请求处理协议版本，不手写握手；实际兼容性以验收记录为准。

## 工具

|工具|用途|输入|输出|
|---|---|---|---|
|`ursa_get_service_info`|模式、能力目录、上传入口|空对象|健康状态、能力及上传约定|
|`ursa_get_tool_contract`|逐模型输入与预期输出|`tool_id`|元数据Schema、文件要求、Job/Result Schema及产物角色|
|`ursa_classify_landcover`|TerraMind候选地物分类|`input_id/metadata/idempotency_key`|异步Job回执|
|`ursa_delineate_fields`|DA V2候选田块提取|同上|异步Job回执|
|`ursa_get_job`|本身份任务查询|`job_id`|Job与成功结果或失败原因|
|`ursa_cancel_job`|取消本身份任务|`job_id`|取消后的Job，仍需查询终态|
|`ursa_get_artifacts`|成功任务结果清单|`job_id`|认证下载URL、SHA256、大小及质量标记|

`tools/list`和调用时均重新查询API目录，`availability=unavailable`的模型不再接受MCP提交；已有Job仍可查询。服务端输入和输出都校验JSON Schema。Job、Result和Metadata结构来自API的OpenAPI定义，避免第二套字段漂移。非预期输出返回`RESULT_INVALID`，不当成成功交付。

当前API仅注册两项模型，输入仍为12波段Sentinel-2 L2A、10米north-up米制投影网格、边长32–1024、TIFF≤16MiB。v2 已提供命名输入槽和异构工具适配器框架；新算法仍须由维护者实现适配器、发布契约并验证。网页动态安装/卸载模型及自动环境管理尚未实现。MCP发现工具不等同于自动安装模型。

文件通过既有`POST /v1/inputs`上传，正文是TIFF字节，返回`input_id`。不向MCP传任意URL、服务器路径或影像base64。下载使用`result.artifacts`清单中的路径和同身份API密钥，核对字节数与SHA256。真实模型结果为未经独立精度评价的候选；模拟模式明确为mock。

## 独立环境与启动

主应用保持`environment.yml`中的GIS环境。MCP使用独立Python3.11+环境（本机验证为3.12，GPU部署可使用既有3.11基础解释器创建独立venv），依赖固定在`requirements-mcp.in`与跨平台含哈希锁文件中，不向原Conda环境安装SDK：

```powershell
python3.12 -m venv .venv-mcp
.venv-mcp\Scripts\python -m pip install --require-hashes -r requirements-mcp.lock
```

Linux对应环境解释器是`.venv-mcp/bin/python`。复制`examples/mcp/server.example.json`到私有目录并填写实际地址；本机验证时`api_base_url`可指向既有公网HTTPS API，`public_mcp_url`填`http://127.0.0.1:18768/mcp`。

将已交付API连接文件映射为MCP身份，输出令牌只写私有文件：

```powershell
python -m ursa_mcp.issue_key --identity-file "私有目录/identities.local.json" --api-connection "私有目录/application-dev.local.json" --client-file "私有目录/mcp-client.local.json" --owner application-dev --public-mcp-url "http://127.0.0.1:18768/mcp"
python -m ursa_mcp.server --config "私有目录/server.local.json" --port 18768
```

生产的三个URL由维护者配置，调用参数不能改变。只有本机HTTP可用；远程必须HTTPS。限制Host/Origin、MCP正文64KiB、按身份每分钟120次请求；网关再按IP限流。调用者提供的owner和X-Forwarded-Host不会改变身份或Host校验。

## 客户端示例

`examples/mcp/codex.example.toml`给出远程连接配置。将真实令牌通过`URSA_MCP_TOKEN`环境变量提供，配置文件不写令牌。DeepSeek Harness使用其`dsh-mcp-client`的`transport: streamable-http`、`url`和Authorization请求头；凭据应在其可信后端保管，不能内置在公开网页。

同一MCP环境中的官方Python SDK示例：

```powershell
# 默认只查询工具和服务，不提交GPU任务。
python examples/mcp/call_mcp.py --connection-file "私有目录/mcp-client.local.json"

# 查询同一身份已有任务，并下载校验结果。
python examples/mcp/call_mcp.py --connection-file "私有目录/mcp-client.local.json" --job-id 本身份任务ID --results-dir "本次结果目录"

# 明确提交新任务；metadata.json必须描述本次真实影像。
python examples/mcp/call_mcp.py --connection-file "私有目录/mcp-client.local.json" --image "本次影像.tif" --metadata "metadata.json" --tool ursa_classify_landcover --results-dir "本次结果目录"
```

新任务的提交键在调用前写入`submission.json`；同目录重试复用上传ID及提交键，输入或元数据变化时拒绝复用旧记录。结果目录优先`--results-dir`，其次`URSA_RESULTS_DIR`，否则创建新的随机任务目录。客户端等待超时不取消服务器任务，可按Job ID恢复。不要把已有Job的查询当成本次新推理证据。

## GPU和云端部署

先盘点现有服务、端口、磁盘、证书、隧道和SSH权限，确认18768未被其他服务占用。部署不需要上传GitHub分支。

1. `python deploy/mcp/build_release.py --output "发布包.zip"`生成仅含明确源码和锁文件的包，记录release_id和SHA256。
2. 通过已授权SSH通道上传发布包和`install_gpu.py`，核对哈希。
3. 在GPU上使用独立的基础Python3.11+运行安装器；API/relay配置引用原服务已有私有文件，不复制到公开代码包：

```bash
python3.12 install_gpu.py --bundle 发布包.zip --sha256 发布包SHA256 --api-profile 原API的application-dev.local.json --relay-config 原API的relay-deploy.local.json
```

安装根为`~/ursa-mcp-service`，每个发布ID有独立venv；用户级`ursa-mcp.service`先在回环启动。新隧道私钥只留GPU主机，公钥是`private/relay-key.pub`。安装器不会启动未配置云端授权的隧道。

4. 经明确授权的云端维护账号，通过sudo运行`install_gateway.py --site /etc/nginx中的实际站点 --relay-user 既有转发账号 --relay-public-key 新MCP隧道公钥文件`。脚本只在既有API站点加入MCP片段，先备份和`nginx -t`；失败恢复本次站点改动。不变更SSH全局规则，不扩大旧转发密钥权限；新增公钥仅允许18768回环转发且禁止Shell。
5. GPU上运行`systemctl --user enable --now ursa-mcp-relay.service`，检查两个新服务、云端回环端口、公网TLS和认证。
6. 从外部客户端执行官方SDK的工具发现和受控调用。无效令牌应401，跨身份Job/产物不可读；用本次公开样例新建GPU任务，验证推理、TerraMind的4个与DA的5个产物及其哈希，同时复查原API仍可用。

云端使用既有IP HTTPS证书，确认自动续期及Nginx加载新证书；不关闭证书验证。公网地址是`https://公网IP/ursa-mcp/mcp`，内部是`http://127.0.0.1:18768/mcp`。两者前缀映射必须一致。Uvicorn仅信任127.0.0.1转发的代理头，Nginx覆盖协议和来源IP头，Host仍按明确允许列表检查。生产部署前必须具备可用、已授权的SSH账号，登录失败不擅自改用更高权限账号。

## 验证和回滚

主应用环境运行仓库要求的Python全套、Node轮询和`git diff --check`。MCP协议测试在独立环境中另外执行：

```powershell
python -X utf8 -m unittest discover -s tests -p test_mcp_service.py -q
```

测试使用官方SDK、内存HTTP API和临时文件，覆盖现代/旧版握手、Schema、幂等重试、跨身份隔离、撤销、权限、停用能力、非法参数、错误结果、Host/Origin、限流、消息体限制及认证重定向。主应用没有SDK时会明确跳过这组测试，不代表MCP已验证；必须另报独立环境结果。

GPU安装器备份旧MCP单元，启动失败恢复；每个发布环境独立保留。手工回滚只停止MCP服务/隧道、恢复其单元或上一发布，并daemon-reload；不停止原模型API或删除已有任务。云端备份位于`/var/backups/ursa-mcp/<时间戳>`；恢复之前确认目标未被其他任务修改，仅移除本次MCP include、片段和对应公钥行，检查后重载Nginx，不整份覆盖后续他人改动。令牌和API凭据单独管理，不随代码回滚撤销或泄漏。
