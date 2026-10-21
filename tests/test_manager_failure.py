"""Manager timeout handling without making network requests."""

from queue import Queue
from types import SimpleNamespace
import unittest

from agents.manager_agent import ManagerAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from gui.app import URSAGUI
import tools.io_kit


class ManagerFailureTests(unittest.TestCase):
    def test_manager_timeout_is_an_error_not_a_confirmation_plan(self):
        session = Session()
        session.start()
        manager = ManagerAgent(
            model="test-model", api_key="test-key",
            base_url="https://example.invalid", system_prompt="test",
        )
        manager.session = session
        manager._call_llm = lambda prompt: "ERROR: [Manager] LLM调用失败: Request timed out."

        response = manager.handle_message(Message(
            sender="User", receiver="Manager", content="分析东城区地物",
        ))

        self.assertEqual(response.msg_type, MessageType.ERROR)
        self.assertEqual(session.manager_confirmation, "")

    def test_manager_report_timeout_is_not_a_report(self):
        session = Session()
        session.start()
        manager = ManagerAgent(
            model="test-model", api_key="test-key",
            base_url="https://example.invalid", system_prompt="test",
        )
        manager.session = session
        manager._call_llm = lambda prompt: "ERROR: [Manager] LLM调用失败: Request timed out."

        response = manager.handle_message(Message(
            sender="Executor", receiver="Manager", content="执行结果",
            msg_type=MessageType.RESULT,
        ))

        self.assertEqual(response.msg_type, MessageType.ERROR)

    def test_gui_stops_after_initial_manager_timeout(self):
        session = Session()
        session.start()
        view = object.__new__(URSAGUI)
        view.session = session
        view.ui_queue = Queue()
        view._running = True
        view.agents = {
            "user": SimpleNamespace(submit_request=lambda text: Message(
                sender="User", receiver="Manager", content=text,
            )),
            "manager": SimpleNamespace(handle_message=lambda message: Message(
                sender="Manager", receiver="User",
                content="ERROR: [Manager] LLM调用失败: Request timed out.",
                msg_type=MessageType.ERROR,
            )),
            "scientist": object(),
            "engineer": object(),
            "executor": object(),
            "supervisor": object(),
        }
        pipeline_calls = []
        view._run_analysis_pipeline = lambda *args: pipeline_calls.append(args)

        URSAGUI._process_flow(view, "分析东城区地物")
        events = list(view.ui_queue.queue)

        self.assertTrue(session.is_terminated())
        self.assertEqual(session.state_machine.get_current_phase(), ConversationPhase.TERMINATED)
        self.assertEqual(pipeline_calls, [])
        self.assertTrue(any(item["type"] == "error" for item in events))
        self.assertFalse(any(
            item["type"] == "system" and "请审核 Manager" in item["data"]
            for item in events
        ))

    def test_gui_stops_after_manager_times_out_on_revision(self):
        session = Session()
        session.start()
        session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
        view = object.__new__(URSAGUI)
        view.session = session
        view.ui_queue = Queue()
        view._running = True
        view.agents = {
            "user": object(),
            "manager": SimpleNamespace(handle_message=lambda message: Message(
                sender="Manager", receiver="User",
                content="ERROR: [Manager] LLM调用失败: Request timed out.",
                msg_type=MessageType.ERROR,
            )),
            "scientist": object(),
            "engineer": object(),
            "executor": object(),
            "supervisor": object(),
        }

        URSAGUI._process_flow(view, "请修改为四分类")
        events = list(view.ui_queue.queue)

        self.assertTrue(session.is_terminated())
        self.assertTrue(any(item["type"] == "error" for item in events))
        self.assertFalse(any(item["type"] == "message" and
                             item["data"].sender == "Manager" for item in events))

    def test_user_message_is_not_echoed_to_frontend_twice(self):
        session = Session()
        session.start()
        view = object.__new__(URSAGUI)
        view.session = session
        view._running = True
        view._monitor_history = []
        view._chat_history = []
        view._raw_history = []
        view._trace_events = []

        result = URSAGUI._process_updates(view, [{
            "type": "message",
            "data": Message(sender="User", receiver="Manager", content="分析东城区地物"),
        }])

        self.assertEqual(result["updates"], [])
        self.assertEqual(len(view._raw_history), 0)

    def test_gui_does_not_mark_report_timeout_as_completed(self):
        session = Session()
        session.start()
        session.confirmed_task = "分析东城区地物"
        view = object.__new__(URSAGUI)
        view.session = session
        view.ui_queue = Queue()
        view.config = {"executor_max_retries": 1}
        review_phases = []

        supervisor = SimpleNamespace(
            select_relevant_files=lambda task, scan: {"relevant_files": []},
            handle_message=lambda message: review_phases.append(
                session.state_machine.get_current_phase()
            ) or None,
        )
        scientist = SimpleNamespace(handle_message=lambda message: Message(
            sender="Scientist", receiver="Engineer", content="分析计划",
        ))
        engineer = SimpleNamespace(handle_message=lambda message: Message(
            sender="Engineer", receiver="Executor", content="print('ok')",
            msg_type=MessageType.CODE,
        ))
        executor = SimpleNamespace(handle_message=lambda message: Message(
            sender="Executor", receiver="Manager", content="执行完成",
            msg_type=MessageType.RESULT,
        ))
        manager = SimpleNamespace(handle_message=lambda message: Message(
            sender="Manager", receiver="User",
            content="ERROR: [Manager] LLM调用失败: Request timed out.",
            msg_type=MessageType.ERROR,
        ))

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            URSAGUI._run_analysis_pipeline(
                view, manager=manager, scientist=scientist, engineer=engineer,
                executor=executor, supervisor=supervisor, sm=session.state_machine,
                user_input="approve",
            )
        finally:
            tools.io_kit.scan_data_directory = original_scan

        events = list(view.ui_queue.queue)
        self.assertTrue(session.is_terminated())
        self.assertNotEqual(session.state_machine.get_current_phase(), ConversationPhase.COMPLETED)
        self.assertTrue(any(item["type"] == "error" for item in events))
        self.assertEqual(review_phases, [ConversationPhase.REVIEW_CHECKING])


if __name__ == "__main__":
    unittest.main()
