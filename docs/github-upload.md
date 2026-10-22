# GitHub上传前检查

## 1. 应上传和不上传的内容

| 内容 | 处理 |
|---|---|
| agents、core、gui、tools、prompts、tests、main.py | 上传源代码及测试 |
| docs、README.md、CLAUDE.md | 上传项目说明；发布前检查是否包含不希望公开的记录或本机路径 |
| environment.yml、requirements.txt | 上传依赖清单，不等于上传实际环境 |
| config.example.json | 上传不含真实凭据的配置示例 |
| config.json、.env及常见私钥文件 | 忽略，真实凭据保留本地 |
| data、results、result、exports、temp | 忽略，保留本地数据、产物、对话及执行脚本 |
| .venv、venv、env、.conda等环境目录 | 忽略 |
| .idea、.vscode、.superpowers | 忽略，本机设置不随项目上传 |
| Python／测试缓存、日志、构建产物 | 忽略 |
| 项目根目录ZIP、.bak／.orig／.rej等恢复残留 | 忽略 |

`.gitignore` 只控制Git收录，不删除本地文件，也不能清除历史提交中的文件。不要用 `git add -f` 强行加入被忽略的敏感文件。使用Git推送遵守忽略规则；手工拖拽整个目录或打包上传不会自动应用这些规则。

忽略规则不是完整的凭据检测器。真正公开前仍应复查源代码、说明文档和Git历史；若密钥曾进入提交，应先撤销／轮换，并专门处理历史，而非仅添加忽略规则。

## 2. 本次清理范围

- 恢复备份 `pre_pycharm_history_restore_20261021.zip` 和 `rollback_snapshot_before_task_spec_20261021.zip` 移入Windows回收站，不进入上传内容；需要时可从回收站恢复，前提是尚未清空。
- 清理项目根目录及agents、core、gui、tests、tools中的 `__pycache__`。这些是可重新生成的缓存，无需恢复。
- 保留 `.idea`、真实配置、输入数据、历史结果、对话导出和临时执行脚本，只通过忽略规则排除它们。
- 不清除PyCharm全局Local History，不删除Git历史或备份标签，也不改动Conda环境。

## 3. 上传前检查命令

在项目根目录运行：

```powershell
git status --short
git check-ignore -v config.json data results exports temp .idea
git ls-files
git remote -v
```

确认 `git ls-files` 中没有真实密钥、输入影像、结果、恢复压缩包或环境目录。被忽略但未创建的目录可以不显示；可用其中的具体文件名测试规则。

当前准备工作不创建GitHub仓库、不配置远程、不推送，也不默认变更仓库可见性。确定仓库地址和公开范围后，再配置远程并推送所需分支。普通分支推送不需要使用 `--all`、`--tags` 或强制推送。

## 4. 在其他机器恢复运行

1. 克隆代码后，通过 `environment.yml` 创建Conda环境。
2. 从 `config.example.json` 复制生成本机 `config.json`，填写真实接口配置；已有配置时不要覆盖。
3. 自行创建 `data/` 并放入有权使用的影像。该目录未上传，文档中的示例影像也不会随仓库提供。
4. 在项目根目录启动 `python main.py`。运行产物和导出文件继续保留在本机。

公开发布前还需核实上游URSA的许可证、署名及再分发要求。本次准备未进行外网许可核查。
