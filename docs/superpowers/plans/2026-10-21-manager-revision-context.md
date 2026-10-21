# Manager Revision Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the original request across manual Manager revisions and pass the complete, ordered context to Scientist after approval.

**Architecture:** Keep `Session.user_request` as the original text, store subsequent feedback in `Session.revision_feedback`, and keep the existing `Session` and Agents during `MANAGER_CONFIRM`. The Manager builds revision prompts from explicit session fields and constructs a plain-text handoff only when revisions exist; the no-revision and one-key-hosting paths retain their current behavior.

**Tech Stack:** Python 3, `unittest`, Flask GUI controller; no remote API in tests.

---

## File map

- `tests/test_ui_updates.py`: GUI-level regression for preserving a manual confirmation session and replay history.
- `tests/test_handoff.py`: offline Manager prompt and approval-handoff regressions.
- `gui/app.py`: distinguish a manual Manager revision from a new request in `start_analysis`.
- `core/session.py`: store ordered revision feedback without replacing the original request.
- `agents/manager_agent.py`: build revision prompts and the approved downstream handoff.

### Task 1: Preserve the manual confirmation session

**Files:** Test `tests/test_ui_updates.py`; modify `gui/app.py:494-520`.

- [ ] **Step 1: Write a failing test** in `ReplayableUpdatesTests`:

```python
def test_manager_revision_preserves_session_agents_and_replay_history(self):
    view = make_view()
    view._running = False
    view.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
    session = view.session
    agents = {"manager": object()}
    view.agents = agents
    view._chat_history = [{"content": "原始需求"}]
    view._ui_events = [{"seq": 1, "type": "message", "content": "原始需求"}]
    view._ui_seq = 1
    view._process_flow = lambda *args: None
    view._create_agents = lambda: {"manager": object()}
    view._setup_session = lambda: setattr(view, "session", Session())

    accepted = view.start_analysis("补充面积占比统计")
    view._thread.join()

    self.assertTrue(accepted["success"])
    self.assertIs(view.session, session)
    self.assertIs(view.agents, agents)
    self.assertEqual(view._chat_history[0]["content"], "原始需求")
    self.assertEqual(view._ui_events[0]["content"], "原始需求")
```

- [ ] **Step 2: Run the test and observe failure.** `D:\miniconda3\envs\ursa-up\python.exe -m unittest tests.test_ui_updates.ReplayableUpdatesTests.test_manager_revision_preserves_session_agents_and_replay_history -v` — expected: session or agents changed, or history cleared.
- [ ] **Step 3: Make the smallest GUI change.** Before the existing reset branch, compute:

```python
is_manager_revision = (
    not auto_pilot
    and not is_continue
    and self.session is not None
    and self.session.is_active()
    and self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM
    and bool(self.agents)
)
if (not is_continue and not is_manager_revision) or not self.agents:
    self._chat_history = []
    self._monitor_history = []
    self._raw_history = []
    self._trace_events = []
    self._ui_events = []
    while True:
        try:
            self.ui_queue.get_nowait()
        except Empty:
            break
    self.agents = self._create_agents()
    self._setup_session()
```

- [ ] **Step 4: Run the focused test and `tests.test_ui_updates`.** Expected: all pass.
- [ ] **Step 5: Commit** `tests/test_ui_updates.py` and `gui/app.py` with `fix: preserve Manager session during manual revisions`.

### Task 2: Preserve original demand and all revision prompts

**Files:** Test `tests/test_handoff.py`; modify `core/session.py:27-31`, `agents/manager_agent.py:72-86`.

- [ ] **Step 1: Write a failing offline test** in `ManagerScientistHandoffTests`:

```python
def test_manager_revisions_keep_original_and_ordered_feedback(self):
    session = Session()
    session.start()
    manager = object.__new__(ManagerAgent)
    manager.name = "Manager"
    manager.session = session
    prompts = []

    def call_llm(prompt):
        prompts.append(prompt)
        return f"确认摘要 v{len(prompts)}"

    manager._call_llm = call_llm
    original = "分析北京东城区四类地物：水体、建筑、植被、其他；输入 data/input.tif；输出 JPG"
    first = "增加各类别面积和占比统计"
    second = "把输出改成 PNG，并保留面积统计"
    for text in (original, first, second):
        manager.handle_message(Message(sender="User", receiver="Manager", content=text))

    self.assertEqual(session.user_request, original)
    self.assertEqual(session.revision_feedback, [first, second])
    self.assertIn(original, prompts[-1])
    self.assertLess(prompts[-1].index(first), prompts[-1].index(second))
    self.assertIn("确认摘要 v2", prompts[-1])
    self.assertEqual(session.manager_confirmation, "确认摘要 v3")
```
- [ ] **Step 2: Run** `D:\miniconda3\envs\ursa-up\python.exe -m unittest tests.test_handoff -v` — expected: new test fails because the original is overwritten and no ordered revision field exists.
- [ ] **Step 3: Implement** in `Session.__init__`:

```python
self.revision_feedback: list[str] = []
```

In the Manager User branch, replace unconditional assignment/prompt construction with:

```python
is_revision = bool(self.session.user_request) and (
    self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM
)
if is_revision:
    previous_confirmation = self.session.manager_confirmation
    self.session.revision_feedback.append(message.content)
    revisions = "\n".join(
        f"{index}. {feedback}"
        for index, feedback in enumerate(self.session.revision_feedback, 1)
    )
    prompt = (
        f"用户原始遥感分析需求：\n{self.session.user_request}\n\n"
        f"按时间顺序的修改意见：\n{revisions}\n\n"
        f"上次 Manager 确认摘要（仅供参考）：\n{previous_confirmation}\n\n"
        "请综合原始需求和所有修改意见重新确认分析计划。后来的明确修改覆盖先前冲突条件，"
        "未被修改的条件保持不变。只追问真正缺失的信息；不要生成代码、捏造结果或假定不存在的数据。"
    )
else:
    self.session.user_request = message.content
    prompt = (f"用户提出了以下遥感分析需求：\n\n{message.content}\n\n"
              "请确认你理解了需求，简要说明分析计划，并询问用户是否确认"
              "（回复 approve 确认，或提出修改意见）。注意：不要生成代码，"
              "不要捏造结果，所有数据必须来自本地文件。")
self.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
```

Keep the existing `ERROR:` check and only set `manager_confirmation` after a successful response.
- [ ] **Step 4: Run focused and full Python tests.** Expected: all pass without network calls.
- [ ] **Step 5: Commit** `tests/test_handoff.py`, `core/session.py`, and `agents/manager_agent.py` with `fix: retain ordered Manager revision context`.

### Task 3: Deliver the full approved context to Scientist

**Files:** Test `tests/test_handoff.py`; modify `agents/manager_agent.py:19-32`.

- [ ] **Step 1: Add failing approval test** in `ManagerScientistHandoffTests`:

```python
def test_manager_approval_hands_off_full_revision_context(self):
    session = Session()
    session.start()
    session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
    session.user_request = "东城区四类地物，输入 data/input.tif，输出 JPG"
    session.revision_feedback = ["新增各类面积占比", "输出改为 PNG"]
    session.manager_confirmation = "最新确认摘要：面积统计与 PNG 图"
    manager = object.__new__(ManagerAgent)
    manager.name = "Manager"
    manager.session = session
    manager._call_llm = lambda _: self.fail("approve 不应调用 LLM")

    handoff = manager.handle_message(Message(
        sender="User", receiver="Manager", content="approve",
        msg_type=MessageType.CONTROL,
    ))

    self.assertEqual(session.confirmed_task, handoff.content)
    self.assertIn(session.user_request, handoff.content)
    self.assertLess(handoff.content.index(session.revision_feedback[0]),
                    handoff.content.index(session.revision_feedback[1]))
    self.assertIn(session.manager_confirmation, handoff.content)
    self.assertIn("后来的明确修改覆盖先前冲突条件", handoff.content)
    self.assertIn("未被修改的条件保持不变", handoff.content)
    self.assertNotIn("approve", handoff.content)
```
- [ ] **Step 2: Run** `D:\miniconda3\envs\ursa-up\python.exe -m unittest tests.test_handoff -v` — expected: new approval test fails because handoff is only `user_request`.
- [ ] **Step 3: Implement** a Manager helper and use it in the approve branch:

```python
def _build_confirmed_task(self) -> str:
    request = self.session.user_request
    revisions = self.session.revision_feedback
    if not revisions:
        return request
    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(revisions, 1))
    return (
        f"用户原始需求：\n{request}\n\n"
        f"用户修改意见（按时间顺序）：\n{numbered}\n\n"
        f"Manager 最新确认摘要（辅助理解，不能覆盖用户原文）：\n"
        f"{self.session.manager_confirmation}\n\n"
        "执行规则：后来的明确修改覆盖先前冲突条件；未被修改的条件保持不变。"
    )

# approve branch, before returning the existing Message:
self.session.confirmed_task = self._build_confirmed_task()
```

`gui/app.py:_run_analysis_pipeline` already reads `confirmed_task`, so no pipeline change is needed.
- [ ] **Step 4: Run** `D:\miniconda3\envs\ursa-up\python.exe -m unittest discover -s tests -q` and `node tests\test_gui_polling.js`; expected: full Python and JS suites pass.
- [ ] **Step 5: Commit** `tests/test_handoff.py` and `agents/manager_agent.py` with `fix: hand off complete Manager revision context`.

### Final checks

- [ ] Inspect `git diff` / `git status` for accidental data, config or autopilot changes.
- [ ] Run `D:\miniconda3\envs\ursa-up\python.exe -m compileall -q agents core gui tests` and the full test commands above.
- [ ] Report verified behavior and the remaining limit: these tests prove complete information transmission, not a model's semantic understanding.
