# DeepSeek工具调用：本批接口与实现范围

> 当前自主入口见[FIX-025使用说明](autonomous-agent.md)：`--autonomous-agent --port 7864` 允许直接回答、专用工具和隔离Python组合。下文记录旧 `--tool-agent` 的12工具路径及当时边界；其中“不提供任意Python”和必须确认的约束不适用于新自主入口。

本批在独立codex/deepseek-tool-agent分支开发，基于当前未提交的能力整合版源码快照；不修改原7862工作区、配置、数据和结果，不拉取远端或发布。用户已授权本地实现，未授权本轮付费联调。

新增路径：Manager确认目标和输入范围→Scientist锁定本次请求→Engineer转交受控工具任务→Executor运行DeepSeek工具调用循环→Supervisor重新核验→Manager通俗交付。原Message/MessageType、状态机与原生成代码路径保留。新增Session.tool_agent_options、tool_agent_plan，序列化保留选项/计划，不包含认证信息；GUI与CLI从同一启动开关启用。API服务、模型名、密钥继续来自原配置，不写入任务产物。

模型选择工具；程序只执行白名单、校验参数与数据条件。锁定输入和元数据、固定模型/算法、独立任务目录、有限调用预算、真实错误回传、当前产物引用和独立核验共同约束执行。不提供任意Python、Shell、安装、下载或参考答案读取工具。

第一批工具：inspect_data、list_capabilities、extract_water、classify_landcover、retrieve_temperature、compute_index、select_classes、calculate_area、summarize_raster、render_map、validate_artifacts、finish_task。稳定工具库为先，旧registry底层接口保持兼容，不直接把任意函数或任意路径交给模型。

finish_task必须使用当前已核验的产物ID；执行完成仍交付候选结果，无法满足的内容以partial/blocked说明。缺少答案时不声称语义精度。API错误不自动换模型或退回任意代码，不进行不可见计费重试。

验证范围：模拟DeepSeek结构化响应、真实小型栅格、模拟GPU与完整角色/GUI/CLI交接。真实付费兼容性和真实GPU组合任务本批不默认执行。每次执行均进入同一URSA_RESULTS_DIR；有限重规划不能改变原输入或核验规则。


## 启动与一句话测试

使用已有ursa-up环境，不重装依赖，不更换API服务或模型名，不更新GPU服务包。默认启动仍保留原处理路径；本批通过显式开关启用新工具Agent：

```powershell
python main.py --tool-agent --port 7863
# 命令行也使用同一路径
python main.py --tool-agent --cli
```

也可在自己的config.json中添加与config.example.json一致的tool_agent设置；默认enabled=false。本机已复用原私有配置，没有打印或改写其中的认证信息。GUI新模式启动/刷新目录只显示静态工具提示，不调用智能建议模型；选择输入并确认之后才开始DeepSeek请求。仍保留一次目标和数据范围确认，approve不会成为分析需求。

先保持原22022隧道和18766的TerraMind服务窗口。打开7863，在顶部选择terramind_demo_01/image.tif，输入：

> 找出这张影像中的农田，给我候选分布图和平方米面积。

DeepSeek应依据数据与工具返回自行选择分类、类别筛选、面积与制图；顺序和调用数量由真实响应决定，不按上述句子关键词绑定模型。若不能满足某项要求，应明确partial/blocked。这个样本是已测旧样本，仅作接入回归。

还可以选择water_01六波段输入问“提取水体并给出占比”；选择lst_01物理辅助量问“看看哪里比较热”；选择Sentinel-2数据问“计算NDVI，给我分布图和数值统计”。不符合输入条件的工具返回结构化错误，DeepSeek在同一已批准目标内决定下一步。

用于检验缺能力行为的提示词：

> 帮我把这张影像去云。

当前没有注册去云工具，应说明未完成并停止，不用临时Python或普通分类冒充去云。复杂需求是否被完全理解仍依赖模型，程序核验无法证明自然语言任务全部满足。

## 注册的12个工具

| 工具名 | 作用和边界 |
|---|---|
| inspect_data | 仅读取本次批准输入的波段、尺寸、坐标等摘要 |
| list_capabilities | 列工具对每个输入的适配情况；远程连通性执行时核对 |
| extract_water | DeepSeek选择mndwi或prithvi，沿用原固定方法与数据契约 |
| classify_landcover | 调用现有TerraMind Small生成式候选分类 |
| retrieve_temperature | 调用既有Landsat 8物理量温度工具 |
| compute_index | 按实际波段描述计算NDVI/EVI/NDWI/MNDWI/NBR/MSAVI，当前仅适配声明来源的S2 DN/10000输入 |
| select_classes | 按已定义的类别编号筛选，保留未判定范围，生成候选掩膜 |
| calculate_area | 根据真实米制投影网格计算平面候选面积，拒绝用经纬度平方代替平方米 |
| summarize_raster | 重算实际栅格统计并输出CSV，分类占比不是置信度 |
| render_map | 为本次栅格制图，保留中文说明和图例 |
| validate_artifacts | 检查全部当前产物、原始GPU返回包、哈希及统计 |
| finish_task | 凭实际产物ID与当前核验结束；completed、partial、blocked分别记录 |

未把旧registry中的任意路径读写、原简化温度计算或任意函数直接暴露给模型。旧接口继续供原路径使用，新路径从同一registry的get_agent_tool_schemas获得上述白名单。不提供任意代码、Shell、安装、下载、训练或参考答案工具。影像在本地/GPU处理，DeepSeek接收元数据、数值摘要和产物引用；本批没有加入视觉图像输入。

## 循环、错误和预算

API使用原OpenAI兼容客户端的结构化tools请求，不改原BaseAgent文字调用。程序保留assistant.tool_calls、每个tool_call_id与role=tool返回；收到reasoning_content时仅保留在本次API上下文，支持需要回传该字段的接口，不写入用户过程日志。finish_reason截断、非法调用ID、重复ID、重复JSON字段、非有限参数和越界参数会被拒绝。服务不支持tools或返回异常时明确停止，不偷偷切换服务/模型或退回生成代码。

默认最多12轮请求、24次工具调用、300秒循环预算、每次最多4096输出token；每个API等待上限为60秒或剩余预算。阻塞的单个GPU/本地工具受自身条件和超时限制，无法保证在循环预算瞬间中断；用户停止后不会继续后续调用或交付成功。实际API用量按服务返回记录，requests计请求尝试次数，缺失的token用量不能作为零费用证明。当前不是精确金额预算器。

工具报错可由DeepSeek重新规划，但同一参数下已成功的昂贵推理或失败请求均复用本次记录，避免反复请求。输入、元数据、GPU设置或已登记产物发生变化直接停止，不能靠换图、改算法或旧任务产物继续。外层旧代码修复循环不会重新启动整套工具Agent，避免放大调用次数。

本批默认需要一张可查看的结果图才能交付completed/partial；仅CSV等特殊输出偏好尚未完整适配。工具判断仅覆盖已实现的物理与格式条件，不能证明分类精度、地理产品来源声明绝对真实，或复杂自然语言需求全部满足。模型补充说明保存在专业内容中，主结果优先展示实际工具facts。参考答案继续在交付封存后由外部评价端读取，未把外部评分接回本次规划。

## 共享接口与文件变化

| 文件/接口 | 变化 |
|---|---|
| core/tool_call_loop.py | 新增有限结构化工具循环、调用结果回传、预算与停止 |
| tools/agent_tools.py | 新增12个白名单封装、只允许本次输入/产物ID、图表统计与原始GPU包核验 |
| tools/tool_agent_workflow.py | 新增原角色链适配、输入锁定、执行收据、Supervisor复核及通俗交付 |
| tools/registry.py | 兼容新增get_agent_tool_schemas，原19个底层工具接口保持 |
| prompts/tool_agent_prompt.txt | 新增目标保留、真实反馈、有限重规划和不读取答案约束 |
| Session.tool_agent_options | dict，默认{}；GUI/CLI写入运行开关与限额，Scientist/Executor读取；序列化保留 |
| Session.tool_agent_plan | dict，默认{}；Scientist写入原需求/修订/输入SHA/元数据SHA/配置SHA与预算，Engineer/Executor/Supervisor读取；不含密钥 |
| Session.tool_agent_receipt | dict，默认{}；Executor写入manifest_sha256，Supervisor消费并复核；序列化保留 |
| Message.metadata.tool_agent_request | dict，仅新路径Scientist→Engineer→Executor传递，必须等于锁定计划；旧消息和枚举不改 |
| agents各角色、core/execution_cycle.py | 增加新路径分支，默认旧路径保留；新循环失败不启动旧修复再计费 |
| GUI、main.py、config.example.json | 新增--tool-agent与配置选项，GUI和CLI共用；页面布局、轮询和前端字段不改 |
| tools/task_contract.py | 新路径不按旧关键词决定模型或温度工具，保留原文与有序修订，由工具自身核验条件 |
| tools/result_delivery.py | 新增受控工具结果适配，候选状态、未完成项、原始专业说明和实际图名 |
| tests/test_tool_agent.py | 31项新离线验收，真实小栅格、回环模拟GPU、GUI与CLI完整角色链 |

每次任务仍写执行器确定的results/task_...目录；新路径在执行器进程内受控调用，不临时改全局URSA_RESULTS_DIR。所有工具显式接收同一任务输出目录，其含义与既有约定一致。模型不能指定目录或任意路径。task内有旧内容时保留现场并拒绝覆盖；错误过程也写tool_agent_calls.json和tool_agent_manifest.json。

## 本批验证与发布状态

- 先写验收测试，确认旧代码缺少工具调用模块；实现后31项专项测试通过。
- 当前最终全量Python319项通过（58.527秒）；Node界面24项通过；git diff --check通过。都是本批当前代码真实运行结果，调用的AI和GPU为模拟。
- 覆盖本地水体/LST、两个模拟GPU服务、分类→农田筛选→面积→图示组合、错误后重规划、重复推理复用、未判定值保留、非法路径/参数/ID、截断、预算、取消、缺能力停止和默认旧路径。
- 真实DeepSeek tools兼容性、真实API组合行为、真实GPU组合任务和新影像精度：未执行。API官方能力不等于用户当前代理服务必然兼容，首次手工测试可能明确报不兼容。
- 分支codex/deepseek-tool-agent；基线21151579da66cc804641041f80289ebb0fbbf237及105份能力整合源码快照。未fetch，不称为最新main。未提交、未推送、未创建PR、未批准、未合并。
- [FIX-022图文记录](fixes/2026-10-28-022-deepseek-tool-agent.html)。测试与图文材料均使用合成数据，不引用真实测试分数证明本批工具选择效果。

官方协议参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[工具调用与推理字段回传](https://api-docs.deepseek.com/guides/thinking_mode/)。
