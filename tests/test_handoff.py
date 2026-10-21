"""Regression tests for the Manager-to-Scientist task handoff."""

from queue import Queue
from types import SimpleNamespace
import unittest

from agents.base_agent import BaseAgent
from agents.manager_agent import ManagerAgent
from agents.scientist_agent import ScientistAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from gui.app import URSAGUI
import tools.io_kit


class DiagnosticAgent(BaseAgent):
    """仅供无网络 LLM 诊断测试使用。"""

    def handle_message(self, message):
        return None


class RaisingCompletions:
    def create(self, **kwargs):
        error = RuntimeError("Connection error")
        error.request_id = "req-diagnostic-test"
        try:
            raise ConnectionError("proxy connection reset")
        except ConnectionError as cause:
            raise error from cause


class StreamingCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="思考中"))],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="分析", reasoning_content=None))],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="方案", reasoning_content=None),
                finish_reason="stop",
            )],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8, total_tokens=20),
        )


class BrokenStreamingCompletions:
    def create(self, **kwargs):
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="不完整", reasoning_content=None))],
            usage=None,
        )
        raise ConnectionError("stream closed")


class ManagerScientistHandoffTests(unittest.TestCase):
    def test_llm_client_disables_automatic_retries(self):
        agent = DiagnosticAgent(
            name="Scientist",
            system_prompt="test",
            model="test-model",
            api_key="test-key",
            base_url="https://example.invalid",
        )

        self.assertEqual(agent._client.max_retries, 0)

    def test_llm_connection_error_trace_includes_safe_diagnostics(self):
        session = Session()
        session.start()
        agent = DiagnosticAgent(
            name="Scientist",
            system_prompt="test",
            model="test-model",
            api_key="test-key",
            base_url="https://example.invalid",
        )
        agent.session = session
        agent._client = SimpleNamespace(
            chat=SimpleNamespace(completions=RaisingCompletions())
        )

        result = agent._call_llm("classify the raster")
        trace = session.tracer.get_events()[-1]

        self.assertEqual(result, "ERROR: [Scientist] LLM调用失败: Connection error")
        self.assertEqual(trace.status, "error")
        self.assertEqual(trace.data["error_type"], "RuntimeError")
        self.assertEqual(trace.data["cause_type"], "ConnectionError")
        self.assertEqual(trace.data["request_id"], "req-diagnostic-test")
        self.assertEqual(trace.data["attempts"], 1)
        self.assertEqual(trace.data["prompt_chars"], len("classify the raster"))

    def test_streamed_llm_collects_final_content_and_usage(self):
        session = Session()
        session.start()
        agent = DiagnosticAgent(
            name="Scientist", system_prompt="test", model="test-model",
            api_key="test-key", base_url="https://example.invalid",
        )
        agent.session = session
        completions = StreamingCompletions()
        agent._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        result = agent._call_llm("analyse image", stream=True, reasoning_effort="low")

        self.assertEqual(result, "分析方案")
        self.assertTrue(completions.kwargs["stream"])
        self.assertTrue(completions.kwargs["stream_options"]["include_usage"])
        self.assertEqual(completions.kwargs["reasoning_effort"], "low")
        trace = session.tracer.get_events()[-1]
        self.assertEqual(trace.data["total_tokens"], 20)
        self.assertTrue(trace.data["streamed"])
        self.assertGreaterEqual(trace.data["first_chunk_ms"], 0)
        self.assertEqual(trace.data["reasoning_chars"], len("思考中"))
        self.assertEqual(agent._conversation_history[-1]["content"], "分析方案")

    def test_scientist_requests_streaming(self):
        session = Session()
        session.start()
        scientist = ScientistAgent(
            model="test-model", api_key="test-key", base_url="https://example.invalid",
            system_prompt="test",
        )
        scientist.session = session
        captured = {}

        def call_llm(prompt, **kwargs):
            captured.update(kwargs)
            return "分析方案"

        scientist._call_llm = call_llm
        scientist.handle_message(Message(
            sender="Manager", receiver="Scientist", content="分析北京东城区绿地"
        ))

        self.assertTrue(captured["stream"])
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertEqual(scientist.max_tokens, 8192)

    def test_broken_stream_does_not_publish_partial_plan(self):
        session = Session()
        session.start()
        agent = DiagnosticAgent(
            name="Scientist", system_prompt="test", model="test-model",
            api_key="test-key", base_url="https://example.invalid",
        )
        agent.session = session
        agent._client = SimpleNamespace(
            chat=SimpleNamespace(completions=BrokenStreamingCompletions())
        )

        result = agent._call_llm("analyse image", stream=True)

        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(agent._conversation_history, [])
        trace = session.tracer.get_events()[-1]
        self.assertEqual(trace.status, "error")
        self.assertTrue(trace.data["streamed"])
        self.assertGreaterEqual(trace.data["first_chunk_ms"], 0)

    def test_manager_persists_confirmed_handoff(self):
        session = Session()
        session.start()
        session.user_request = "绘制北京东城区 2023 年绿地分布图"
        session.manager_confirmation = "目标、区域、时间和输出均已向用户确认。"

        manager = object.__new__(ManagerAgent)
        manager.name = "Manager"
        manager.session = session
        manager._call_llm = lambda _: self.fail("确认阶段不应再次调用付费模型")

        handoff = ManagerAgent.handle_message(
            manager,
            Message(
                sender="User",
                receiver="Manager",
                content="approve",
                msg_type=MessageType.CONTROL,
            ),
        )

        self.assertEqual(session.confirmed_task, session.user_request)
        self.assertEqual(handoff.content, session.user_request)

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
        for requirement in (original, first, second):
            manager.handle_message(Message(sender="User", receiver="Manager", content=requirement))

        self.assertEqual(session.user_request, original)
        self.assertEqual(session.revision_feedback, [first, second])
        self.assertIn(original, prompts[-1])
        self.assertLess(prompts[-1].index(first), prompts[-1].index(second))
        self.assertIn("确认摘要 v2", prompts[-1])
        self.assertEqual(session.manager_confirmation, "确认摘要 v3")

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
        self.assertIn(session.revision_feedback[0], handoff.content)
        self.assertIn(session.revision_feedback[1], handoff.content)
        self.assertLess(handoff.content.index(session.revision_feedback[0]),
                        handoff.content.index(session.revision_feedback[1]))
        self.assertIn(session.manager_confirmation, handoff.content)
        self.assertIn("后来的明确修改覆盖先前冲突条件", handoff.content)
        self.assertIn("未被修改的条件保持不变", handoff.content)
        self.assertNotIn("approve", handoff.content)

    def test_scientist_reports_llm_failure_as_error_message(self):
        session = Session()
        session.start()
        scientist = object.__new__(ScientistAgent)
        scientist.name = "Scientist"
        scientist.session = session
        scientist._call_llm = lambda _, **kwargs: "ERROR: [Scientist] LLM调用失败: Connection error."

        result = scientist.handle_message(Message(
            sender="Manager", receiver="Scientist", content="已确认任务: 提取绿地"
        ))

        self.assertEqual(result.msg_type, MessageType.ERROR)
        self.assertEqual(session.state_machine.get_current_phase(), ConversationPhase.SCIENTIST_ANALYSIS)

    def test_gui_stops_pipeline_when_scientist_fails(self):
        session = Session()
        session.start()
        session.user_request = "提取北京东城区绿地"
        called = []

        class Supervisor:
            def select_relevant_files(self, user_input, scan):
                return {"relevant_files": []}

        class Scientist:
            def handle_message(self, message):
                return Message(sender="Scientist", receiver="Engineer",
                               content="ERROR: [Scientist] LLM调用失败: Connection error.",
                               msg_type=MessageType.ERROR)

        class Engineer:
            def handle_message(self, message):
                called.append("engineer")

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view = object.__new__(URSAGUI)
            view.ui_queue = Queue()
            view.session = session
            URSAGUI._run_analysis_pipeline(
                view, manager=None, scientist=Scientist(), engineer=Engineer(),
                executor=None, supervisor=Supervisor(), sm=session.state_machine,
                user_input="approve",
            )
            queued = list(view.ui_queue.queue)
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertEqual(called, [])
        self.assertTrue(session.is_terminated())
        self.assertNotEqual(session.state_machine.get_current_phase(), ConversationPhase.COMPLETED)
        self.assertTrue(any(item["type"] == "error" for item in queued))

    def test_gui_done_event_does_not_report_failed_run_as_complete(self):
        session = Session()
        session.start()
        session.terminate("Scientist LLM 调用失败")
        view = object.__new__(URSAGUI)
        view.session = session
        view._running = False
        view._monitor_history = []
        view._chat_history = []
        view._raw_history = []
        view._trace_events = []

        result = URSAGUI._process_updates(view, [
            {"type": "error", "data": "Scientist LLM 调用失败"},
            {"type": "done", "data": ""},
        ])

        self.assertTrue(result["done"])
        self.assertNotEqual(result["status"], "分析完成")

    def test_gui_uses_confirmed_task_not_approval_token_for_scientist(self):
        session = Session()
        session.start()
        session.confirmed_task = "提取北京东城区 2023 年绿地，输出专题图和覆盖率。"
        captured = {}

        class Supervisor:
            def select_relevant_files(self, user_input, scan):
                return {"relevant_files": [], "summary": ""}

        class Scientist:
            def handle_message(self, message):
                captured["input"] = message.content
                session.terminate("test complete")
                return Message(sender="Scientist", receiver="Engineer", content="unused")

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view = object.__new__(URSAGUI)
            view.ui_queue = Queue()
            view.session = session
            URSAGUI._run_analysis_pipeline(
                view,
                manager=None,
                scientist=Scientist(),
                engineer=None,
                executor=None,
                supervisor=Supervisor(),
                sm=session.state_machine,
                user_input="approve",
            )
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertIn(session.confirmed_task, captured["input"])
        self.assertNotIn("用户需求: approve", captured["input"])

    def test_gui_confirmation_generates_manager_handoff_before_pipeline(self):
        session = Session()
        session.start()
        session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
        received = {}

        class Manager:
            def handle_message(self, message):
                received["message"] = message
                session.confirmed_task = "Manager 的正式交接任务"
                return Message(sender="Manager", receiver="Scientist", content=session.confirmed_task)

        class Supervisor:
            pass

        view = object.__new__(URSAGUI)
        view.session = session
        view.ui_queue = Queue()
        view._running = True
        view.agents = {
            "user": object(),
            "manager": Manager(),
            "scientist": object(),
            "engineer": object(),
            "executor": object(),
            "supervisor": Supervisor(),
        }
        pipeline_calls = []
        view._run_analysis_pipeline = lambda *args, **kwargs: pipeline_calls.append((args, kwargs))

        URSAGUI._process_flow(view, "approve")

        self.assertIn("message", received)
        self.assertEqual(received["message"].msg_type, MessageType.CONTROL)
        self.assertEqual(received["message"].content, "approve")
        self.assertEqual(len(pipeline_calls), 1)


if __name__ == "__main__":
    unittest.main()
