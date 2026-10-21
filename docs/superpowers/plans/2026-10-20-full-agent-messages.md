# 智能体消息全文与折叠 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 智能体长回复完整传输与导出，在聊天面板中默认折叠并可逐条展开、收起。

**Architecture:** 后端保留 `Message.content` 全文，让现有可重放事件协议原样承载；前端只用高度裁剪已渲染的内容，不裁掉字符串。真实按钮控制每条气泡的展开状态，短消息无按钮。

**Tech Stack:** Python unittest、Flask、原生 HTML/CSS/JavaScript、Node `node:test`。

---

项目没有 `.git` 目录；不创建工作树或提交。使用现有工作目录，并保留用户的其他文件改动。禁止通过真实 LLM 验证。

### Task 1: 后端完整保留消息

**Files:**
- Modify: `tests/test_ui_updates.py`
- Modify: `gui/app.py:615`

- [x] **Step 1: 写失败测试。** 在 `ReplayableUpdatesTests` 中加入以下测试；`make_view()` 已为离线测试创建所需对象。

```python
    def test_long_agent_message_is_preserved_in_updates_and_histories(self):
        for sender in ("Manager", "Engineer", "Scientist", "SupervisorAgent"):
            with self.subTest(sender=sender):
                view = make_view()
                original = "分析方案\n" + "遥感数据" * 600
                view.ui_queue.put({"type": "message", "data": Message(
                    sender=sender, receiver="User", content=original,
                )})
                event = view.get_updates()["updates"][0]
                self.assertEqual(event["content"], original)
                self.assertEqual(view._raw_history[0]["content"], original)
                history = view._monitor_history if sender == "SupervisorAgent" else view._chat_history
                self.assertIn(original, str(history))
```

- [x] **Step 2: 运行并确认失败。** `D:\miniconda3\envs\ursa-up\python.exe -m unittest discover -s tests -p test_ui_updates.py -v`；预期 Manager/Engineer/Supervisor 子用例因 2000 字符截断失败。
- [x] **Step 3: 最小修复。** 将 `gui/app.py` 消息分支的 `content = msg.content if msg.sender in ("Scientist",) else msg.content[:2000]` 改为 `content = msg.content`，保留其余处理。
- [x] **Step 4: 再运行同一命令。** 预期全部通过；用断言确认可重放事件与原始历史均为全文。

### Task 2: 聊天气泡展开与收起

**Files:**
- Modify: `tests/test_gui_polling.js`
- Modify: `gui/templates/index.html:79-94,410-423,592-605`

- [x] **Step 1: 扩展测试页面的 DOM 模拟。** 让 `makeElement()` 的 `querySelector('.bubble-content')` 返回稳定的内容节点，允许测试设置 `scrollHeight=400`、`clientHeight=240`；保存插入的按钮节点、`onclick` 和 `aria-expanded`。测试只模拟浏览器测量与点击，不模拟折叠算法本身。
- [x] **Step 2: 写并运行失败测试。** 分别断言长 Agent 消息有“展开全文”按钮且初始 `aria-expanded=false`，点击后全文容器不再受 240px 限制、按钮变“收起”，再次点击恢复；短 Agent 消息和用户消息无按钮。运行 `node --test tests/test_gui_polling.js`，预期新用例因按钮不存在而失败。
- [x] **Step 3: 最小实现。** 在气泡内用 `.bubble-content` 包住完整 HTML；为 Agent 类型的消息测量 `scrollHeight`，超过 `240px` 时添加原生 `<button type="button">`。默认 `.collapsed` 限高 240px、`overflow:hidden`；按钮切换类名、文本、`aria-expanded`。`processUpdates()` 只对 `bot` 与 Supervisor 消息启用，使用现有 `formatContent()` 转义原文。按钮与气泡样式保持当前深色主题。
- [x] **Step 4: 再运行 Node 测试。** 预期新增和既有用例全部通过。检查长回复重放仍经 `processUpdates()` 创建可切换气泡。

### Task 3: 完整回归验证

**Files:**
- Review: `docs/superpowers/specs/2026-10-20-full-agent-messages-design.md`
- Review: `gui/app.py`, `gui/templates/index.html`, `tests/test_ui_updates.py`, `tests/test_gui_polling.js`

- [x] **Step 1: 运行 Python 套件。** `D:\miniconda3\envs\ursa-up\python.exe -W ignore -m unittest discover -s tests -v`；结果 38 项通过。
- [x] **Step 2: 运行浏览器逻辑套件。** `node --test tests/test_gui_polling.js`；结果 13 项通过。
- [x] **Step 3: 编译检查。** 现有 `__pycache__` 目标文件拒绝写入；改用 `py_compile.compile(..., cfile=<temp>)` 编译 `gui/app.py` 与 `tests/test_ui_updates.py`，退出码 0。
- [x] **Step 4: 按设计稿核对全文、默认折叠、展开收起、短消息、可重放和导出路径。** 仅报告实际验证过的结果。正在运行的用户 Flask 进程不会自动加载 Python 变更，需要用户自行重启。
