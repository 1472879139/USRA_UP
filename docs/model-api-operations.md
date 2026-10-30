# 模型 API 运维说明

## 当前部署结构

```text
各电脑本机应用 ── HTTPS/Bearer ──> 云服务器 Nginx :443
                                      │ 仅该 API 路径
                                      ▼
                                 127.0.0.1:18767
                                      ▲
                             GPU 主动建立 SSH 反向转发
                                      │
                                GPU 127.0.0.1:18767
                                      ▼
                          单任务队列 → 独立推理子进程
```

GPU 无需公网 IP；不向公网暴露 GPU 的 SSH 或推理端口。各调用者持有自己的 API 密钥，无需取得服务器 SSH 密钥。管理通道与 API 中转使用不同凭据。

已使用现有服务器证书与自动续期计时器；证书到期仍需持续监控。Nginx 原站点已备份，仅加入独立 API 路径、限流和连接限制，保留其他业务路径。真实地址、账号、认证文件均不写入此文档。

## 1. 本机模拟开发

应用开发 ZIP 已带合成 fixtures，Python 3.10 及以上即可，**不需要 Conda、GIS 或 GPU**。解压到新目录，在包根目录执行：

```powershell
python -m model_service serve --mode mock --data-dir .model-service/mock --port 18767
```

另开终端到同一目录，运行：

```powershell
python examples/model_api/call_api.py --mock --input examples/model_api/mock-fixtures/input.tif --metadata examples/model_api/mock-fixtures/metadata.json --capability da-v2-field-delineation --output .model-service/demo-da
python examples/model_api/call_api.py --mock --input examples/model_api/mock-fixtures/input.tif --metadata examples/model_api/mock-fixtures/metadata.json --capability terramind-small-landcover --output .model-service/demo-terramind
```

默认认证令牌仅是公开的本机模拟标记。服务强制监听127.0.0.1；不得用反向代理把 mock 暴露为真实服务。模拟结果始终有 `execution_mode=mock`、`synthetic_demo` 与图像水印。

如果使用源码而非已带 fixtures 的 ZIP，在具备项目 GIS 依赖的打包机运行一次：

```powershell
python -m model_service.prepare_demo examples/model_api/mock-fixtures
```

该命令要求目标目录尚不存在，避免覆盖已准备文件。fixtures 属于可重建合成材料，未纳入 Git。

## 2. GPU 服务管理

本次部署复用现有已验收环境；不重新安装 PyTorch/GDAL/Rasterio，不下载替代模型。安装目录为 `~/ursa-gpu-runtime/model-api-20261030`。

运行时映射保存在该目录的 `runtime.local.json`：DA 使用既有独立 venv 和任务权重；TerraMind 首版适配复用 `~/ursa-gpu-runtime/services/landcover_292e61f891f9a042/runtime.py` 中已核验的 GPUModel。这里只导入模型执行代码，不需要旧 HTTP 服务运行。**这些历史运行时目录仍是当前 API 的依赖，不能当缓存删除。** 后续迁移需连同权重、固定运行时和环境一起规划；仅克隆 API 源码不能在空服务器直接启动两种真实模型。

在 GPU 用户终端：

```bash
systemctl --user status ursa-model-api.service ursa-model-api-relay.service --no-pager
journalctl --user -u ursa-model-api.service --since '15 minutes ago' --no-pager
journalctl --user -u ursa-model-api-relay.service --since '15 minutes ago' --no-pager
curl --fail http://127.0.0.1:18767/health
```

两项服务已配置随用户管理器启动和故障重启；已核验用户 linger 开启。它们不依赖 Windows 保持 SSH 窗口。GPU 主机或云服务器断网仍会影响可用性；这里只做了运行状态核验，未安排真实断电演练。

恢复时按实际故障分别操作，重启 API 会中断运行任务：

```bash
systemctl --user restart ursa-model-api-relay.service
systemctl --user restart ursa-model-api.service
```

只读健康成功不代表模型已加载；模型按任务启动独立进程，结束后释放资源。旧模型服务与其他 GPU 使用者仍可能竞争显存，模型维护者需协调。

## 3. 发布更新

在模型服务任务工作树打包：

```powershell
python deploy/model_api/build_release.py model-api-runtime.zip
```

输出发布 ID、逐文件清单和 ZIP SHA256。只含 `model_service/*.py` 与 SDK，没有凭据、权重或用户数据。将包与安装脚本通过既有管理通道送入 GPU。

GPU 用原 `run-python.sh` 执行 `install_remote.py --bundle <ZIP> --sha256 <SHA256> --relay-config <私有中转配置>`。安装器核对权重和发布哈希，保留旧 release 与数据目录，更新本服务的 systemd 单元并重启 API。中转配置结构为 host、user、host_key、public_url；必须使用已通过可信渠道核验的云端主机公钥，不能盲目接受新指纹。

`install_gateway.py` 用于本次云站点的独立接入，首次已执行；它不是面向任意站点的一键覆盖脚本。更换云服务器时先审查站点锚点、备份和服务路径，不能直接覆盖其他人的 Nginx 配置。云转发密钥被限制为指定回环端口，不提供交互 Shell。

回滚时先确认没有运行任务，将本任务的 `~/.config/systemd/user/ursa-model-api.service` 中 WorkingDirectory 指向仍保留的旧 release，执行 daemon-reload、restart，再核验 health、契约和样例。不要改旧业务服务或删除数据目录来回滚。

## 4. 凭据与私密交付

- `api-keys.local.json`：服务端 owner → token 的 SHA256；服务器只存散列。
- `private/maintainer.local.json`：维护者客户端原始密钥。
- `private/application-dev.local.json`：应用开发者独立密钥；只将这一份通过私密渠道给对方，不发送维护者凭据或服务器私钥。
- `runtime.local.json`、中转配置、SSH 私钥均不进 Git 或公开 ZIP。

新增身份时在当前 release 的目录中运行以下命令，替换身份与私有地址：

```bash
bash ~/ursa-gpu-runtime/run-python.sh -m model_service issue-key \
  --owner another-developer \
  --keys-file ~/ursa-gpu-runtime/model-api-20261030/api-keys.local.json \
  --client-file ~/ursa-gpu-runtime/model-api-20261030/private/another-developer.local.json \
  --base-url https://MODEL_HOST/ursa-model-api
systemctl --user restart ursa-model-api.service
```

命令不会打印原始 token。撤销身份时备份认证文件，删除指定 owner 项并重启 API；新请求失去访问权限。已运行的任务不会仅因撤销密钥自动停止，必要时在撤销前取消。轮换时用新 owner 发放新凭据，再撤销旧身份。

## 5. 数据与容量

API 数据位于 root/data，含 SQLite 索引、上传和任务产物。不会自动清除原始输入或结果。接近容量阈值时先停止接收新工作，等待或取消活动任务，停止 API，备份**整个 data 目录**，然后在服务单元中配置新的空数据目录并启动。旧任务只能在旧索引/文件一起恢复的服务中查询；提前通知调用者并导出所需结果。不要在线删除 SQLite 或只删某个任务目录。

维护者的 `worker.log` 可能有内部路径和诊断信息，不通过 API 对外返回；共享日志前先审查内容。Nginx 专用访问日志不记录 Authorization、请求正文或查询串。

## 6. 排障顺序

1. GPU 本机 health：失败先看 API systemd 与日志。
2. 云回环18767 health：失败检查反向转发单元、云受限公钥、端口冲突。
3. 云本机 HTTPS：检查 nginx -t、证书与续期计时器，保持证书校验。
4. 调用电脑 HTTPS：若 GPU 所在网络成功而某电脑 TLS 握手失败，排查该电脑代理/TUN/网络路径；不要改成公网明文 HTTP 或关闭证书校验来“通过”。
5. health 成功后再查401、能力、输入预检和任务错误。不要把 TLS 问题误判为模型权重损坏。

本轮 Windows 公网 TLS 的最终验收状态在交接目录的 `验收结果.json` 中记录。只有从该电脑实际完成调用，才能标记该电脑连接通过；本机 mock 或 GPU 网络验证不能替代另一台电脑验收。

2026-10-30 网络复查：用户按代理/TUN排查提示调整后，浏览器恢复 `ready`；本机 Windows SDK 随后使用标准证书校验，通过正式 HTTPS 完成认证、能力与契约查询、两个已封存任务查询及9份产物下载哈希核验，无效密钥和跨身份访问也被拒绝。本次只复查既有任务，没有启动新推理或付费 DeepSeek 请求。恢复后系统路由仍显示 Mihomo，因此不把“必须永久关闭TUN”作为结论，也未定位具体拦截规则。再次发生握手中断时先检查该电脑代理规则和网络路径。另一台真实客户端仍需自行验收。
