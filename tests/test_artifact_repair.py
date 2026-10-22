"""产物核验后的自动修复回归；使用模拟模型、真实临时产物，不发起API请求。"""
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.executor_agent import ExecutorAgent
from agents.supervisor_agent import SupervisorAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from tests.test_ui_updates import make_view


def artifact_code(area_missing=True, create_image=True, csv_name="class_statistics.csv"):
    table = ("类别编号,类别,像元数,面积_km2,占比_%\n"
             "1,水体,1,N/A,25\n2,其他,3,N/A,75\n") if area_missing else (
             "类别编号,类别,像元数,占比_%\n1,水体,1,25\n2,其他,3,75\n")
    return ("# 完整代码开头必须保留\n" + "# 已验证的其他任务逻辑\n" * 220 +
            "import os\nfrom pathlib import Path\n"
            "root = Path(os.environ['URSA_RESULTS_DIR'])\n"
            f"(root / {csv_name!r}).write_text({table!r}, encoding='utf-8-sig')\n" +
            ("from PIL import Image\nImage.new('RGB', (8, 8), 'white').save(root / 'map.jpg')\n" if create_image else "") +
            "print('任务产物已保存')\n")


class ArtifactRepairTests(unittest.TestCase):
    def run_gui_case(self, tmp, *, repaired_code=None, attempts=3):
        view = make_view()
        view.config = {"executor_max_retries": attempts}
        view.session.user_request = "根据本地影像分类并输出JPG和占比；无坐标信息，不要编造面积"
        view.session.revision_feedback = ["保留未知类别，不要伪造精度"]
        view.session.confirmed_task = view.session.user_request
        supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
        supervisor.session, supervisor._project_root = view.session, tmp
        supervisor.select_relevant_files = lambda *args: {"relevant_files": []}
        executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
        executor.session, executor._project_root = view.session, tmp
        repairs, reports, executions = [], [], []
        initial = artifact_code()

        def engineer(message):
            if message.sender == "Scientist":
                code = initial
            else:
                repairs.append(message)
                code = repaired_code if repaired_code is not None else artifact_code(False, False)
            return Message(sender="Engineer", receiver="Executor", content=code, msg_type=MessageType.CODE)

        execute = executor.handle_message

        def counted_execute(message):
            executions.append(message)
            return execute(message)

        def manager(message):
            reports.append(message)
            return Message(sender="Manager", receiver="User", content="产物可读取，语义待复核；未提供可计算地面面积")

        with patch("tools.io_kit.scan_data_directory", return_value={"data": {"files": []}}):
            view._run_analysis_pipeline(
                manager=SimpleNamespace(handle_message=manager),
                scientist=SimpleNamespace(handle_message=lambda m: Message(sender="Scientist", receiver="Engineer", content="分析计划")),
                engineer=SimpleNamespace(handle_message=engineer, report_issue=lambda **kw: None),
                executor=SimpleNamespace(handle_message=counted_execute),
                supervisor=supervisor, sm=view.session.state_machine,
            )
        return view, repairs, reports, executions, initial

    def test_gui_repairs_na_csv_and_rechecks_retained_image(self):
        with TemporaryDirectory() as tmp:
            view, repairs, reports, executions, initial = self.run_gui_case(tmp)
            self.assertEqual(len(repairs), 1)
            self.assertEqual(len(executions), 2)
            self.assertEqual(len(reports), 1)
            self.assertFalse(view.session.is_terminated())
            self.assertEqual(view.session.result_audit["errors"], [])
            self.assertEqual(view.session.result_audit["status"], "needs_review")
            self.assertIn(initial, repairs[0].content)
            self.assertIn(view.session.user_request, repairs[0].content)
            self.assertIn(view.session.revision_feedback[0], repairs[0].content)
            self.assertIn("不能填0", repairs[0].content)
            self.assertEqual(len(reports[0].metadata["files_generated"]), 2)
            self.assertTrue(any(name.endswith("map.jpg") for name in reports[0].metadata["files_generated"]))
            self.assertTrue(all(row["area_km2"] is None for row in view.session.result_audit["statistics"]))
            view._running = False
            self.assertEqual(view.get_updates()["status"], "分析结束（结果待复核）")
            validations = [e for e in view.session.tracer.get_events() if e.data.get("tool_name") == "artifact_validation"]
            self.assertEqual([e.data["audit_status"] for e in validations], ["failed", "needs_review"])

    def test_gui_stops_at_shared_attempt_limit_without_report(self):
        with TemporaryDirectory() as tmp:
            view, repairs, reports, executions, _ = self.run_gui_case(tmp, repaired_code=artifact_code(), attempts=2)
            self.assertEqual(len(executions), 2)
            self.assertEqual(len(repairs), 1)
            self.assertEqual(reports, [])
            self.assertTrue(view.session.is_terminated())
            self.assertIn("最大尝试次数", view.session.get_terminate_reason())
            self.assertEqual(view.session.result_audit["status"], "failed")

    def test_renaming_csv_does_not_hide_the_failed_artifact(self):
        with TemporaryDirectory() as tmp:
            view, repairs, reports, executions, _ = self.run_gui_case(
                tmp, repaired_code=artifact_code(False, False, "replacement.csv"), attempts=2)
            self.assertEqual(len(executions), 2)
            self.assertEqual(reports, [])
            self.assertTrue(view.session.is_terminated())
            self.assertTrue(any("class_statistics.csv" in e for e in view.session.result_audit["errors"]))

    def test_zero_attempt_limit_never_publishes_success(self):
        with TemporaryDirectory() as tmp:
            view, repairs, reports, executions, _ = self.run_gui_case(tmp, attempts=0)
            self.assertEqual(repairs, [])
            self.assertEqual(executions, [])
            self.assertEqual(reports, [])
            self.assertTrue(view.session.is_terminated())

    def test_removing_required_statistics_cannot_hide_the_failure(self):
        code = ("import os\nfrom pathlib import Path\n"
                "(Path(os.environ['URSA_RESULTS_DIR']) / 'class_statistics.csv').write_text('说明\\n已生成\\n', encoding='utf8')\n")
        with TemporaryDirectory() as tmp:
            view, repairs, reports, executions, _ = self.run_gui_case(tmp, repaired_code=code)
            self.assertEqual(len(executions), 2)
            self.assertEqual(reports, [])
            self.assertTrue(view.session.is_terminated())
            self.assertIn("删除必需字段", view.session.get_terminate_reason())

    def test_engineer_error_stops_before_another_execution(self):
        with TemporaryDirectory() as tmp:
            # 用一次执行后再替换Engineer响应，验证错误内容不会交给Executor。
            view = make_view()
            view.config = {"executor_max_retries": 3}
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = view.session, tmp
            supervisor.select_relevant_files = lambda *a: {"relevant_files": []}
            responses = iter([
                Message(sender="Engineer", receiver="Executor", content="initial", msg_type=MessageType.CODE),
                Message(sender="Engineer", receiver="Manager", content="ERROR: 模型响应失败", msg_type=MessageType.ERROR)])
            with patch("tools.io_kit.scan_data_directory", return_value={"data": {"files": []}}):
                view._run_analysis_pipeline(
                    manager=SimpleNamespace(handle_message=lambda m: self.fail("不应生成报告")),
                    scientist=SimpleNamespace(handle_message=lambda m: Message(sender="Scientist", receiver="Engineer", content="计划")),
                    engineer=SimpleNamespace(handle_message=lambda m: next(responses)),
                    executor=SimpleNamespace(handle_message=lambda m: Message(sender="Executor", receiver="Manager", content="完成", msg_type=MessageType.RESULT, metadata={"files_generated": []})),
                    supervisor=supervisor, sm=view.session.state_machine)
            self.assertTrue(view.session.is_terminated())
            self.assertIn("模型响应失败", view.session.get_terminate_reason())


    def test_execution_error_and_artifact_error_share_one_budget(self):
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            session = Session()
            session.start()
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, tmp
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            executor.session, executor._project_root = session, tmp
            codes = iter([artifact_code(), artifact_code(False, False)])
            repairs = []

            def repair(message):
                repairs.append(message)
                return Message(sender="Engineer", receiver="Executor", content=next(codes), msg_type=MessageType.CODE)

            result = run_execution_cycle(session, SimpleNamespace(handle_message=repair), executor, supervisor,
                Message(sender="Engineer", receiver="Executor", content="raise RuntimeError('临时计算错误')", msg_type=MessageType.CODE),
                max_attempts=3, is_fatal_data_error=lambda text: False)
            self.assertIsNotNone(result)
            self.assertEqual([m.sender for m in repairs], ["Executor", "SupervisorAgent"])
            self.assertEqual(len(result.metadata["files_generated"]), 2)
            self.assertEqual(session.result_audit["errors"], [])

    def test_task_boundary_error_blocks_without_asking_engineer(self):
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            session = Session()
            session.start()
            root = Path(tmp) / "results"
            root.mkdir()
            (root / "foreign.txt").write_text("另一个任务的产物", encoding="utf8")
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, tmp
            result = run_execution_cycle(session,
                SimpleNamespace(handle_message=lambda m: self.fail("跨任务产物不应自动重试")),
                SimpleNamespace(handle_message=lambda m: Message(sender="Executor", receiver="Manager", content="完成",
                    msg_type=MessageType.RESULT, metadata={"files_generated": ["foreign.txt"]})),
                supervisor, Message(sender="Engineer", receiver="Executor", content="code"), max_attempts=3)
            self.assertIsNone(result)
            self.assertTrue(session.is_terminated())
            self.assertTrue(any("本次任务目录" in e for e in session.result_audit["errors"]))

    def test_missing_executor_response_cannot_become_success(self):
        from core.execution_cycle import run_execution_cycle
        session = Session()
        session.start()
        result = run_execution_cycle(session, object(), SimpleNamespace(handle_message=lambda m: None),
                                     object(), Message(sender="Engineer", receiver="Executor", content="code"))
        self.assertIsNone(result)
        self.assertTrue(session.is_terminated())

    def test_missing_audit_cannot_become_success(self):
        from core.execution_cycle import run_execution_cycle
        session = Session()
        session.start()
        result = run_execution_cycle(session, object(),
            SimpleNamespace(handle_message=lambda m: Message(sender="Executor", receiver="Manager", content="完成", msg_type=MessageType.RESULT)),
            SimpleNamespace(handle_message=lambda m: None), Message(sender="Engineer", receiver="Executor", content="code"))
        self.assertIsNone(result)
        self.assertTrue(session.is_terminated())
        self.assertIn("核验结果", session.get_terminate_reason())

    def test_cancel_during_repair_prevents_next_execution(self):
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            session = Session()
            session.start()
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, tmp
            calls = []

            def execute(message):
                calls.append(message)
                return Message(sender="Executor", receiver="Manager", content="完成", msg_type=MessageType.RESULT,
                               metadata={"files_generated": []})

            def repair(message):
                session.terminate("用户取消")
                return Message(sender="Engineer", receiver="Executor", content="print('不应执行')", msg_type=MessageType.CODE)

            result = run_execution_cycle(session, SimpleNamespace(handle_message=repair),
                SimpleNamespace(handle_message=execute), supervisor,
                Message(sender="Engineer", receiver="Executor", content="code"))
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)
            self.assertEqual(session.get_terminate_reason(), "用户取消")

    def test_fatal_input_error_does_not_retry(self):
        from core.execution_cycle import run_execution_cycle
        session = Session()
        session.start()
        reports = []
        result = run_execution_cycle(session,
            SimpleNamespace(handle_message=lambda m: self.fail("缺少输入不能凭空修复"), report_issue=lambda **kw: reports.append(kw)),
            SimpleNamespace(handle_message=lambda m: Message(sender="Executor", receiver="Engineer",
                content="FileNotFoundError: 输入影像不存在", msg_type=MessageType.ERROR)),
            object(), Message(sender="Engineer", receiver="Executor", content="code"),
            max_attempts=5, is_fatal_data_error=lambda text: True)
        self.assertIsNone(result)
        self.assertEqual(len(reports), 1)
        self.assertTrue(session.is_terminated())

    def test_warning_only_finishes_without_repair(self):
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            session = Session()
            session.start()
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            executor.session, executor._project_root = session, tmp
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, tmp
            result = run_execution_cycle(session,
                SimpleNamespace(handle_message=lambda m: self.fail("语义待复核不能触发修复")),
                executor, supervisor, Message(sender="Engineer", receiver="Executor",
                content=artifact_code(False), msg_type=MessageType.CODE))
            self.assertIsNotNone(result)
            self.assertEqual(session.result_audit["status"], "needs_review")
            self.assertEqual(len(session.tracer.get_events()), 2)  # 一次执行与一次核验
            self.assertFalse(session.is_terminated())

    def test_unrelated_existing_image_cannot_satisfy_this_task(self):
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            session = Session()
            session.start()
            session.user_request = "输出JPG"
            old = Path(tmp) / "results" / "old_task"
            old.mkdir(parents=True)
            from PIL import Image
            Image.new("RGB", (8, 8), "white").save(old / "map.jpg")
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            executor.session, executor._project_root = session, tmp
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, tmp
            result = run_execution_cycle(session, object(), executor, supervisor,
                Message(sender="Engineer", receiver="Executor", content=artifact_code(False, False), msg_type=MessageType.CODE),
                max_attempts=1)
            self.assertIsNone(result)
            self.assertTrue(any("JPG" in e for e in session.result_audit["errors"]))

    def test_real_engineer_accepts_supervisor_repair_message(self):
        from agents.engineer_agent import EngineerAgent
        from core.execution_cycle import _repair_message
        session = Session()
        session.start()
        session.user_request = "输出分类图"
        agent = EngineerAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
        agent.session = session
        failure = Message(sender="SupervisorAgent", receiver="Engineer", content="占比与像元数不一致",
                          msg_type=MessageType.ERROR, metadata={"action": "repair_artifacts", "diagnosis": {"issues": []}})
        repair = _repair_message(session, failure, "print('完整原代码')")
        with patch.object(agent, "_call_llm_with_memory", return_value="print('修复后的代码')") as call:
            result = agent.handle_message(repair)
        self.assertEqual(result.msg_type, MessageType.CODE)
        self.assertIn("完整原代码", call.call_args[0][0])
        self.assertIn(session.result_relative_dir, call.call_args[0][0])
        self.assertIn("禁止修改项目源码", call.call_args[0][0])

    def test_cli_recovers_or_stops_using_the_shared_cycle(self):
        import main
        for limit in (1, 3):
            with self.subTest(limit=limit), TemporaryDirectory() as tmp:
                session = Session()
                supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
                supervisor._project_root = tmp
                supervisor.select_relevant_files = lambda *a: {"relevant_files": []}
                executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
                executor._project_root = tmp
                repairs, reports = [], []

                def manager(message):
                    if message.msg_type == MessageType.RESULT:
                        reports.append(message)
                        return Message(sender="Manager", receiver="User", content="产物可读，语义待复核")
                    session.user_request = session.confirmed_task = "本地分类，输出JPG和占比"
                    return Message(sender="Manager", receiver="Scientist", content=session.confirmed_task)

                def engineer(message):
                    if message.sender != "Scientist":
                        repairs.append(message)
                    return Message(sender="Engineer", receiver="Executor",
                        content=artifact_code(False, False) if repairs else artifact_code(), msg_type=MessageType.CODE)

                user = SimpleNamespace(name="User", submit_request=lambda t: Message(sender="User", receiver="Manager", content=t))
                manager_agent = SimpleNamespace(name="Manager", handle_message=manager)
                scientist = SimpleNamespace(name="Scientist", handle_message=lambda m: Message(sender="Scientist", receiver="Engineer", content="计划"))
                engineer_agent = SimpleNamespace(name="Engineer", handle_message=engineer, report_issue=lambda **kw: None)
                (Path(tmp) / "config.json").write_text(json.dumps(
                    {"model": "test", "api_key": "test", "base_url": "https://example.invalid",
                     "executor_max_retries": limit}), encoding="utf8")
                prompts = Path(tmp) / "prompts"
                prompts.mkdir()
                for name in ("manager", "scientist", "engineer", "executor", "supervisor"):
                    (prompts / (name + "_prompt.txt")).write_text("离线测试", encoding="utf8")
                inputs = iter(["本地分类"])
                def input_text(*args):
                    try:
                        return next(inputs)
                    except StopIteration:
                        raise EOFError
                previous = Path.cwd()
                try:
                    os.chdir(tmp)
                    with patch("core.session.Session", return_value=session), \
                         patch("agents.user_agent.UserAgent", return_value=user), \
                         patch("agents.manager_agent.ManagerAgent", return_value=manager_agent), \
                         patch("agents.scientist_agent.ScientistAgent", return_value=scientist), \
                         patch("agents.engineer_agent.EngineerAgent", return_value=engineer_agent), \
                         patch("agents.executor_agent.ExecutorAgent", return_value=executor), \
                         patch("agents.supervisor_agent.SupervisorAgent", return_value=supervisor), \
                         patch("tools.io_kit.print_data_summary"), \
                         patch("tools.io_kit.scan_data_directory", return_value={"success": True, "data": {"files": [], "file_count": 0}}), \
                         patch("builtins.input", side_effect=input_text), \
                         patch("sys.stdout", new=io.StringIO()):
                        main.run_cli()
                finally:
                    os.chdir(previous)
                if limit == 1:
                    self.assertEqual(repairs, [])
                    self.assertEqual(reports, [])
                    self.assertTrue(session.is_terminated())
                else:
                    self.assertEqual(len(repairs), 1)
                    self.assertEqual(len(reports), 1)
                    self.assertFalse(session.is_terminated())
                    self.assertEqual(session.get_current_phase(), "完成")
                    self.assertEqual(session.result_audit["errors"], [])
                    self.assertEqual(len(reports[0].metadata["files_generated"]), 2)



if __name__ == "__main__":
    unittest.main()
