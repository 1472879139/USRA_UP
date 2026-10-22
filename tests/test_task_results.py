"""Each analysis task owns one output folder, including its repair attempts."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from agents.engineer_agent import EngineerAgent
from agents.executor_agent import ExecutorAgent
from agents.supervisor_agent import SupervisorAgent
from core.message import Message, MessageType
from core.session import Session


class TaskResultsTests(unittest.TestCase):
    def test_tasks_started_in_same_second_have_distinct_output_folders(self):
        first, second = Session("same-id"), Session("same-id")
        self.assertNotEqual(getattr(first, "result_subdir", None), getattr(second, "result_subdir", None))

    def test_engineer_initial_and_repair_prompts_keep_same_task_folder(self):
        session = Session()
        session.start()
        engineer = object.__new__(EngineerAgent)
        engineer.name, engineer.session = "Engineer", session
        prompts = []
        engineer._call_llm_with_memory = lambda prompt: prompts.append(prompt) or "print('ok')"
        engineer._sanitize_response = lambda text: text
        engineer.handle_message(Message(sender="Scientist", receiver="Engineer", content="分析计划"))
        engineer.handle_message(Message(sender="SupervisorAgent", receiver="Engineer", content="修复图片"))
        for prompt in prompts:
            self.assertIn("URSA_RESULTS_DIR", prompt)
            self.assertIn(session.result_subdir, prompt)

    def test_executor_provides_task_folder_and_keeps_retries_together(self):
        with TemporaryDirectory() as tmp:
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test", code_timeout=20)
            executor._project_root = tmp
            executor.session = Session()
            executor.session.start()
            for filename in ("map.txt", "repaired.txt"):
                code = ("import os\nfrom pathlib import Path\n"
                        "folder = Path(os.environ['URSA_RESULTS_DIR'])\n"
                        f"(folder / '{filename}').write_text('result', encoding='utf-8')\nprint(folder)")
                response = executor.handle_message(Message(sender="Engineer", receiver="Executor", content=code, msg_type=MessageType.CODE))
                self.assertEqual(response.msg_type, MessageType.RESULT, response.content)
                expected = executor.session.result_subdir + "/" + filename
                self.assertIn(expected, response.metadata["files_generated"])
            folder = Path(tmp) / "results" / executor.session.result_subdir
            self.assertTrue((folder / "map.txt").is_file())
            self.assertTrue((folder / "repaired.txt").is_file())

    def test_executor_does_not_accept_outputs_written_to_results_root(self):
        with TemporaryDirectory() as tmp:
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test", code_timeout=20)
            executor._project_root = tmp
            executor.session = Session()
            executor.session.start()
            response = executor.handle_message(Message(
                sender="Engineer", receiver="Executor", msg_type=MessageType.CODE,
                content="from pathlib import Path\nPath('results/mixed.txt').write_text('bad location', encoding='utf-8')",
            ))
            self.assertEqual(response.msg_type, MessageType.ERROR)
            self.assertIn("任务目录", response.content)

    def test_shared_io_tools_follow_executor_output_directory(self):
        from tools.io_kit import _ensure_results_dir
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "results" / "task_test"
            with patch("tools.io_kit._get_project_root", return_value=tmp), patch.dict(os.environ, {"URSA_RESULTS_DIR": str(folder)}):
                self.assertEqual(Path(_ensure_results_dir("indices")), folder / "indices")
                self.assertTrue((folder / "indices").is_dir())

    def test_supervisor_rejects_another_tasks_outputs(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "results" / "another_task"
            folder.mkdir(parents=True)
            (folder / "file.txt").write_text("other task", encoding="utf-8")
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor._project_root = tmp
            supervisor.session = Session()
            supervisor.session.start()
            response = supervisor.handle_message(Message(
                sender="Executor", receiver="Manager", content="成功", msg_type=MessageType.RESULT,
                metadata={"files_generated": ["another_task/file.txt"]},
            ))
            self.assertTrue(supervisor.session.is_terminated())
            self.assertEqual(response.msg_type, MessageType.ERROR)

    def test_supervisor_accepts_this_tasks_output_and_reports_actual_subfolder(self):
        with TemporaryDirectory() as tmp:
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor._project_root = tmp
            supervisor.session = Session()
            supervisor.session.start()
            folder = Path(tmp) / "results" / supervisor.session.result_subdir
            folder.mkdir(parents=True)
            (folder / "report.txt").write_text("result", encoding="utf-8")
            response = supervisor.handle_message(Message(
                sender="Executor", receiver="Manager", content="成功", msg_type=MessageType.RESULT,
                metadata={"files_generated": [supervisor.session.result_subdir + "/report.txt"]},
            ))
            self.assertIsNone(response)
            self.assertTrue(supervisor.session.is_active())
            self.assertIn(supervisor.session.result_relative_dir + "/report.txt", supervisor.session.result_audit["facts"])


if __name__ == "__main__":
    unittest.main()
