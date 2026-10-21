"""Offline tests for the replayable GUI update protocol."""

from queue import Queue
from threading import RLock
from types import SimpleNamespace
import unittest

from core.session import Session
from core.message import Message
from core.message import MessageType
from core.state_machine import ConversationPhase
from gui.app import URSAGUI
import tools.io_kit


def make_view():
    view = object.__new__(URSAGUI)
    view.session = Session()
    view.session.start()
    view.ui_queue = Queue()
    view._running = True
    view._chat_history = []
    view._monitor_history = []
    view._raw_history = []
    view._trace_events = []
    view._ui_lock = RLock()
    view._ui_events = []
    view._ui_seq = 0
    return view


class ReplayableUpdatesTests(unittest.TestCase):
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
                if sender == "SupervisorAgent":
                    self.assertIn(original, view._monitor_history[0])
                else:
                    self.assertEqual(view._chat_history[0]["content"], original)

    def test_two_clients_can_read_the_same_event_without_consuming_it(self):
        view = make_view()
        view.ui_queue.put({"type": "system", "data": "分析已开始"})

        first = view.get_updates(since=0)
        second = view.get_updates(since=0)
        after = view.get_updates(since=first["cursor"])

        self.assertEqual(first["updates"], second["updates"])
        self.assertEqual(first["updates"][0]["content"], "分析已开始")
        self.assertGreater(first["updates"][0]["seq"], 0)
        self.assertEqual(after["updates"], [])
        self.assertEqual(len(view._raw_history), 1)

    def test_old_done_event_cannot_override_a_running_session(self):
        view = make_view()
        view.session.state_machine.force_transition(ConversationPhase.ENGINEER_CODING)
        view.ui_queue.put({"type": "done", "data": ""})

        state = view.get_updates(since=0)

        self.assertTrue(state["running"])
        self.assertFalse(state["done"])
        self.assertEqual(state["phase"], "engineer")

    def test_terminal_and_confirmation_snapshots_do_not_need_chat_events(self):
        for phase, status in (
            (ConversationPhase.MANAGER_CONFIRM, "等待确认"),
            (ConversationPhase.COMPLETED, "分析完成"),
            (ConversationPhase.TERMINATED, "已终止"),
        ):
            with self.subTest(phase=phase):
                view = make_view()
                view._running = False
                view.session.state_machine.force_transition(phase)
                state = view.get_updates(since=0)
                self.assertFalse(state["running"])
                self.assertTrue(state["done"])
                self.assertEqual(state["status"], status)
                self.assertEqual(
                    state["waiting_confirm"], phase == ConversationPhase.MANAGER_CONFIRM,
                )

    def test_busy_submission_is_identified_without_starting_another_thread(self):
        view = make_view()
        result = view.start_analysis("approve")

        self.assertFalse(result["success"])
        self.assertTrue(result["busy"])

    def test_old_worker_must_exit_before_new_analysis_starts(self):
        view = make_view()
        view._running = False
        view._thread = SimpleNamespace(is_alive=lambda: True)

        result = view.start_analysis("分析新任务")

        self.assertFalse(result["success"])
        self.assertTrue(result["busy"])

    def test_unhandled_worker_error_enters_a_terminal_state(self):
        view = make_view()

        def fail_submission(text):
            raise RuntimeError("offline test failure")

        view.agents = {
            "user": SimpleNamespace(submit_request=fail_submission),
            "manager": object(),
            "scientist": object(),
            "engineer": object(),
            "executor": object(),
            "supervisor": object(),
        }

        view._process_flow("分析东城区")

        self.assertTrue(view.session.is_terminated())
        self.assertTrue(view.get_updates()["done"])

    def test_unhandled_pipeline_error_enters_a_terminal_state(self):
        view = make_view()

        def fail_selection(task, scan):
            raise RuntimeError("offline selection failure")

        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view._run_analysis_pipeline(
                manager=object(), scientist=object(), engineer=object(),
                executor=object(),
                supervisor=SimpleNamespace(select_relevant_files=fail_selection),
                sm=view.session.state_machine,
            )
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertTrue(view.session.is_terminated())

    def test_accepted_request_is_replayable_once_despite_user_bus_echo(self):
        view = make_view()
        view._running = False
        view.agents = {}
        view._create_agents = lambda: {"manager": object()}
        view._setup_session = lambda: None
        view._process_flow = lambda *args: None

        accepted = view.start_analysis("分析东城区四类地物")
        view._thread.join()
        view.ui_queue.put({"type": "message", "data": Message(
            sender="User", receiver="Manager", content="分析东城区四类地物",
        )})
        state = view.get_updates(since=0)

        self.assertTrue(accepted["success"])
        self.assertEqual(
            [(item["sender"], item["content"]) for item in state["updates"]],
            [("User", "分析东城区四类地物")],
        )
        self.assertEqual(len(view._raw_history), 1)

    def test_confirmation_is_replayable_and_busy_rejection_adds_no_event(self):
        view = make_view()
        view._running = False
        view.agents = {"manager": object()}
        view._process_flow = lambda *args: None

        accepted = view.start_analysis("approve")
        view._thread.join()
        rejected = view.start_analysis("approve")
        state = view.get_updates(since=0)

        self.assertTrue(accepted["success"])
        self.assertTrue(rejected["busy"])
        self.assertEqual(len(state["updates"]), 1)
        self.assertEqual(state["updates"][0]["content"], "approve")

    def test_new_request_clears_old_log_without_reusing_sequence_numbers(self):
        view = make_view()
        view._running = False
        view._ui_seq = 5
        view._ui_events = [{"seq": 5, "type": "system", "content": "旧会话", "time": "00:00:00"}]
        view.ui_queue.put({"type": "system", "data": "旧队列"})
        view.agents = {}
        view._create_agents = lambda: {"manager": object()}
        view._setup_session = lambda: None
        view._process_flow = lambda *args: None

        view.start_analysis("新任务")
        view._thread.join()
        state = view.get_updates(since=0)

        self.assertEqual([item["content"] for item in state["updates"]], ["新任务"])
        self.assertGreater(state["updates"][0]["seq"], 5)

    def test_new_chat_clears_backlog_and_pending_queue(self):
        view = make_view()
        view._running = False
        view._ui_seq = 5
        view._ui_events = [{"seq": 5, "type": "system", "content": "旧会话", "time": "00:00:00"}]
        view.ui_queue.put({"type": "system", "data": "旧队列"})
        view._build_data_summary_html = lambda: ""
        view._build_data_analysis_html = lambda: ""

        view.new_chat()
        state = view.get_updates(since=0)

        self.assertEqual(state["updates"], [])
        self.assertEqual(state["cursor"], 5)

    def test_new_chat_does_not_discard_a_running_analysis(self):
        view = make_view()
        view._ui_events = [{"seq": 1, "type": "system", "content": "运行中", "time": "00:00:00"}]
        view._ui_seq = 1

        response = view.new_chat()

        self.assertFalse(response["success"])
        self.assertTrue(view._running)
        self.assertIsNotNone(view.session)
        self.assertEqual(len(view._ui_events), 1)

    def test_new_chat_waits_for_a_terminated_worker_to_exit(self):
        view = make_view()
        view._running = False
        view._thread = SimpleNamespace(is_alive=lambda: True)

        response = view.new_chat()

        self.assertFalse(response["success"])
        self.assertIsNotNone(view.session)

    def test_exhausted_executor_retries_enter_a_terminal_state(self):
        view = make_view()
        view.config = {"executor_max_retries": 1}
        view.session.confirmed_task = "分析东城区"
        view._is_fatal_data_error = lambda message: False
        supervisor = SimpleNamespace(
            select_relevant_files=lambda task, scan: {"relevant_files": []},
        )
        scientist = SimpleNamespace(handle_message=lambda message: Message(
            sender="Scientist", receiver="Engineer", content="分析计划",
        ))
        engineer = SimpleNamespace(handle_message=lambda message: Message(
            sender="Engineer", receiver="Executor", content="print('x')",
            msg_type=MessageType.CODE,
        ))
        executor = SimpleNamespace(handle_message=lambda message: Message(
            sender="Executor", receiver="Engineer", content="执行失败",
            msg_type=MessageType.ERROR,
        ))
        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view._run_analysis_pipeline(
                manager=object(), scientist=scientist, engineer=engineer,
                executor=executor, supervisor=supervisor,
                sm=view.session.state_machine,
            )
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertTrue(view.session.is_terminated())
        view._running = False  # _process_flow 的 finally 在真实线程中清除此标志
        self.assertTrue(view.get_updates()["done"])

    def test_fatal_data_error_enters_a_terminal_state(self):
        view = make_view()
        view.config = {"executor_max_retries": 1}
        view.session.confirmed_task = "分析东城区"
        view._is_fatal_data_error = lambda message: True
        supervisor = SimpleNamespace(
            select_relevant_files=lambda task, scan: {"relevant_files": []},
        )
        scientist = SimpleNamespace(handle_message=lambda message: Message(
            sender="Scientist", receiver="Engineer", content="分析计划",
        ))
        engineer = SimpleNamespace(
            handle_message=lambda message: Message(
                sender="Engineer", receiver="Executor", content="print('x')",
                msg_type=MessageType.CODE,
            ),
            report_issue=lambda **kwargs: None,
        )
        executor = SimpleNamespace(handle_message=lambda message: Message(
            sender="Executor", receiver="Engineer", content="缺少输入文件",
            msg_type=MessageType.ERROR,
        ))
        original_scan = tools.io_kit.scan_data_directory
        tools.io_kit.scan_data_directory = lambda: {"success": True, "data": {"files": []}}
        try:
            view._run_analysis_pipeline(
                manager=object(), scientist=scientist, engineer=engineer,
                executor=executor, supervisor=supervisor,
                sm=view.session.state_machine,
            )
        finally:
            tools.io_kit.scan_data_directory = original_scan

        self.assertTrue(view.session.is_terminated())


if __name__ == "__main__":
    unittest.main()
