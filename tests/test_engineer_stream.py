"""Engineer LLM stream and failure propagation regression tests."""

from queue import Queue
from types import SimpleNamespace
import unittest

from agents.engineer_agent import EngineerAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from gui.app import URSAGUI
import tools.io_kit


class TruncatedCompletions:
    def create(self, **kwargs):
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="print('unfinished')", reasoning_content=None),
                finish_reason="length",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=32, total_tokens=42),
        )


class EngineerStreamingTests(unittest.TestCase):
    def make_engineer(self):
        session = Session()
        session.start()
        engineer = EngineerAgent(
            model="test-model", api_key="test-key", base_url="https://example.invalid",
            system_prompt="test", max_tokens=32768,
        )
        engineer.session = session
        engineer._build_memories_prompt = lambda: ""
        return session, engineer

    def test_engineer_requests_streaming_for_code_generation(self):
        _, engineer = self.make_engineer()
        captured = {}

        def call_llm(prompt, *args, **kwargs):
            captured.update(kwargs)
            return "print('ok')"

        engineer._call_llm = call_llm
        result = engineer.handle_message(Message(
            sender="Scientist", receiver="Engineer", content="分析四类地物"
        ))

        self.assertTrue(captured["stream"])
        self.assertEqual(result.msg_type, MessageType.CODE)

    def test_engineer_returns_error_without_sanitizing_llm_failure(self):
        _, engineer = self.make_engineer()
        engineer._call_llm = lambda *args, **kwargs: "ERROR: [Engineer] LLM调用失败: Connection error."
        engineer._sanitize_response = lambda _: self.fail("LLM 失败文本不得清理成代码")

        result = engineer.handle_message(Message(
            sender="Scientist", receiver="Engineer", content="分析四类地物"
        ))

        self.assertEqual(result.msg_type, MessageType.ERROR)
        self.assertIn("Connection error", result.content)

    def test_gui_stops_before_executor_when_engineer_fails(self):
        session = Session()
        session.start()
        session.user_request = "分析四类地物"
        called = []

        class Supervisor:
            def select_relevant_files(self, user_input, scan):
                return {"relevant_files": []}

        class Scientist:
            def handle_message(self, message):
                return Message(sender="Scientist", receiver="Engineer", content="分析计划")

        class Engineer:
            def handle_message(self, message):
                return Message(sender="Engineer", receiver="Executor",
                               content="ERROR: [Engineer] LLM调用失败: Connection error.",
                               msg_type=MessageType.ERROR)

        class Executor:
            def handle_message(self, message):
                called.append("executor")

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view = object.__new__(URSAGUI)
            view.ui_queue = Queue()
            view.session = session
            URSAGUI._run_analysis_pipeline(
                view, manager=None, scientist=Scientist(), engineer=Engineer(),
                executor=Executor(), supervisor=Supervisor(), sm=session.state_machine,
                user_input="approve",
            )
            queued = list(view.ui_queue.queue)
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertEqual(called, [])
        self.assertTrue(session.is_terminated())
        self.assertNotEqual(session.state_machine.get_current_phase(), ConversationPhase.COMPLETED)
        self.assertTrue(any(item["type"] == "error" for item in queued))

    def test_gui_stops_repair_loop_when_engineer_fails(self):
        session = Session()
        session.start()
        session.user_request = "分析四类地物"
        calls = []

        class Supervisor:
            def select_relevant_files(self, user_input, scan):
                return {"relevant_files": []}

            def handle_message(self, message):
                return None

        class Scientist:
            def handle_message(self, message):
                return Message(sender="Scientist", receiver="Engineer", content="分析计划")

        class Engineer:
            def handle_message(self, message):
                if message.sender == "Scientist":
                    return Message(sender="Engineer", receiver="Executor",
                                   content="print('first try')", msg_type=MessageType.CODE)
                return Message(sender="Engineer", receiver="Executor",
                               content="ERROR: [Engineer] LLM调用失败: Connection error.",
                               msg_type=MessageType.ERROR)

        class Executor:
            def handle_message(self, message):
                calls.append("executor")
                return Message(sender="Executor", receiver="Engineer",
                               content="SyntaxError: invalid syntax", msg_type=MessageType.ERROR)

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view = object.__new__(URSAGUI)
            view.ui_queue = Queue()
            view.session = session
            view.config = {"executor_max_retries": 5}
            view._is_fatal_data_error = lambda _: False
            URSAGUI._run_analysis_pipeline(
                view, manager=None, scientist=Scientist(), engineer=Engineer(),
                executor=Executor(), supervisor=Supervisor(), sm=session.state_machine,
                user_input="approve",
            )
            queued = list(view.ui_queue.queue)
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertEqual(calls, ["executor"])
        self.assertTrue(session.is_terminated())
        self.assertNotEqual(session.state_machine.get_current_phase(), ConversationPhase.COMPLETED)
        self.assertTrue(any(item["type"] == "error" for item in queued))

    def test_truncated_stream_does_not_publish_partial_code(self):
        session, engineer = self.make_engineer()
        engineer._client = SimpleNamespace(
            chat=SimpleNamespace(completions=TruncatedCompletions())
        )

        result = engineer._call_llm("generate code", stream=True)

        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(engineer._conversation_history, [])
        self.assertEqual(session.tracer.get_events()[-1].data["finish_reason"], "length")


if __name__ == "__main__":
    unittest.main()
