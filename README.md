# USRA_UP 遥感多智能体分析系统

基于本地遥感数据的需求确认、分析规划、代码生成、执行和结果核验项目。当前项目使用自有多智能体调度链路。

项目仓库：[1472879139/USRA_UP](https://github.com/1472879139/USRA_UP)。项目展示名称统一为 `USRA_UP`；已有Conda环境仍沿用 `ursa-up`，不需要因改名重建环境。

## 多人协作

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
