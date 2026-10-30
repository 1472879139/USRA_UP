# USRA_UP 遥感多智能体分析系统

基于本地遥感数据的需求确认、分析规划、代码生成、执行和结果核验项目。当前项目使用自有多智能体调度链路。

项目仓库：[1472879139/USRA_UP](https://github.com/1472879139/USRA_UP)。项目展示名称统一为 `USRA_UP`；已有Conda环境仍沿用 `ursa-up`，不需要因改名重建环境。

## 当前自主Agent入口（FIX-025）

新入口 `python main.py --autonomous-agent --port 7864`：DeepSeek可直接解释、选择专用工具/模型、生成Python或组合处理；代码在独立断网Docker中执行，错误回传后有限修复；增加明确来源的遥感数据适配、原分辨率分块拼接和按用户要求逐项验收。普通“发送”直接执行，缺必要信息时才询问。原默认角色链和 `--tool-agent` 路径保留。

首次通用代码准备需启动Docker Linux引擎并运行 `python deploy/python_runtime/prepare.py`；本批开发机器已经准备。完整启动、示例、16工具和边界见[自主Agent使用说明](docs/autonomous-agent.md)，修改及验证见[FIX-025图文幻灯片](docs/fixes/2026-10-29-025-autonomous-agent.html)。当前368项Python、35项网页回归及9项真实容器检查通过；模型响应模拟，真实API/GPU请求0，不代表选路或遥感精度已实测。本轮仅本地未提交修改，以下各批测试数量与状态为历史快照。

## DeepSeek调用状态

FIX-024将调用标志改为对话区上方的整行状态条：请求中显示“DeepSeek正在调用”，结束后保留“本轮已调用N次”，短请求也能留下记录；新会话归零，轮询500毫秒。见[当前状态与重启说明](docs/deepseek-activity-visibility.md)及[图文记录](docs/fixes/2026-10-28-024-deepseek-activity-visibility.html)。本批332项Python、33项界面离线测试通过，另经真实Flask接口与模拟SDK进行浏览器核验；未新增付费调用。此前[FIX-023](docs/deepseek-activity.md)保留真实任务复核和当时状态方案。

## DeepSeek自主调用工具预览

FIX-022在原角色链中新增DeepSeek结构化工具调用：模型从12个受控工具中选择步骤，接收真实返回后继续规划，支持分类、类别筛选、面积统计与制图组合。以 `python main.py --tool-agent --port 7863` 启用；默认旧路径保留。详见[启动、工具清单、接口与限制](docs/deepseek-tool-agent.md)和[图文记录](docs/fixes/2026-10-28-022-deepseek-tool-agent.html)。本批319项Python、24项界面离线检查通过；未发起真实付费API联调，首次真实tools兼容性及模型选择行为仍待验证。已有模型服务和权重不用更新。

## 当前整合预览

FIX-021将本地水体、地表温度、远程Prithvi水体和TerraMind Small地物分类整合到同一Agent入口。具体输入条件、服务器更新、端口7862启动与一句话测试见[整合版使用说明](docs/agent-capability-integration.md)，变更与验证见[图文记录](docs/fixes/2026-10-28-021-agent-capability-integration.html)。本批为本地未提交修改，原工作区保留；288项Python及24项界面测试通过，新增TerraMind常驻服务的真实GPU联调待用户执行。下文FIX-008～020段落中的独立预览和待办描述属于各批历史快照，当前整合状态以FIX-021为准。

## 多人协作

两人开始开发请先看[从GitHub拉取、配置、运行与API联调](docs/developer-setup.md)。应用分支为 `codex/autonomous-task-agent`，模型服务分支为 `codex/model-service-handoff`；新模型API已部署，网页接入由应用方继续实现，不能把旧 `gpu.local.json` 当成统一API配置。

开发前请阅读[多人协作开发规范](CONTRIBUTING.md)。采用独立任务分支与PR审查流程，禁止直接向 `main` 推送开发改动；规范包含共享接口、测试、环境依赖、冲突处理和合并检查要求。GitHub分支保护及CI需由维护者另行配置，文档不会自动启用这些限制。

首次协作可直接按规范第4节执行“同步main → 任务分支开发 → 本地提交 → 推送 → PR审查 → 合并后同步”，无写权限成员使用其中的Fork流程。使用Codex时从项目根目录开始，并读取 [AGENTS.md](AGENTS.md) 和规范第14节；文档中的推送与合并示例不构成自动发布授权。

## 本地运行

在项目根目录执行：

```powershell
conda env create -f environment.yml
conda activate ursa-up
# 仅首次配置时执行；如果已有 config.json，不要覆盖
if (-not (Test-Path -LiteralPath config.json)) {
    Copy-Item -LiteralPath config.example.json -Destination config.json
}
New-Item -ItemType Directory -Force -Path data | Out-Null
python main.py
```

首次启动前，编辑本地 `config.json`，填写服务商支持的模型名、API密钥和OpenAI兼容接口地址。示例中的密钥和地址都是占位内容，不能直接请求。将自己的影像放入 `data/`。界面默认地址为 `http://127.0.0.1:7860`。

`environment.yml` 中先安装Conda的NumPy、Rasterio/GDAL、Matplotlib编译依赖，再安装pip层依赖。Windows下不要在此基础上随意用pip覆盖GDAL/Rasterio，以免引入DLL冲突。生成的分析代码如需额外算法依赖，应单独核验与现有环境的兼容性。

实际虚拟环境、输入影像、密钥配置和运行产物不进入Git；保留 `environment.yml` 和 `requirements.txt` 作为环境重建说明。

在另一台电脑使用当前功能分支时，按[跨电脑运行与资料迁移说明](docs/migrate-to-another-computer.md)克隆正确分支、重建环境，并单独配置API和复制所需影像。

## 输出与测试

FIX-013新增[地表温度工具与外部产品一致性评价](docs/lst-agent.md)：选择已有 `lst_01/thermal_auxiliary.tif`，输入“帮我看看哪里比较热”，可得到中文温度图、摄氏度统计、相对高温位置和无效范围。只支持已提供大气辅助量、发射率和MTL常数的Landsat 8样本；普通RGB不生成假温度。结束后在答卷页独立计算温度差与覆盖率；与同源USGS产品一致不等于独立地面测温准确。当前仅本地未发布，详见[本批图文记录](docs/fixes/2026-10-26-013-lst-agent.html)。

普通对话现已接入固定水体工具，例如输入“帮我提取water_01和water_02的水体，告诉我有多少”，可走已安装工具的完整处理路径。正常GUI/CLI只检查产物与统计，不读取参考答案。开发者在任务结束后通过 `http://127.0.0.1:7860/assessment` [封存答卷并独立评分](docs/blind-assessment.md)，分数不反馈到本次任务；当前不代表所有方向均已接入。

已有[固定方法验收基础](docs/validated-benchmarks.md)：启动后打开 `http://127.0.0.1:7860/benchmarks`，可运行固定水体基线并与人工答案独立评分，不调用模型。目前17例可检查材料完整性，10例水体接入自动评分；这与Agent交卷测评分别使用。[4个新增场景首次评测](docs/water-generalization-validation.md)发现两例明显漏检，相关质量提示现由外部评分展示，普通新影像不凭空知道漏检率。其他方向与跨数据集泛化尚待验证。参考答案单独保存在项目外，需要评分端私有 `benchmark.local.json` 配置，不能只复制 `data/`。

此前截至2115157的[累计修改记录](docs/source-to-current-changelog.md)提供源码基线、阶段演变、逐文件清单、验证证据及发布状态，也可查看[当时的累计图文幻灯片](docs/fixes/2026-10-23-source-to-current-overview.html)。本轮新增验收功能单独记录于[FIX-008图文幻灯片](docs/fixes/2026-10-25-008-validated-water-benchmark.html)。

后续批次分别见[FIX-009：对话工具接入](docs/fixes/2026-10-25-009-water-agent-integration.html)及[FIX-010：新场景与话术稳定性](docs/fixes/2026-10-25-010-water-generalization.html)；各批记录保留当时范围与验证快照。

当前行为以[FIX-011：普通分析不读答案、交卷后独立评价](docs/fixes/2026-10-26-011-blind-assessment.html)为准。已提供干净运行包准备工具；本机只验证应用路径分离，严格操作系统权限隔离尚未启用，详见完整说明。

[FIX-012新增两种方法与输入选路实验](docs/water-method-library.md)：十幅场景共40次离线处理后统一评分。旧困难样本改善，但新四幅平均成绩未改善，因此未替换默认方法。可用 `python -m benchmarks.compare --case water_05 --output exports/water_methods_manual_01`生成独立比较报告；Manager响应模拟，真实模型请求0。
FIX-014提供[远程Prithvi工具与无答案分歧检查](docs/prithvi-remote-agent.md)：在私有SSH转发下调用4090D常驻模型，锁定输入、返回包与方法，输出候选水体和指数对照。仅适配已声明来源及编码的Sentinel-2 L1C、13波段、512×512影像；默认不启用。查看[图文升级记录](docs/fixes/2026-10-27-014-prithvi-remote-agent.html)。本分支为独立预览，桌面原目录未提交的FIX-008～013未整合，原版本仍保留；真实GPU服务及真实DeepSeek调度尚待用户联调。

[从GitHub源码到当前本地版本的累计修改记录](docs/source-to-current-changelog.md)提供源码基线、阶段演变、逐文件清单、验证证据及发布状态；也可查看[累计图文幻灯片](docs/fixes/2026-10-23-source-to-current-overview.html)。

界面统一使用一种展示方式。可先选择已有影像，或添加TIFF文件（一次最多8个、总计128MB），再用一句话说明想了解什么。没有明确目标时先推荐可做的分析，不自行决定分析目的。目录文件扫描位于对话区上方，支持展开、收起和刷新；桌面右侧保留分析管线、监督、追溯与导出入口。完成后直接查看结果图、通俗说明、尚需核实的事项和下载入口；需要方法和参数时，在结果下方展开“专业详情”。完整分析消息、代码和本次对话仍在对话区的“查看分析过程与数据”中。

“结果已生成”与“识别准确”分别说明。当前尚未接入独立精度验证时，系统保留候选状态，不把模型自评作为可信百分比。普通RGB影像缺少测温信息、变化分析只选择一期等条件不足会被指出。新界面需重启程序生效；不会自动重跑旧会话。详见[普通用户结果交付说明](docs/plain-language-results.md)及[FIX-007图文记录](docs/fixes/2026-10-23-007-sidebar-pipeline-layout.html)。

每个新任务的产物保存至 `results/task_年月日_时分秒_随机标识/`，同一任务的代码修复复用该目录。

- [遥感测试案例与验收指标](docs/remote-sensing-test-cases.md)
- [数据预检与结果核验设计](docs/data-aware-analysis-design.md)
- [任务输出目录约定](docs/task-results.md)
- [GitHub上传前检查](docs/github-upload.md)
- [产物核验失败后的自动诊断与修复循环（图文记录）](docs/fixes/2026-10-22-004-artifact-repair-loop.html)

GUI 和 CLI 共用“执行 → 产物核验 → 诊断 → Engineer 修复 → 再执行、再核验”循环。执行报错与产物核验失败共用 `executor_max_retries` 总尝试上限，包含首次执行；默认最多 5 次执行、4 次修复请求，不另起无限重试。达到上限、缺少必要输入、用户终止、跨任务产物或模型未返回可执行代码时停止，不生成成功报告；语义待复核提示不会触发重跑。

修复上下文包含原始需求、有序修改、数据事实和上一版完整代码。修复复用原任务目录，并累计本循环实际生成的产物清单，防止只更新 CSV 后漏掉原图、栅格或此前失败文件。此前已失败的分类统计不能通过删除必需字段、改成无法识别的表格来被当作修复成功。每次产物核验在现有追踪中记录 `artifact_validation`，可查看尝试编号、错误、提示和文件清单。

本功能不修改产物读取器或核验规则，也不允许模型以改规则、填假数或读取参考答案来通过核验。例如不可计算的可选面积可调整为现有接口支持的省略面积列，同时在报告说明缺失；不能把未知面积改成零。模型仍可能无法修复；规划阶段失败、数据缺失、算法选择和独立遥感精度不由这个循环保证。保持原有 API 配置，自动修复可能增加调用费用；本地离线测试使用模拟模型，不发起真实收费请求。修改代码后重启程序生效，旧会话不会自动重跑。

离线回归测试：

```powershell
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
```

第二条命令需要已安装Node.js，仅用于界面测试。

## 使用限制

无独立参考样本时，分类语义及精度需要人工复核；数值一致不代表地物识别正确。生成代码的检查与子进程执行也不是操作系统级安全沙箱，请勿把服务直接暴露到不受信任网络。

本项目由URSA相关代码重构而来。公开发布或再分发前，应核对原项目授权及署名要求；本仓库说明不授予或替代上游许可。
