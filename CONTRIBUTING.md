# USRA_UP 多人协作开发规范

版本：1.1；更新日期：2026-10-22。

适用仓库：[1472879139/USRA_UP](https://github.com/1472879139/USRA_UP)。适用于人工开发与AI辅助开发。

本文中“必须／禁止”属于团队协作约定；“建议”可由维护者根据任务调整。**写入文档不会自动启用GitHub分支保护、审查要求或CI。本次仅制定规范，不代表这些设置已生效。**

首次协作请完整阅读第4节；无原仓库写权限时走第4.9节Fork流程。使用Codex的成员还须阅读第14节及根目录 [AGENTS.md](AGENTS.md)。命令是操作示例，不是授权AI自动发布、审批或合并的指令；遇到错误必须停在当前步骤处理，不能整段忽略报错继续执行。

## 1. 必须遵守的核心规则

1. `main` 是共享稳定分支，必须保持可安装、可启动、既有测试通过；禁止直接向 `main` 推送开发改动。
2. 一项任务对应一个短期分支和一个PR。功能、修复、重构、依赖升级不要混进同一个PR。
3. 协作者变更必须经PR审查，并取得仓库负责人 `1472879139` 的正式批准，再由负责人执行合并；其他成员的审查是补充，不能代替负责人确认。负责人自己的PR不能自己批准，应另行安排非作者审查及明确的维护例外。紧急修复也走PR，只缩短响应时间，不省略审查。
4. 先约定共享接口，再并行实现；不能擅自修改其他成员正在依赖的消息字段、状态、会话数据或前后端协议。
5. 功能与缺陷修复必须提供相应测试；提示词改动也属于行为变更，不能按“仅文字修改”免测。
6. 不提交密钥、真实配置、虚拟环境、业务影像、运行产物、对话记录、缓存或恢复备份。
7. 冲突由提交者在任务分支解决，涉及其他成员逻辑时共同确认；禁止靠整文件覆盖或强制推送丢弃他人修改。
8. 作者对最终提交负责，包括AI生成的代码、测试、提示词和文档。

## 2. 团队角色与任务认领

| 角色 | 责任 |
|---|---|
| 仓库维护者 | 确认范围、协调共享接口、安排审查、合并PR、处理版本与回滚 |
| 任务负责人 | 认领Issue、开发、自测、更新文档、处理审查与冲突 |
| 审查者 | 检查需求、实际代码、测试证据与兼容性，不能只看界面截图 |

实际负责人通过Issue或PR指定，不在本文预设未确认的人员名单。仓库管理员权限仅分配给确实需要管理仓库的成员，日常协作者使用满足开发需求的最小权限。

开发前创建或认领Issue，至少写明：

- 背景与可复现问题，或明确的用户需求。
- 本次目标、不做什么、验收标准。
- 负责人、审查者、预计涉及的模块和共享文件。
- 是否涉及依赖、消息结构、会话状态、接口、模型调用或数据安全。
- 相关Issue、前置PR及已知兼容性风险。

纯文档或明显的小修订可在PR中记录以上信息，不强制单独建Issue；仍需独立分支与审查。

## 3. 分支与提交命名

当前采用 `main + 短期任务分支`，不额外引入长期 `develop` 分支。只有维护者另行批准发布维护需求后，才建立版本维护分支。

分支名称使用小写英文、数字和连字符：

| 类型 | 示例 |
|---|---|
| 新功能 | `feat/42-task-export` |
| 缺陷修复 | `fix/43-manager-context` |
| 重构 | `refactor/44-data-inspection` |
| 文档 | `docs/45-contributing` |
| 测试 | `test/46-result-audit` |
| 环境或工具维护 | `chore/47-dependency-update` |
| Codex创建的分支 | `codex/48-manager-context` |

示例编号表示对应Issue，不要照抄不存在的编号；没有Issue时可省略编号。禁止多人未经约定共同推送同一个任务分支。任务合并后不复用原分支继续开发新任务。

提交信息采用：

```text
<type>(<scope>): <说明>

fix(manager): 保留用户多轮修改中的未变更条件
test(audit): 覆盖跨任务产物误引用
docs(contributing): 增加冲突处理流程
```

`type` 使用 `feat、fix、refactor、test、docs、chore`；`scope` 可省略。说明可用中文，必须描述实际变化，避免“修改一下”“最终版”等无信息表述。共享协议的破坏性变更必须在PR标题或说明中明确标注，不靠提交名称代替迁移说明。

## 4. 日常开发流程

### 4.1 先理解本地、远端和权限

完整流程：

```text
远端主仓库main
  → 更新本地main
  → 创建本地任务分支
  → 开发、测试、git commit
  → git push到远端任务分支
  → 创建目标为主仓库main的PR
  → 审查修改、原分支追加提交
  → 负责人批准并合并
  → 本地同步新main，开始下一任务
```

| 名称／操作 | 含义 | 不代表什么 |
|---|---|---|
| 本地 `main` | 开发者电脑上的主分支 | 不会始终自动等于GitHub上的最新main |
| `origin` | 一个远程别名；实际地址用 `git remote -v` 检查 | 不一定是主仓库，Fork场景通常是自己的Fork |
| `origin/main` | 上次fetch记录的origin主分支位置 | 不fetch就不一定最新，也不是当前工作区 |
| `git fetch` | 获取远端提交并更新远端跟踪引用 | 不自动把提交合入当前分支 |
| `git commit` | 将已暂存改动保存成本地提交 | 没有上传到GitHub |
| `git push` | 将本地提交上传到指定远端分支 | 没有自动创建PR，也没有合并main |
| PR | 请求将来源分支的变化合入目标分支 | 已创建不等于已批准或已合并 |

先选择协作方式：

- **有主仓库写权限**：可以向主仓库的任务分支推送，按第4.2—4.8节执行；仍禁止直接推送 `main`。
- **没有主仓库写权限**：先Fork，向自己的Fork推送，再向主仓库提交跨仓库PR，按第4.9节执行。
- 能克隆公开仓库不代表有写权限。权限不明时请负责人确认；不要用试推 `main` 来探测权限，也不要擅自修改权限或切换账号。

### 4.2 首次加入：克隆与环境准备

以下为有主仓库写权限成员的首次克隆命令；只读访问者也能克隆，但之后推送必须改走Fork流程。已有工作副本不要再次克隆覆盖原目录。

```powershell
git clone https://github.com/1472879139/USRA_UP.git
cd USRA_UP
git remote -v
git branch --show-current

# 仅首次创建环境时执行；已有环境先确认，不要覆盖或重建
conda env create -f environment.yml
conda activate ursa-up
if (-not (Test-Path -LiteralPath config.json)) {
    Copy-Item -LiteralPath config.example.json -Destination config.json
}
```

在本机配置真实API信息，不提交 `config.json`。按[README](README.md)准备自己的影像；仓库不包含业务输入文件。日常离线测试不需要调用真实模型，也不应产生API费用。

### 4.3 每个新任务：更新main，再创建分支

以下各步骤在项目根目录执行。先检查：

```powershell
git rev-parse --show-toplevel
git status --short
git branch --show-current
git remote -v
```

`git status --short` 没有输出表示工作区干净；有输出时先判断改动归属。不得把其他任务、其他开发者或用户尚未处理的改动混进本任务；也不能擅自提交、stash、丢弃或覆盖这些改动。可在负责人确认后保存原任务，或使用独立克隆／工作树隔离。

确认 `origin` 指向主仓库、工作区干净且本地有 `main` 后，逐步执行：

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c codex/manager-context
git branch --show-current
```

预期最后显示 `codex/manager-context`。该名称只是同一任务的贯穿示例；实际使用时选择未被占用且符合任务的名称，并替换后续所有对应分支名。

- 若旧副本没有本地 `main`，在fetch后检查 `git branch -a`，可在工作区干净且确认不存在同名本地分支时先执行 `git switch --track origin/main`。
- 若分支名已存在，先确认是否为同一任务。不要删除或重置它；继续既有任务应核对其提交和PR后再切入。
- 若当前是detached HEAD，先确认工作树用途和起点，不能直接提交后推送到 `main`。
- 若 `pull --ff-only` 报分叉，说明本地main有独有提交或历史变化；停止并检查 `git log --oneline --graph --decorate -15`，不能改用强制重置、自动rebase或普通pull掩盖原因。

### 4.4 开发、自测与本地提交

只在任务分支修改，先读相关代码、测试和共享接口约定。修复问题时先复现并补回归测试，不修改无关模块。

```powershell
git status --short
git diff
git diff --check
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
```

逐条检查退出状态：前面的命令失败时不要继续执行提交或上传。PowerShell默认不保证一行原生命令失败就停止后续行。若用于自动化，可在每条Git／Python／Node命令后检查 `$LASTEXITCODE`，非零立即停止；对于“无匹配”允许返回1的只读查询，必须单独解释，不能盲目套用。

测试和自查通过后，核对当前任务文件，再暂存和本地提交：

```powershell
# 按本次实际文件替换下列示例，逐个加入，避免盲目 git add .
git add -- agents/manager_agent.py tests/test_handoff.py
git diff --cached --name-only
git diff --cached
git diff --cached --check
git commit -m "fix(manager): 保留多轮需求上下文"
git log -1 --oneline
git status --short
```

暂存后尤其检查真实配置、输入数据、产物及不属于本任务的文件。`git diff` 默认不显示未跟踪文件的正文，必须结合 `git status` 和实际文件阅读检查。检查完若又修改文件，需重新暂存并验证实际待提交版本。

不要提交命令输出中的密钥。测试失败须先修复或在草稿PR中如实说明，不能宣称通过；未解决失败不满足合并条件。

### 4.5 首次将任务分支上传远端

这是对远端的写操作。人工按任务流程执行；Codex必须先确认用户已明确授权推送到此仓库的任务分支。

上传前保存当前工作，确认无未处理改动；获取主分支最新变化，检查差异：

```powershell
git branch --show-current
git remote -v
git fetch origin
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

如果main已更新，在自己的任务分支执行 `git merge origin/main`；出现冲突按第9节处理，合并后重新测试。变更必须针对最新相关主分支代码验证，不能把旧测试结果当成合并后结果。

确认当前分支及差异正确后：

```powershell
git push -u origin codex/manager-context
git status -sb
git ls-remote origin refs/heads/codex/manager-context
git rev-parse HEAD
```

确认推送返回成功，远端任务分支的提交ID与本地HEAD一致。首次 `-u` 建立上游关系；后续仍建议明确写出远端与分支，防止推错。禁止使用 `HEAD:main`、`--all`、`--mirror` 或顺手推送所有标签。

若被拒绝，先区分登录失败、权限不足、远端有他人提交或规则阻止，参见第4.10节。**推送成功只代表远端任务分支更新，main尚未改变。**

### 4.6 在GitHub创建PR

推送后打开[主仓库](https://github.com/1472879139/USRA_UP)：

1. 点击任务分支对应的 **Compare & pull request**；没有提示时进入 **Pull requests → New pull request**。
2. 核对 `base repository` 为 `1472879139/USRA_UP`，`base` 为 `main`，`compare` 为 `codex/manager-context`。不要颠倒来源和目标，不要把另一个成员的任务分支选为目标。
3. 检查 **Commits** 和 **Files changed**，确保仅包含本次任务；若带入旧任务提交，先检查分支起点，不要直接请求合并。
4. 按第10节填写说明、关联Issue，邀请负责人 `1472879139` 审查；核心模块再邀请相关成员。
5. 未完成或仍需讨论时选择草稿PR；准备就绪后再标记为可审查。
6. 记录PR链接。创建PR不代表已批准、已完成合并或已通过自动检查。[官方PR流程](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request)

已安装并登录GitHub CLI时，也可在**获得创建PR授权后**使用下列示例，正文须填写完整的第10节信息，不能仅保留示例说明：

```powershell
gh pr create --repo 1472879139/USRA_UP --base main --head codex/manager-context --title "fix(manager): 保留多轮需求上下文" --body "请替换为完整PR说明" --reviewer 1472879139
```

同一来源分支已有相同任务的未合并PR时应继续更新原PR，不重复创建；自己就是PR作者时不能请求自己审查，需另行安排审查者。

### 4.7 审查要求修改：更新同一个PR

PR未合并时，继续在原任务分支处理意见，不另开分支或另建同内容PR：

```powershell
git status --short
# 确认可以安全切换后；若已经在此分支，无须再次切换
git switch codex/manager-context
# 修改相关文件并运行第8节测试
git add -- agents/manager_agent.py tests/test_handoff.py
git diff --cached
git diff --cached --check
git commit -m "fix(manager): 根据审查补充边界处理"
git push origin codex/manager-context
```

授权原PR范围内继续推送修订时可复用该授权；没有这项授权的Codex应先确认。若远端分支被他人修改，先协调并安全获取其提交，不直接推送覆盖。

同一来源分支的新提交会更新原PR，无需重新创建。回复每条意见，说明修改点及测试证据；无法采纳时解释原因，不擅自标记讨论已解决或代替审查者批准。新增实质改动需重新审查，是否自动撤销旧批准取决于实际保护设置。

### 4.8 负责人合并后：同步本地并开始下一任务

协作者和其Codex应停在“等待负责人审查／合并”。负责人核对第11节后按团队约定执行 **Squash and merge**；该功能需在仓库中允许。[合并策略说明](https://docs.github.com/en/pull-requests/reference/pull-request-merges)

确认GitHub上PR状态为 **Merged**，不要把 **Closed** 或已批准误当成已合并。之后在工作区干净的情况下同步：

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
git log -5 --oneline
git status -sb
git switch -c codex/next-task
```

新分支从更新后的main创建，不从上次已Squash的旧任务分支创建。Squash后的main提交ID通常不同于原任务分支最后一个提交ID，不能用二者必须相等判断PR是否合并。

分支删除是独立的清理动作：确认PR已合并、没有独有未保存工作、无人继续依赖后，由分支负责人按权限清理。不要自动执行 `branch -D` 或远端删除。`git branch -d` 在Squash场景可能拒绝删除，这是需要核验的保护信号，不是要求改用强制删除。

### 4.9 无主仓库写权限：Fork完整流程

**第一步：创建自己的Fork。** 在GitHub主仓库点击 **Fork**，核对所属账号和名称，等待完成。Codex不能因推送权限不足就自行创建Fork，必须有用户明确授权。

**第二步：克隆自己的Fork，并添加主仓库为upstream。** 以下变量必须替换为实际Fork账号；不要把 `YOUR_GITHUB_USERNAME` 原样执行，也不要在URL中附带token。

```powershell
$developerAccount = 'YOUR_GITHUB_USERNAME'
git clone "https://github.com/$developerAccount/USRA_UP.git"
cd USRA_UP
git remote add upstream https://github.com/1472879139/USRA_UP.git
git remote -v
```

预期：`origin` 指向开发者自己的Fork，`upstream` 指向 `1472879139/USRA_UP`。已有origin或upstream时先核验地址，不重复添加，不盲目执行 `remote set-url`。按第4.2节准备环境和本地配置。

**第三步：同步主仓库main，并从它创建任务分支。** 工作区必须干净，Fork本地main不应承载独有开发提交：

```powershell
git fetch upstream
git switch main
git merge --ff-only upstream/main
git switch -c codex/manager-context
```

Fork中尚无本地main时，先检查分支清单和实际默认分支，再在确认没有同名本地分支时使用 `git switch -c main --track upstream/main`。出现分叉时停止协调，不强制重置自己的Fork历史。

**第四步：开发、自测、提交，再上传到自己的Fork。** 按第4.4节操作后执行：

```powershell
git push -u origin codex/manager-context
```

上传前同步的主仓库基线应使用 `upstream/main`，即将第4.5节的 `fetch origin`、`origin/main` 分别替换为 `fetch upstream`、`upstream/main`；实际推送仍然是 `origin`，不是 `upstream`。

**第五步：创建跨仓库PR。** 在主仓库选择 **New pull request → compare across forks**，检查：

| PR项目 | 必须选择 |
|---|---|
| base repository | `1472879139/USRA_UP` |
| base branch | `main` |
| head repository／head fork | 开发者自己的Fork |
| compare branch | `codex/manager-context` |

按第10节填写说明，请负责人审查。若使用CLI，用户授权后可指定 `--repo 1472879139/USRA_UP --base main --head 开发者账号:codex/manager-context`。不要把PR建成“合并到自己的Fork”。[GitHub Fork PR说明](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request-from-a-fork)

“Allow edits from maintainers”等选项会授予额外协作能力，可能涉及Fork工作流权限，不由Codex默认开启；由开发者核对影响后决定。

**第六步：修订与合并后同步。** 审查修改继续提交并推送到同一个Fork任务分支；主仓库PR随之更新。负责人合并后执行：

```powershell
git fetch upstream
git switch main
git merge --ff-only upstream/main
# 可选：经授权后，将已核验的main同步回自己的Fork，不是主仓库
git push origin main
git switch -c codex/next-task
```

此处向 `origin main` 推送只允许在已经核实origin是自己的Fork、且只是同步主仓库既有提交的前提下执行；不是允许向主仓库main推送开发改动。无需更新远端Fork main时可省略该push。[Fork同步说明](https://docs.github.com/en/pull-requests/how-tos/work-with-forks/syncing-a-fork)

### 4.10 常见失败：在哪一步停止

| 现象 | 应做什么 | 禁止做什么 |
|---|---|---|
| `Authentication failed`／登录失败 | 检查当前登录和认证工具，请账号本人完成登录 | 把token写入命令、URL、文档或聊天 |
| `403`／无推送权限 | 确认账号、仓库地址和权限，必要时经授权走Fork流程 | 试推main、擅自提权、偷偷换账号 |
| `non-fast-forward`／远端领先 | fetch并检查该任务分支的他人提交，协调后合入并重测 | 直接force push覆盖远端 |
| `pull --ff-only`失败 | 核查本地main独有提交和远端变化，保留现场 | reset --hard或改用危险命令“同步” |
| 推送成功却看不到main变化 | 检查远端任务分支，创建／查看PR状态 | 把同样提交再推到main |
| PR出现无关历史／别人的改动 | 检查基线和是否复用了已合并分支，联系维护者 | 为缩小diff擅自改写他人的历史 |
| 无法合并／审批不足／CI失败 | 处理具体检查或等待负责人，保留真实失败信息 | 修改保护规则、伪造批准、关闭检查 |
| 网络中断，不确定push／PR是否成功 | 先读取远端分支或已有PR核验结果 | 不检查就重复创建PR或宣称已完成 |

## 5. 减少多人改动冲突

### 5.1 模块边界与协调重点

| 模块 | 主要范围 | 并行开发要求 |
|---|---|---|
| `agents/`、`prompts/` | 智能体行为、提示词与交接 | 涉及同一角色时同步评审代码与提示词 |
| `core/` | 消息、路由、状态机、Session | 先约定接口；合并后下游再适配 |
| `gui/app.py`、`gui/templates/` | 后端交互与前端展示 | 字段、轮询与终止状态需成套验证 |
| `tools/` | 数据读取、计算、制图、结果核验 | 保持输入输出契约，提供小型数据测试 |
| `tests/` | Python与界面回归 | 新测试随行为变更一并提交 |
| 环境与配置清单 | `environment.yml`、`requirements.txt`、配置示例 | 集中协调依赖变更，避免多人同时升级 |

以上是协作边界，不意味着文件归某人永久独占。不要为避免冲突而复制出多份Manager、Session或相同工具实现。

### 5.2 高频共享文件

修改 `core/session.py`、`core/message.py`、`core/state_machine.py`、`gui/app.py`、`main.py`、`agents/base_agent.py` 或依赖清单前，在Issue/PR中说明改动位置，通知正在依赖它们的成员。

- 尽量拆分小函数和独立模块，不为小功能重排整个文件。
- 逻辑变更与大面积格式化分开提交、分开PR。
- 每天开始和提交PR前获取最新主分支；分支尽量短期存在。
- 两人修改同一函数时先协调顺序，不能依靠“后合并的人自行处理”解决语义冲突。
- 存在依赖的PR应明确合并顺序。不要把别人的未合并分支悄悄当成公共基础；必要时先合并最小兼容接口。

## 6. 项目专属接口与行为约束

### 6.1 消息与会话

- 智能体交互沿用 `core/message.py` 的 `Message` 与 `MessageType`，不能另造并行消息协议。
- `user_request`、有序 `revision_feedback`、`manager_confirmation`、`confirmed_task`、`data_context`、`result_audit` 等会话字段的语义变更必须说明所有读写方及兼容方案。
- `approve` 是控制指令，不得替代分析需求。用户在确认阶段修订时保留原始需求，未变更条件继续有效，明确冲突以较新的修改为准。
- 数据预检事实应在Manager、Scientist、Engineer间保持一致；文件改变时不得继续使用过期批准与旧数据快照。
- 状态机、运行标记、轮询游标、会话消息重放、终止与错误状态必须联动验证，不能只改界面标签掩盖后端状态问题。
- 手动确认、CLI、GUI及一键托管路径可能共享逻辑。未明确要求更改的路径应保留行为，并检查是否受到间接影响。

### 6.2 产物与遥感解释

- 执行代码通过 `URSA_RESULTS_DIR` 获取任务输出目录。最终图、栅格、统计表、报告及中间文件统一放入该目录；重试复用，新任务隔离。
- 不硬编码开发者机器盘符或绝对目录，不将旧任务产物充当本次结果。
- 波段依据实际描述映射，不把B8误当成任何文件的第8个波段；运算前处理dtype、无效值和分母为零等问题。
- 面积单位、NoData、统计分母、阈值边界、坐标系必须明确；栅格、CSV、图例与报告应一致。
- 缺少热红外、第二时相、样本或行政边界时，如实说明限制。没有独立验证证据不能编造OA、F1或可靠地物语义。
- 保留数值核验与“语义待复核”的区分，不能通过删除检查、放宽到无效条件或强行补类别来让测试变绿。

### 6.3 共享接口变更说明

涉及接口的PR必须列出：旧字段／新字段、数据类型、含义、默认值、生产者、消费者、序列化影响及测试。优先增加兼容字段或适配层，再另开PR移除旧接口；不能仅升级一端。

模型、接口地址、提示词模板或重试策略的变化，应说明调用成本、超时、上下文长度、截断及失败状态的影响；不能把测试绕过当成稳定性修复。

## 7. 代码与环境规范

- Python基线为 `environment.yml` 声明的3.10；缩进4空格，文件使用UTF-8，沿用现有模块风格。
- 模块、函数、变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。共享函数新增或修改时补充必要的类型、参数、返回值和异常说明。
- 只引入任务需要的改动。避免无关重命名、自动格式化整个仓库、替换框架或修改未授权的功能路径。
- 不吞掉会导致状态不一致的异常；错误信息应有阶段、原因和可定位信息，但不能泄漏密钥或敏感原始内容。
- 不执行导入模块就发起付费模型调用的新增逻辑；耗时或有副作用的动作应有明确入口。
- 本项目尚未统一启用全仓库自动格式化／静态检查工具。引入时单独提维护PR，确定版本和规则，不能个人格式化后要求他人被动适配。
- 新环境以 `environment.yml` 为准：先Conda编译依赖，再pip层依赖。不要以旧说明中的简化pip命令替代此安装顺序，也不要随意用pip覆盖GDAL/Rasterio。
- 新增、升级或删除依赖须同步相关清单，说明必要性、版本约束、Windows兼容性和验证步骤；禁止提交本机整份无筛选的 `pip freeze` 代替维护依赖。
- 修改公共配置键时更新 `config.example.json`、默认值处理及文档；不能要求协作者提交真实 `config.json`。

## 8. 测试要求

功能或代码变更合并前，在项目根目录运行并记录真实结果：

```powershell
python -X utf8 -m unittest discover -s tests -q
node tests/test_gui_polling.js
git diff --check
```

测试数量会变化，不把当前数量硬编码为合并标准。纯文档PR可免跑运行测试，但需检查内容准确性、Markdown格式和链接，并注明“未运行：仅文档变更”。

| 变更类型 | 必须补充或核对的测试 |
|---|---|
| Manager／交接 | 原始需求保留、多轮修订优先级、确认指令、下游接收内容 |
| Scientist／Engineer提示词 | 显式约束与禁止项保留、失败响应、输出截断、代码输出契约 |
| 数据预检／工具 | 临时小栅格、波段映射、掩膜、文件变化、数值与单位 |
| Executor／产物 | 任务隔离、重试目录、覆盖文件追踪、越出目录、错误退出 |
| Supervisor／核验 | 缺失与损坏产物、统计矛盾、跨任务引用、待复核状态 |
| GUI／状态机 | 重复确认、页面刷新重放、忙碌与失败状态、长文本展开收起 |
| 依赖清单 | 干净环境安装、关键库导入、完整离线回归 |

- 缺陷修复应先写能复现问题的回归测试，再修复；不能只增加总能通过的断言。
- 使用临时目录与小型合成栅格，不依赖某成员私有影像或模型余额。测试不得覆盖 `data/`、`results/` 中真实文件。
- 默认离线：模型调用使用模拟响应，禁止把真实API密钥写入测试或在CI中默认付费调用。
- 真实模型联调与完整遥感案例需单独批准，记录模型、参数、输入版本、耗时、重试与成本；必要时做多次运行，不能用一次成功证明普遍稳定。
- 提示词无法仅凭模拟测试证明模型理解正确，需结合[遥感测试案例](docs/remote-sensing-test-cases.md)做行为验收。

## 9. 冲突解决与安全同步

默认在任务分支合入最新 `main`，不改写协作者已经共享的历史。同步前确认工作区干净，并已保存当前任务提交：

```powershell
git status --short
git fetch origin
# 确认当前确实位于自己的任务分支，再执行
git merge origin/main
```

出现冲突后：

1. 用 `git status` 查看冲突文件，联系相关改动作者，确认两边需求和最终语义。
2. 编辑冲突块，不能机械保留“我的版本”或“对方版本”，尤其是状态机、依赖清单和提示词。
3. 检查没有遗留冲突标记，复核合并后的完整函数、调用方与测试；“没有文本冲突”不代表没有逻辑冲突。
4. 对明确解决的文件逐个 `git add -- <文件>`，完成合并后运行完整测试并推送任务分支。最终测试后若再次改动，需重新验证。
5. 无法确认时暂停合并并协调，不强行猜测。需要放弃本次合并时，在核对现场后使用 `git merge --abort`，不要用破坏性重置替代。

禁止向 `main` 或共享分支强制推送；禁止 `git reset --hard`、`git clean -fd`、整目录复制覆盖等方式“修复冲突”。个人分支如确需rebase并改写远程历史，必须先协调、确认没有其他人依赖并使用有租约保护的方式；不作为默认流程。

## 10. PR说明模板

提交PR时复制并填写以下内容。本文包含模板文字，但未自动安装GitHub PR模板。

```markdown
## 关联任务
- Issue：
- 本PR目标：
- 明确不包含：

## 改动说明
- 修改的模块与原因：
- 共享接口／会话／提示词／配置／依赖变化：无／具体说明
- 对GUI、CLI、一键托管及旧行为的影响：

## 验证证据
- 复现步骤或验收方法：
- 新增／更新的测试：
- 执行命令、结果、运行环境及测试对应提交：
- 未验证项及原因：
- 界面变化截图：无／脱敏截图
- 真实模型联调：未执行／批准范围与结果

## 风险与回滚
- 已知风险、性能／成本影响：
- 是否兼容旧接口与配置：
- 回滚方法及是否存在外部副作用：

## 自检
- [ ] 本PR只处理一个逻辑任务，无无关格式化或临时调试文件
- [ ] 已与共享文件及接口相关成员协调
- [ ] 新增回归测试，必要测试通过；未执行项已明确说明
- [ ] 未提交密钥、真实配置、影像、产物、对话、缓存或环境目录
- [ ] 已更新必要文档、配置示例和依赖清单
- [ ] 无未解决冲突，审查后新改动已重新验证
```

## 11. 审查与合并门槛

维护者合并前必须确认：

- 目标分支、任务范围、验收标准清楚，协作者PR已取得负责人 `1472879139` 正式批准；负责人自身PR按已明确的非作者审查／维护例外处理，不能伪造自批。
- 共享接口及关联模块已同步，当前主分支的相关变化已纳入验证，没有未解决的审查问题。
- 对实际待合并内容的测试证据有效，不拿旧截图或另一提交的结果替代。
- 新增失败路径有覆盖，既有测试没有被无理由删除或弱化。
- 无凭据、私有数据、许可证或来源不清的复制代码；遵守上游授权与署名要求。
- 影响需求传递、执行安全、结果可信度、依赖栈等高风险变更有专项证据；团队人数允许时增加第二名审查者。
- PR标题适合作为Squash后的主分支提交说明，回滚范围明确。

合并后维护者做一次不触发付费请求的关键路径检查；需要真实模型冒烟测试时先确认费用权限。出现回归时优先通过新的回滚PR撤销对应主分支提交，保留历史，禁止通过重置或强制推送倒退共享分支。

## 12. GitHub管理设置建议（需维护者另行实施）

本文不修改仓库权限、分支保护或自动化配置。建议维护者后续评估并设置：

1. 保护 `main`，要求PR合并、至少一名非作者批准、解决审查讨论，禁止强制推送及删除主分支；新实质改动需要重新审查。管理员也遵守同一流程。GitHub可通过分支保护设置审查和状态检查要求。[官方分支保护说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
2. 配置与本地环境兼容的离线CI，覆盖Python及界面测试。先验证工作流稳定运行，再将实际存在且名称明确的任务设为必需状态检查；不能要求一个从未运行的检查阻塞全部PR。
3. 如需强制协作者变更经负责人批准，应在main配置CODEOWNERS指定 `1472879139`，并启用代码负责人审查要求；仅要求“1名批准”不等于指定负责人批准。注意作者不能自批，负责人自身PR及管理员例外须先约定，不要配置出无人能满足的审批条件。不填写虚构账号，不把自动分配审查等同于批准。
4. 允许并默认使用Squash合并，避免团队成员混用合并策略。复杂发布分支如需其他策略，先由维护者约定。
5. 对外部贡献先做安全审查，不在不受信任PR中暴露密钥或执行带权限的任意代码；CI默认不配置付费模型密钥。

具体可用设置受仓库类型、权限与GitHub套餐影响，以官方文档及实际设置为准。团队约定即使尚未自动强制执行，也应遵守。

## 13. 相关文档与规范维护

- [运行与环境说明](README.md)
- [数据预检和结果核验设计](docs/data-aware-analysis-design.md)
- [任务产物目录约定](docs/task-results.md)
- [遥感任务测试案例](docs/remote-sensing-test-cases.md)
- [上传排除与敏感文件检查](docs/github-upload.md)

本规范变更同样通过文档PR讨论和审查。未约定的重大架构、共享协议、框架替换或数据策略，不由单个开发者直接决定；先形成设计说明并由维护者确认。发现旧说明与当前环境清单或运行代码矛盾时，应提出修正文档的PR，不静默选择对自己方便的解释。

## 14. Codex执行约定与可复制任务说明

### 14.1 如何让协作者的Codex读取规范

根目录的 [AGENTS.md](AGENTS.md) 是简短项目指引入口，要求代码智能体继续读取本规范；详细流程不在两个文件中重复维护。OpenAI官方说明了Codex对AGENTS.md的发现和加载方式，具体有效指引也可能受到全局指引、目录覆盖和配置影响。[OpenAI官方说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

协作者应先将包含这些文件的版本同步到自己的本地，再从该项目根目录开始新的Codex任务。已有会话可显式要求重新读取文件；不要因为网页上已有文档，就认为本机旧副本和旧会话也已读取。

每次首次接入可发送：

> 请读取当前仓库根目录的AGENTS.md与CONTRIBUTING.md，确认项目根目录、分支、远程地址及未提交改动。先说明本任务准备采用的协作方式、授权边界和测试方法。文档中的示例命令不是要求你现在全部执行。

不要求协作者安装项目作者个人目录中的技能，不引用他人机器的绝对路径，也不修改协作者全局Codex配置来“强制生效”。项目文档能引导行为，但不能替代GitHub权限与分支保护。

### 14.2 开始工作前的检查

Codex在修改前按任务需要检查并简要报告：

1. 仓库根目录是否为本项目，Git状态和当前分支是什么；是否存在他人改动、不同任务或detached HEAD。
2. origin／upstream的实际地址，当前采用直接协作还是Fork；不得根据文件夹名或远程别名猜测。
3. 用户要求是解释、设计、本地实现、提交、推送、创建PR还是合并；操作范围以本轮明确授权及仍有效的任务授权为准。
4. 有无需先协调的共享接口、环境变动和数据安全风险。
5. 相关测试、依赖与数据是否可用，是否会触发真实模型请求及费用。

存在权限、网络或未提交改动问题时，继续完成安全且不受影响的本地工作；需要扩大授权或可能破坏他人工作时停止并说明，不通过忽略规则、换账号、关闭保护来绕过。

### 14.3 本地实现、发布与审批的授权边界

| 用户明确要求 | Codex可按范围执行 | 不自动授权 |
|---|---|---|
| 解释、检查、评审、给方案 | 相关只读检查、报告建议 | 实施代码、提交或远端写入 |
| 修复／开发／完善代码或文档 | 任务范围内的本地变更、安全验证、必要的任务分支准备 | push、创建Fork、创建PR、审批、合并、修改权限 |
| 提交本地Git | 核对实际差异后，仅提交本任务文件 | 推送到远端、捎带无关改动 |
| 推送指定任务分支 | 核验目标、授权和待上传内容后push并读取远端核验 | 创建PR、推送main、推送其他分支或标签 |
| 为本任务创建／更新PR | 为该PR正常推送正确来源分支、创建或更新对应PR | 创建其他任务的PR、自动Fork、批准或合并 |
| 要求合并 | 还须确认请求者具备维护授权、目标PR和负责人审批条件 | 冒充负责人、替他人批准、绕过检查、修改保护规则 |

“提交”未说明是本地commit还是上传GitHub时，应结合上下文明确含义；若不能判断，先询问，不把它自动解释成推送main。有效授权覆盖的常规步骤不必反复询问，但不能把另一任务的旧授权扩展到当前任务。

协作者的Codex默认以PR准备完成或已授权创建PR作为交付终点，等待负责人 `1472879139` 审查和执行合并。即使登录凭据恰好具备管理权限，也不构成使用这些权限的授权。模型不得自动批准自己或同伴的PR，也不能为方便合并关闭CODEOWNERS、CI或保护规则。

### 14.4 可复制的任务指令

以下是给开发者Codex的任务模板，不是现在执行的命令。替换任务内容、范围、验收标准及分支名后使用。

**模板A：只做本地开发，暂不上传。**

```text
先完整阅读AGENTS.md和CONTRIBUTING.md。
任务：修复Manager在多轮修改中遗漏未变更需求的问题。
范围：相关Manager交接逻辑和回归测试；不更换框架，不修改一键托管。
验收：保留原需求；新修订覆盖明确冲突；Scientist接收完整已确认上下文。
请先检查分支、远程和脏工作区，从已核验的最新主仓库main创建任务分支
codex/manager-context；不要覆盖用户或其他任务的改动。
只做本地实现和离线测试，不commit、不push、不创建PR、不调用付费模型。
完成后报告改动、测试、未验证项及后续待办。
```

分支名和缺陷只是示例，不表示该缺陷现在仍存在。同步需要网络而当前不可用时，应明确基线未刷新，不声称使用了最新main。

**模板B：验证后提交并上传任务分支，但不创建PR。**

```text
本次授权只针对当前已完成任务的文件。
请检查AGENTS.md和CONTRIBUTING.md，确认测试通过、差异仅属于本任务，
且没有密钥、真实配置、影像、产物或用户其他改动。
允许提交本地Git，并推送当前任务分支codex/manager-context到已核验的origin。
不要推送main、其他分支或标签；不要创建PR、Fork、修改权限、审批或合并。
请核对远端任务分支提交ID与本地一致，报告实际提交与上传结果。
```

**模板C：为已有任务创建PR，等待负责人合并。**

```text
请按AGENTS.md和CONTRIBUTING.md为当前任务创建PR。
目标仓库：1472879139/USRA_UP；目标分支：main。
来源：当前已核验的codex/manager-context任务分支；先检查该分支是否已有PR。
允许提交本任务文件，并为此PR推送正确来源分支；若没有主仓库写权限，
先说明是否已有个人Fork及远程配置，未获授权不要自行创建Fork。
填写完整改动、测试、风险与未验证项，请负责人1472879139审查。
禁止代替负责人批准、合并、开启自动合并或绕过保护规则。
完成后提供PR链接；如果因权限或网络失败，如实说明停在哪一步。
```

**模板D：处理已有PR的审查意见。**

```text
请先读取AGENTS.md、CONTRIBUTING.md和指定PR的审查意见。
PR链接：由我提供真实链接。
仅处理本PR范围内意见，保留原始验收约束；不能理解或与需求冲突的意见先说明。
允许在原来源分支追加本地提交并推送以更新同一个PR，不新建重复PR。
重新运行相关测试并更新验证说明；不要伪造批准、替审查者解决争议或执行合并。
```

### 14.5 完成后的交付格式

Codex最后应以真实证据说明：

```text
任务：具体目标与实际完成范围
分支与远程：当前本地分支、实际来源仓库及目标main
改动：主要文件、行为及接口影响
验证：命令、真实结果、未执行项及原因
本地提交：未提交／实际提交ID
远端推送：未推送／实际分支及已核验提交ID
PR：未创建／真实URL及当前状态
审批与合并：未执行，等待负责人／经核验的实际状态
风险与待办：权限、数据、费用、兼容性或待复核事项
```

没有执行的动作写“未执行”，不能把“代码已写好”说成“GitHub已更新”，不能把push成功说成“已合并”。远端操作失败后先读取核验，避免重复创建外部对象。
