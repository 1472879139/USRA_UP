# 修改记录：核验修复与普通用户结果交付

- [从GitHub源码到当前本地版本：累计图文总览](2026-10-23-source-to-current-overview.html)
- [完整累计修改记录与逐文件清单](../source-to-current-changelog.md)

FIX-004从已核验的GitHub主分支main（eabcb1488c2e32a8fdc4c22c82e2d85316a025e1）重新开始。本地先前的11个已修改文件与6个新增修复文件已撤回并另行备份，下载数据、现有结果、导出的测试资料和本地配置共133个文件在恢复时逐文件校验未变。此前FIX-001～003不属于这次重建后的源码。FIX-005继续记录普通用户结果交付的第一批实现。

- [FIX-004图文HTML幻灯片](2026-10-22-004-artifact-repair-loop.html)
- [HTML目录](index.html)
- [FIX-007：恢复右侧管线与顶部可折叠目录扫描](2026-10-23-007-sidebar-pipeline-layout.html)
- [FIX-006：统一结果界面，专业详情按需展开](2026-10-23-006-inline-professional-details.html)
- [FIX-005：任务约定、适用条件与普通用户结果交付](2026-10-23-005-plain-language-results.html)
- [FIX-005接口与范围说明](../plain-language-results.md)

以下为FIX-004的开发与验证快照。FIX-005的范围、共享接口、验证与限制，以及FIX-006、FIX-007的界面修订，请查看上方单独记录。旧记录保留当时快照，当前布局以FIX-007为准。

## 实现范围

- core/execution_cycle.py：GUI与CLI共用有限循环，失败停止、成功结果才进入Manager。
- tools/artifact_diagnosis.py：按当前核验错误分类，生成针对任务产物的修改建议。
- agents/supervisor_agent.py：在编排器明确声明可重试时返回产物修复请求；独立调用仍按原行为阻断失败。
- gui/app.py、main.py：统一调用共用循环；CLI补齐自身所用的Message与MessageType导入。
- tests/test_artifact_repair.py：16个新增离线测试，包含GUI、真实CLI入口和真实临时CSV/JPEG/执行子进程；模型响应被模拟。
- tools/result_audit.py、Scientist预算、真实config.json和环境未作修改。

## 共享消息契约

| 字段/事件 | 生产者 | 消费者与含义 |
|---|---|---|
| RESULT.metadata.allow_artifact_repair = true | 共用循环 | Supervisor消费并移除；只在受限编排中推迟可修复失败的终止 |
| ERROR.metadata.action = repair_artifacts | Supervisor | 共用循环路由给Engineer，重试前保留完整任务及上一版代码 |
| ERROR.metadata.diagnosis | Supervisor/diagnose_artifact_failure | repairable为布尔值，issues为category/error/guidance列表；错误文字只是定位数据，不是授权 |
| RESULT.metadata.files_generated | Executor原始产生，共用循环累计 | Supervisor及Manager使用同循环、同任务已生成文件并集；不能引用其他任务或隐藏此前损坏产物 |
| tool_call，tool_name = artifact_validation | 共用循环 | 沿用现有追踪与导出；数据含attempt/max_attempts/audit_status/errors/warnings/files |

Message与Session已有字段结构、状态机枚举、GUI事件type及前端字段不变。修复过程沿用“监督检查、Engineer编码、Executor执行”等阶段。核验失败不再提前进入不可恢复的TERMINATED；只有受控循环中的可修复错误会重试。跨任务目录问题仍直接阻断。每次实际执行计入同一个executor_max_retries，默认5次包含首次，额外修复调用最多4次。

## 独立核验与保留文件

核心读取器保持主分支原样，N/A解析兼容修复没有重新加入。对确实未知的可选面积，模拟Engineer修改任务CSV省略面积列并说明限制，重新核验得到needs_review；未把未知面积填0。数值与产物核验通过不能证明真实地物分类正确。

循环只累计执行器在本次运行中报告的实际生成文件，不从目录扫描导入旧任务的现成输出。修复只更新CSV时，原JPG仍参与后续检查。另起新CSV而遗留损坏旧CSV、或删掉已要求修复的必需统计字段，都不能得到成功报告。

“禁止修改宿主源码/核验器、读取参考答案、编造数值”的约束进入修复提示词，并继续使用现有执行检查。它们不等于操作系统级沙箱，也不保证模型一定遵守；本次没有新增任意源码自动更新能力。

## 实际验证

- 干净基线：79项Python测试通过。
- 先写5个回归测试：旧流程4项失败，明确复现核验错误直接终止、缺少修复回路。
- 实现后新增16项专项测试全部通过；包含真实临时CSV/JPEG及子进程，模拟AI响应。
- 最终Python全量95项通过；界面JavaScript13项通过；git diff --check通过。
- CLI入口真实调用已覆盖成功恢复与预算耗尽两条路径；无网络模型请求。
- HTML与保留数据的最终核验另记本地验证证据，不把页面检查当算法验证。
- 未执行真实DeepSeek调用、真实遥感新分析、服务重启或旧会话重跑。

本地证据保存在忽略目录results/artifact_repair_verification_20261022/。备份与恢复校验记录保存在独立的本地备份目录；交付消息提供入口，不纳入Git。图中流程与缩小表格均标为示意或离线模拟，不使用业务影像、完整对话或密钥。

## 生效与限制

保存现有会话后重启程序，再使用原来的一句话提示词发起新任务。原模型/API配置保持不变。界面遇到可修复核验失败时会显示“产物核验失败，Engineer自动诊断与修复中”，随后重新执行和核验。

没有专业模型、输入数据不足、规划输出截断或遥感方法本身不合理，不能靠此循环保证解决。需要继续用独立参考数据测评。若错误涉及核验器本身的逻辑，循环最多建议兼容的真实输出格式；不能让模型篡改检查规则来宣称成功。

分支codex/artifact-repair-loop。以上为开发验证快照，记录时尚未提交或发布；后续提交和推送状态以Git记录为准。PR、批准与合并须另行授权。
