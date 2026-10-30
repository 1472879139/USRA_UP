# DeepSeek实际调用状态与真实组合任务复核

> 当前显示已由[FIX-024：整行状态条与累计次数](deepseek-activity-visibility.md)更新。以下保留FIX-023当时的验证快照。

本批在codex/deepseek-tool-agent分支延续同一工具Agent任务。保留已有未提交修改，原API配置、模型、预算、工具算法、输入、任务结果和界面管线位置不改。未提交、未推送、未创建PR、未批准、未合并。

## 用户这次真实运行

用户已手工执行一次真实DeepSeek＋4090D组合任务。记录显示6次模型请求、9次成功工具调用：inspect_data、list_capabilities、classify_landcover、select_classes、calculate_area、summarize_raster、render_map、validate_artifacts、finish_task。已检查实际结果、原始GPU返回包、输入与文件哈希，并独立重算类别和面积。

农作物候选10,284像元，占有效区域20.49585459%；每像元100平方米，平面候选面积1,028,400平方米，即1.0284平方公里。类别筛选与原分类的编号4逐像元一致，CSV面积与真实网格重算一致。分类输出与此前同一输入的Agent结果完全一致，本次证明自主工具组合成功，不代表识别精度提升或泛化稳定。

专业补充说明还声称“指数工具不适用于本输入”，但本次工具预检没有提供这项结论；当前S2十二波段包含NDVI所需波段，因此不能把该补充说明当作能力事实。此项属于报告事实约束的后续改进点，本批不更改原会话和产物。

本轮仅复核已有运行并实现状态标志，没有新增真实DeepSeek请求或真实GPU推理，没有重读参考答案评分。

## 网页行为

在原对话区标题旁增加DeepSeek状态标志：

- 实际API请求正在等待/接收响应：显示“DeepSeek正在使用”、转圈、角色对应阶段及已等待秒数。
- 执行本地计算或GPU工具、等待确认及结束：显示“DeepSeek未调用”，不把整个分析时段当作模型调用。
- 页面轮询断开：显示“DeepSeek状态待确认”，恢复连接后读取真实状态。
- 刷新页面：从会话快照恢复在途请求状态；新会话不继承旧标志。
- 接口报错、正常返回和流式响应结束：finally清除对应请求；多个请求不会相互误清除。
- 用户停止：立即停止该会话的等待动画。该状态表示会话已停止等待，不能保证已发给服务商的网络请求已被取消或免计费。

沿用两秒轮询，状态更新有轮询延迟；极短请求可能不会在两个快照之间被显示。标志表示请求生命周期，不是服务商内部思考进度、GPU占用或网络连通性保证。现有管线、顶部目录、对话过程和专业详情布局保留。

## 接口与文件

| 文件/字段 | 生产者、消费者及语义 |
|---|---|
| core/session.py | 新增begin_llm_call/end_llm_call/get_llm_activity，锁保护在途请求，按内部唯一标识清理 |
| Session.to_dict().llm_activity | 新增兼容字段dict，包含active(bool)、agent(str)、request_count(int)、elapsed_seconds(int)；默认inactive，不含请求文本/私钥/密钥/接口地址 |
| agents/base_agent.py | 原普通和流式API调用开始记录，在finally结束记录，包含异常路径 |
| core/tool_call_loop.py | 新增内部request_started/request_finished通知，严格包裹原SDK请求；不改变预算、工具选路或返回协议 |
| tools/tool_agent_workflow.py | 将上述内部通知连接到Session；不误写成“工具已返回”或重复计入工具日志 |
| gui/app.py | /api/updates新增llm_activity状态快照；页面刷新不依赖旧队列事件 |
| gui/templates/index.html | 在原对话标题处新增标志，文字通过textContent写入；完成/停止/新会话清理，断连显示未知；支持减少动画偏好 |
| tests/test_llm_activity.py | 9项后端回归：重叠请求、普通/流式、异常、实际线程等待、工具循环、会话终止、时间与序列化 |
| tests/test_gui_polling.js | 新增6项前端回归，总计30项；覆盖刷新、工具执行、结束、新会话、断连恢复、文本安全和旧响应兼容 |

本批未新增LLM请求、自动重试、费用、依赖或配置项；默认旧路径和新工具Agent路径都可标记各自会话内的实际调用。旧启动期独立智能建议发生在页面服务启动前，不由此标志表示。

## 验证

- 先写9项生命周期回归，旧代码全部失败；实现后9项专项全部通过。
- 最终Python全量328项通过（59.909秒）；Node界面30项通过；git diff --check通过。均为本批实际运行，AI响应与GPU为模拟。
- 页面桌面/手机状态切换、等待标志、断连和恢复另留浏览器核验材料，不将截图当作真实新模型调用证据。
- 用户真实任务仅只读复核，源码更改前已封存原结果并保存文件SHA；本次不重跑分析，也不替换任何原产物。
- 新标志真实付费调用期间的手工观察尚待用户重启后验证；不使用之前319项测试代替本批检查。

[FIX-023图文修改记录](fixes/2026-10-28-023-deepseek-activity.html)

## 生效方法

先保存需要的会话记录，停止本机7863窗口（Ctrl+C），在同一项目根目录重新运行：

```powershell
cmd /d /c 'call "D:\Anaconda3\Scripts\activate.bat" ursa-up && python main.py --tool-agent --port 7863'
```

打开http://127.0.0.1:7863并刷新页面。SSH隧道与GPU服务窗口保持，不需要重装环境、重新下载模型或为了显示标志重跑旧结果。新任务确认后按原API计费。
