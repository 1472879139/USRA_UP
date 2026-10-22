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

## 输出与测试

每个新任务的产物保存至 `results/task_年月日_时分秒_随机标识/`，同一任务的代码修复复用该目录。

- [遥感测试案例与验收指标](docs/remote-sensing-test-cases.md)
- [数据预检与结果核验设计](docs/data-aware-analysis-design.md)
- [任务输出目录约定](docs/task-results.md)
- [GitHub上传前检查](docs/github-upload.md)

离线回归测试：

```powershell
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
```

第二条命令需要已安装Node.js，仅用于界面测试。

## 使用限制

无独立参考样本时，分类语义及精度需要人工复核；数值一致不代表地物识别正确。生成代码的检查与子进程执行也不是操作系统级安全沙箱，请勿把服务直接暴露到不受信任网络。

本项目由URSA相关代码重构而来。公开发布或再分发前，应核对原项目授权及署名要求；本仓库说明不授予或替代上游许可。
