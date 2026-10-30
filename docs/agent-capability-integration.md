# Agent能力整合与TerraMind远程分类

本批将桌面水体/LST、Prithvi预览和TerraMind部署代码整合到同一独立工作区，补齐TerraMind的任务选择、远程执行、产物核验与通俗交付。保持既有DeepSeek配置和界面布局。分支为 `codex/agent-capability-integration`，共同基线为 `21151579da66cc804641041f80289ebb0fbbf237`；未拉取远端，不能将此基线称为最新main。

本批为本地未提交修改；未推送、未创建PR、未批准、未合并。三个原工作区保留原样，108份原改动文件逐一SHA256复核未变。下载数据和既有评测不改动。

## 四个可选处理入口

| 用户需求 | 方法与位置 | 当前接受的输入 | 输出 |
|---|---|---|---|
| 提取水体、给出占比 | 本地MNDWI>0 | Sentinel-2六波段B2/B3/B4/B8/B11/B12；明确DN/10000编码；现有工具条件 | 候选水体图、占比、统计 |
| 看哪里比较热 | 本地Landsat 8反演 | 六个已适配热辐亮度/大气辅助量/发射率/QA波段；物理单位、MTL常数与米制投影 | 摄氏度图、统计、相对高温区 |
| 提取水体、给出占比 | GPU Prithvi；127.0.0.1:18765 | Sentinel-2 L1C，13波段，512×512，明确原始DN和来源 | 模型水体、指数对照、分歧提示 |
| 做地物分类、给出主要类别占比 | GPU TerraMind Small+LULC解码器；127.0.0.1:18766 | Sentinel-2 L2A，12波段，224×224，原始DN及来源说明 | 地物候选图、逐类统计、RGB/分类对照与图例 |

这是四条有边界的工具路径，不代表已支持任意遥感任务。去云、变化检测等仍未安装专用模型。TerraMind当前只接整幅地物分类，不把指定单一目标、面积/矢量输出、裁剪或训练需求改写成默认分类。MNDWI的实验选路仍未默认开启。

## 本机测试

本机为隔离整合版使用端口7862，原7860/7861及原目录保留。演示数据均来自已测试的旧样本，仅作功能回归；没有新盲测分数。两个TerraMind样本只增加明确的波段描述与metadata，逐像元、掩膜、坐标系、网格均核对未变。data内只准备6幅输入及各自metadata，没有参考答案，也没有复制评分端的benchmark.local.json。

在上方选择一幅影像，再输入对应的一句话；看到需求确认后回复 `approve`：

| 选择文件 | 可直接输入 |
|---|---|
| `terramind_demo_01/image.tif` 或 `terramind_demo_02/image.tif` | 给我一张地物分类图，说明主要类别和占比。 |
| `prithvi_demo/USA_66511/image.tif` | 帮我提取这张影像中的水体，给出分布图和占比。 |
| `benchmark_suite_20261022/water_01/image.tif` | 帮我提取这张影像中的水体，给出分布图和占比。 |
| `benchmark_suite_20261022/lst_01/thermal_auxiliary.tif` | 帮我看看哪里比较热。 |

应分别出现对应工具的需求确认。完成后直接看结果图、主要数值、待核实事项；方法和参数按需展开“专业详情”，完整过程留在对话区，分析管线仍在右侧。遇到未完成，不用反复提交：保留错误与当前任务目录后检查服务窗口。

本机已有Conda环境可直接使用，不需要重装PyTorch或GIS依赖。项目根目录执行：

```powershell
cmd /d /c 'call "D:\Anaconda3\Scripts\activate.bat" ursa-up && python main.py --port 7862'
```

启动及刷新目录时，现有智能分析建议仍可能调用配置的DeepSeek；本批离线回归全部模拟AI响应，没有发起付费调用。

## GPU服务与本地设置

复用服务器现有 `~/ursa-gpu-runtime/run-python.sh`、固定Small主权重与LULC解码器，保留已验证的diffusers 0.42.0。新增服务不安装、降级或下载依赖。无新权重、无影像、无标签、无密钥的服务包由源码构建：

```powershell
python deploy/terramind_gpu/build_service.py exports/ursa-terramind-service.pyz
```

在现有SSH隧道正常、本机22022能连GPU时，上传包并启动：

```powershell
scp -P 22022 "exports/ursa-terramind-service.pyz" "yy@127.0.0.1:~/ursa-terramind-service.pyz"
ssh -t -p 22022 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18766:127.0.0.1:18766 yy@127.0.0.1 "bash ~/ursa-gpu-runtime/run-python.sh ~/ursa-terramind-service.pyz"
```

等待“模型已就绪”，保持SSH窗口开启。服务只监听GPU本机回环；使用原私有隧道，不开放公网端口。旧Prithvi继续使用自己的18765窗口，本命令不重复绑定18765。本机可只读检查：

```powershell
Invoke-RestMethod 'http://127.0.0.1:18766/health'
```

本地 `gpu.local.json` 是忽略的运行设置，顶层兼容原Prithvi，新增 `terramind` 对象。仓库的 `gpu.example.json` 默认两个工具均关闭。本机整合预览已在私有设置中启用；只有对应服务在线时才能成功执行，不把“配置已启用”当作“服务已在线”。

```json
{"enabled":true,"base_url":"http://127.0.0.1:18765","timeout_seconds":120,
 "terramind":{"enabled":true,"base_url":"http://127.0.0.1:18766","timeout_seconds":120}}
```

TerraMind输入旁需要metadata.json，TIFF自身也要有相同顺序的波段描述、真实空间参考、scale=1/offset=0。不能仅修改声明使任意影像冒充合规数据：

```json
{"sensor":"Sentinel-2","product_level":"L2A","encoding":"DN/10000",
 "bands":["B1","B2","B3","B4","B5","B6","B7","B8","B8A","B9","B11","B12"]}
```

原始DN直接送官方标准化，不预乘0.0001；0～1反射率、L1C、错波段、全无效、超大文件均拒绝，不自动裁剪。当前上传接口上传TIFF后仍须准备可信来源说明；本批没有实现任意产品的自动格式适配。

## 流程、失败与结果核验

Manager保留原需求和所选输入，统一入口优先匹配TerraMind或Prithvi，再走已有本地工具；匹配固定工具后Scientist锁定计划、Engineer生成固定调用，Executor在本任务URSA_RESULTS_DIR执行。Supervisor重算分类统计并检查输入、请求ID、锁定权重、返回ZIP、每个产物SHA及空间网格，再交Manager展示。继续沿用已有remote_tool_plan，按name分派；Session序列化、GUI和CLI同步消费。

TerraMind默认每幅固定seed=20261028、float32、完整权重严格加载；主模型600项、解码器310项、随机补齐0项。此前五例批处理使用seed+样本下标，因此不保证新服务的第2～5例逐像元等于旧批处理；这不是精度提升声明。invalid输入设255，未判定类别0仍占有效区域分母，不删除困难像元。所有有效像元均未判定时，明确无法给出可靠主要地物结论。

每幅输出在当前任务的landcover_01等目录：原始remote_response.zip、final_terramind_classification.tif、terramind_class_statistics.csv、final_terramind_preview.png、remote_result.json；总清单为terramind_workflow.json。逐类含未判定、水、树、淹水植被、作物、建成、裸地、雪冰、云、草灌。占比是模型候选分布，不是准确率或置信度。

远程断开、GPU忙、推理失败、返回损坏或输入/配置被更改均明确停止；不让LLM悄悄换方法、改数据、拼旧任务结果。原通用生成代码的有限修复循环保留，固定远程路径不能靠改锁定算法来通过核验。

## 如何判断好坏

本轮核验回答“结果是否属于本次输入、文件是否完整、统计能否从实际分类重算”。它不能独立判断每个地物是否识别正确。实际模型从未读取参考答案；该声明与哈希校验不能代替操作系统级权限隔离或对恶意服务的密码学证明。

已有五幅TerraMind离线批处理曾事后与官方示例配对ESRI产品比较，像素一致率83.43%、8个出现类别汇总mIoU58.80%；这些是旧试验的产品一致性，预训练重叠未排除、不是独立人工真值，也不是本次Agent新运行的成绩。

现有/assessment自动评分继续保留已适配的水体/LST。新增TerraMind远程任务不伪称已接入该页面自动评分；目前仍需封存当前分类图及输入身份后，由外部评价端另取配对参考对齐类别、空间网格和有效掩膜。本批没有使用参考反复调参，也没有新增盲测。

## 主要文件变化

| 文件 | 从什么到什么 |
|---|---|
| `tools/remote_models.py` | 仅Prithvi入口 → 统一TerraMind/Prithvi远程分派 |
| `tools/terramind_{protocol,remote,workflow}.py` | 独立批处理 → Agent固定计划、私有请求、输出制图、无答案复核与逐幅交付 |
| `deploy/terramind_gpu/{runtime,service,build_service}.py` | 一次运行五幅样例 → 可复用已装权重的常驻地物服务及代码包 |
| `agents/manager_agent.py`、`scientist_agent.py`、`engineer_agent.py`、`supervisor_agent.py` | 分散工具 → 共用路由、锁定执行、核验及通俗说明 |
| `core/session.py`、`core/execution_cycle.py`、`agents/executor_agent.py` | 合并原版本的本地计划、远程计划、修复和执行子进程路径约束 |
| `tools/data_context.py`、`result_delivery.py`、GUI相关文件 | 合并数据来源变化检查、水体/LST及远程多图结果，保留原布局 |
| `gpu.example.json` | 单远程设置 → 原Prithvi顶层兼容＋独立TerraMind设置 |
| `tests/test_terramind_agent.py` | 无接入测试 → 23项新离线检查，含真实HTTP、执行子进程及完整角色链 |

此前桌面和预览版的提示词修改也随整合保留；本批新增TerraMind固定路径由程序契约控制，没有再增加一套外部模型API或放宽原修复提示约束。

## 当前验证与待验证

- 本批最终Python全量288项通过（48.747秒），其中新增23项TerraMind Agent回归；界面JavaScript24项通过；git diff --check通过。
- 真正执行了回环HTTP请求、真实临时GeoTIFF/CSV/PNG、执行器子进程、完整角色交接、ZIP与产物篡改/忙/断开/空输入检查；GPU模型和AI响应被模拟。
- 服务包只有六个代码成员，真实执行--help成功；未捆绑权重、输入、评分器、参考、真实配置。
- 未执行新服务的真实4090D启动、真实DeepSeek联调、新影像精度测评、付费API调用或公网部署。
- 图文记录：[FIX-021](fixes/2026-10-28-021-agent-capability-integration.html)。历史记录保留各批验证快照。
