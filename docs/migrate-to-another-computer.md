# 在另一台电脑运行当前版本

适用于`codex/plain-language-results`功能分支。main尚未合入时，直接克隆默认分支不会取得这些新功能。阶段HTML和累计记录中的“未提交／未推送”是当时快照，实际发布状态以Git远端分支和提交号为准。

## 需要带走的内容

| 内容 | 是否需要 | 如何处理 |
|---|---|---|
| 代码、prompts、environment.yml、文档 | 必需 | 从下述GitHub任务分支克隆，无需手工复制代码 |
| `config.json` | 需要配置，文件可以重建 | 从旧电脑安全复制到新项目根目录，或由`config.example.json`生成后重新填写模型名、API地址和API密钥；不上传GitHub |
| `data/`中的输入影像、辅助数据及参考答案 | 要运行相同案例就需要 | 保持原来的子目录结构复制；只运行新案例时可以改用自己的数据 |
| `exports/benchmark_suite_20261022/` | 可选，继续基准测试时建议带 | 包含本地测试资料；若需要原提示词和评测说明，一并复制 |
| `results/task_.../`及其他需要保留的结果、评测报告 | 可选 | 只用于保留旧图、栅格、表格和报告，不是启动程序的必需项 |
| `exports/`中的对话导出 | 可选 | 用于查阅以前记录；复制文件不会恢复网页中正在运行的会话 |
| `data/.engineer_memory.json` | 可选，可不带 | 是本地经验记录，不是输入影像；新电脑可从空记录开始 |
| Anaconda安装目录、Conda环境目录、`.venv` | 不需要直接搬 | 根据仓库的`environment.yml`重新创建环境，避免旧路径和GIS动态库问题 |
| `temp/`、缓存、日志、本地验证脚本 | 不需要 | 运行时生成；正常启动还会清理temp，不作为历史备份位置 |

2026-10-23核对时，本机`data/`约44MB，主要包括：

- `data/benchmark_suite_20261022/`：之前准备的多方向测试集，含输入和参考资料。
- `data/eswd_water_eval_20261022/`：水体测试资料。
- `data/system_benchmark_20261022/`：系统测试资料。
- `data/Sentinel2_Dongcheng_20230718.tif`：原有影像。

最省事的做法是复制整个`data/`，其中经验记录可以跳过。若之后通过界面添加过影像，也要带上相应的`data/uploads/`子目录。数字仅是此次资料快照，之后添加文件会变化。

## 新电脑首次运行

先准备Git和Anaconda或Miniconda。下面命令在新电脑的**Anaconda Prompt**中执行；这样能避免尚未初始化的PowerShell无法激活Conda的问题。

选择一个尚未存在`USRA_UP`子目录的位置，然后执行：

```bat
git clone --branch codex/plain-language-results --single-branch https://github.com/1472879139/USRA_UP.git USRA_UP
cd USRA_UP
conda env create -f environment.yml
conda activate ursa-up
```

如果新电脑已有同名环境，先确认用途，不要直接删除或覆盖。`environment.yml`会先安装Conda的NumPy、Rasterio/GDAL和Matplotlib，再安装pip层依赖；不要额外用pip覆盖GDAL/Rasterio。

接着复制旧电脑的`config.json`到项目根目录，并把需要的`data/`复制到项目内。没有旧配置时，用以下命令仅在文件不存在时创建示例配置：

```bat
if not exist config.json copy config.example.json config.json
notepad config.json
```

示例API密钥和地址是占位内容，必须填写自己可用的配置。当前系统使用外接API，新电脑需要能访问相应服务，不需要搬运本地DeepSeek模型权重。如果配置或旧提示词中写了旧电脑的绝对路径，改为新位置；数据和结果路径尽量使用`data`、`results`等项目相对路径。

从项目根目录启动：

```bat
python main.py
```

在新电脑浏览器打开`http://127.0.0.1:7860`，选择影像并提出问题。建议使用`data/benchmark_suite_20261022/...`这样的相对路径，不继续使用旧电脑的盘符路径。

只为使用网页界面，不需要安装Node.js；Node仅在运行JavaScript界面测试时需要。复制旧结果和对话文件不会恢复原电脑内存中的活动会话，新分析需要重新发起。

## 核对是否拿到正确代码

```bat
git branch --show-current
git log -1 --oneline
```

分支应为`codex/plain-language-results`。提交号与交付消息中已核验的提交一致；不要因为仓库页面默认显示main，就误以为main已经包含任务分支的功能。

以后在这份工作副本更新当前分支，可在保存工作并确认没有未处理本地修改后运行：

```bat
git pull --ff-only
```

本说明不自动迁移真实配置、影像或历史对话，这些资料继续由用户单独保管和传输。
