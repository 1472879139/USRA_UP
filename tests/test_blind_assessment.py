"""闭卷处理与外部评分：使用临时合成数据，不调用收费模型。"""
import json
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np

from agents.supervisor_agent import SupervisorAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase
from tests.test_water_agent import sample, session_for
from tests.test_benchmarks import raster
from tools.artifact_paths import file_hash
from tools.result_delivery import build_result_delivery


def completed(root):
    from tools.water_workflow import select_water_tool, run_water_workflow
    session = session_for(root, "帮我圈出water_01的水面并统计面积", revisions=[])
    session.revision_feedback = []
    session.tool_plan = select_water_tool(session)
    with patch("tools.water_workflow.Path.cwd", return_value=root):
        with contextlib.redirect_stdout(io.StringIO()):
            run_water_workflow(session.tool_plan, root/session.result_relative_dir)
    folder = root/session.result_relative_dir
    files = [p.relative_to(root/"results").as_posix() for p in folder.rglob("*") if p.is_file()]
    supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
    supervisor.session, supervisor._project_root = session, str(root)
    message = Message(sender="Executor", receiver="Manager", content="完成", msg_type=MessageType.RESULT,
                      metadata={"files_generated": files})
    supervisor.handle_message(message)
    session.result_delivery = build_result_delivery(session, root, report="候选水面，准确性尚未独立验证。")
    session.state_machine.force_transition(ConversationPhase.COMPLETED)
    return session


class BlindRuntimeTests(unittest.TestCase):
    def test_manager_discards_an_invented_accuracy_score(self):
        from agents.manager_agent import ManagerAgent
        from core.session import Session
        session = Session(); session.start(); session.user_request = "找水体"
        session.result_audit = {"status": "needs_review", "facts": "本次只检查统计，识别精度未知。"}
        manager = ManagerAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
        manager.session = session; manager._call_llm = Mock(return_value="水体召回率99.9%，已与人工答案核对。")
        response = manager.handle_message(Message(sender="Executor", receiver="Manager", content="执行结束"))
        self.assertNotIn("99.9", response.content)
        self.assertIn("无独立依据", response.content)

    def test_normal_supervisor_never_reads_answers_even_if_configured(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root); sample(root, "water_02")
            (root/"benchmark.local.json").write_text("这份答案配置故意不可解析", encoding="utf-8")
            with patch("benchmarks.catalog.load_catalog", side_effect=AssertionError("生产端不能加载评分清单")):
                session = completed(root)
            self.assertFalse(session.is_terminated())
            self.assertEqual(len(session.result_delivery["scene_results"]), 2)
            for scene in session.result_delivery["scene_results"]:
                self.assertNotIn("scores", scene["evaluation"])
            self.assertIn("尚未", session.result_delivery["verification"])
            self.assertNotIn("人工答案对比", session.result_audit["facts"])

    def test_changing_answers_does_not_change_runtime_facts_or_prediction(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root); sample(root, "water_02")
            answer = raster(root/"answers/water.tif", np.zeros((2, 2), dtype="uint8"))
            (root/"benchmark.local.json").write_text(json.dumps({"reference_root": "answers"}), encoding="utf-8")
            first = completed(root)
            raster(answer, np.ones((2, 2), dtype="uint8"))
            second = completed(root)
            a, b = first.result_audit["scenes"][0], second.result_audit["scenes"][0]
            self.assertEqual(a["findings"], b["findings"])
            self.assertEqual(file_hash(root/"results"/a["prediction_path"]), file_hash(root/"results"/b["prediction_path"]))


class SubmissionTests(unittest.TestCase):
    def setup_case(self, root):
        sample(root); sample(root, "water_02")
        answer = raster(root/"answers/water.tif", np.array([[1, 0], [1, 0]], dtype="uint8"), nodata=0)
        image = root/"data/water_01/image.tif"
        case = {"id": "synthetic", "enabled": True, "inputs": [{"sha256": file_hash(image)}],
                "source": "https://example.invalid/synthetic",
                "reference": {"path": "water.tif", "sha256": file_hash(answer)}}
        (root/"benchmark.local.json").write_text(json.dumps({"reference_root": "answers"}), encoding="utf-8")
        return case

    def test_freeze_then_grade_never_changes_the_agent_submission(self):
        from tools.submission import freeze_submission, verify_submission
        from benchmarks.assessment import grade_submission
        with TemporaryDirectory() as tmp:
            root = Path(tmp); case = self.setup_case(root)
            session = completed(root); before = json.dumps(session.result_delivery, ensure_ascii=False)
            receipt = freeze_submission(session, root)
            folder = root/receipt["directory"]
            manifest = verify_submission(folder, receipt["sha256"])
            self.assertNotIn("reference_root", json.dumps(manifest))
            self.assertNotIn("scores", manifest["delivery"]["scene_results"][0]["evaluation"])
            with patch("benchmarks.catalog.load_catalog", return_value=[case]):
                report = grade_submission(folder, receipt["sha256"], root)
            self.assertEqual(report["scenes"][0]["scores"]["f1"], 1)
            self.assertEqual(json.dumps(session.result_delivery, ensure_ascii=False), before)
            self.assertEqual(freeze_submission(session, root), receipt)

    def test_modified_prediction_or_manifest_is_rejected(self):
        from tools.submission import freeze_submission, verify_submission
        with TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_case(root); session = completed(root)
            receipt = freeze_submission(session, root); folder = root/receipt["directory"]
            manifest = verify_submission(folder, receipt["sha256"])
            prediction = folder/manifest["scenes"][0]["prediction"]
            prediction.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "哈希"):
                verify_submission(folder, receipt["sha256"])
            (folder/"submission.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "哈希"):
                verify_submission(folder, receipt["sha256"])

    def test_active_or_unconfirmed_session_cannot_be_frozen(self):
        from tools.submission import freeze_submission
        from core.session import Session
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "结束"):
                freeze_submission(Session(), tmp)

    def test_failed_task_is_retained_with_no_accuracy_score(self):
        from tools.submission import freeze_submission
        from benchmarks.assessment import grade_submission
        from core.session import Session
        with TemporaryDirectory() as tmp:
            root = Path(tmp); session = Session(); session.start(); session.user_request = "提取水体"
            session.terminate("缺少输入")
            receipt = freeze_submission(session, root)
            with patch("benchmarks.catalog.load_catalog", side_effect=AssertionError("失败任务不评分")):
                report = grade_submission(root/receipt["directory"], receipt["sha256"], root)
            self.assertEqual(report["status"], "not_completed")
            self.assertEqual(report["scenes"], [])

    def test_input_change_prevents_freezing_an_outdated_plan(self):
        from tools.submission import freeze_submission
        with TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_case(root); session = completed(root)
            (root/"data/water_01/image.tif").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "输入"):
                freeze_submission(session, root)

    def test_sealed_trace_keeps_model_usage_without_client_secrets(self):
        from tools.submission import freeze_submission, verify_submission
        with TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_case(root); session = completed(root)
            session.tracer.add_event("llm_call", agent="Manager", data={"model": "test-model", "total_tokens": 12,
                                                                       "api_key": "SECRET_MARKER", "prompt": "PRIVATE_PROMPT"})
            receipt = freeze_submission(session, root)
            manifest = verify_submission(root/receipt["directory"], receipt["sha256"])
            event = manifest["execution_trace"][-1]
            self.assertEqual(event["data"]["model"], "test-model")
            self.assertEqual(event["data"]["total_tokens"], 12)
            self.assertNotIn("SECRET_MARKER", json.dumps(manifest))
            self.assertNotIn("PRIVATE_PROMPT", json.dumps(manifest))

    def test_missing_artifact_is_preserved_as_not_completed(self):
        from tools.submission import freeze_submission, verify_submission
        from benchmarks.assessment import grade_submission
        with TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_case(root); session = completed(root)
            preview = root/"results"/session.result_audit["scenes"][0]["preview_path"]
            preview.unlink()
            receipt = freeze_submission(session, root)
            manifest = verify_submission(root/receipt["directory"], receipt["sha256"])
            self.assertTrue(manifest["missing_artifacts"])
            self.assertEqual(grade_submission(root/receipt["directory"], receipt["sha256"], root)["status"], "not_completed")

    def test_clean_runtime_has_no_evaluator_secrets_or_history(self):
        from benchmarks.prepare import prepare_runtime
        source = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            destination = Path(tmp)/"runtime"
            # 复制器本身只测源码结构，输入改用临时合成目录，避免依赖私有影像。
            small_root = Path(tmp)/"source"; small_root.mkdir()
            for folder in ("agents", "core", "tools", "gui", "prompts"):
                import shutil
                shutil.copytree(source/folder, small_root/folder, ignore=shutil.ignore_patterns("__pycache__"))
            for filename in ("main.py", "environment.yml", "requirements.txt", "config.example.json"):
                shutil.copy2(source/filename, small_root/filename)
            sample(small_root)
            (small_root/"config.json").write_text("SECRET_MARKER")
            (small_root/"data/.engineer_memory.json").write_text("ANSWER_HISTORY")
            prepare_runtime(small_root, ["water_01/image.tif"], destination)
            self.assertFalse((destination/"benchmarks").exists())
            self.assertFalse((destination/"benchmark.local.json").exists())
            self.assertFalse((destination/"config.json").exists())
            self.assertFalse((destination/"data/.engineer_memory.json").exists())
            self.assertFalse((destination/"tools/water_evaluation.py").exists())
            self.assertTrue((destination/"tools/water_audit.py").is_file())
            with self.assertRaises(ValueError):
                prepare_runtime(small_root, ["water_01/image.tif"], destination)


class AssessmentApiTests(unittest.TestCase):
    def test_freeze_grade_and_download_do_not_mutate_the_conversation(self):
        from gui.app import create_app
        from tests.test_ui_updates import make_view
        with TemporaryDirectory() as tmp:
            root = Path(tmp); case = SubmissionTests().setup_case(root)
            view = make_view(); view._running = False; view.session = completed(root)
            app = create_app(controller=view); app.config["PROJECT_ROOT"] = root
            client = app.test_client(); before = json.dumps(view.session.result_delivery, ensure_ascii=False)
            frozen = client.post("/api/submissions/freeze").get_json()
            self.assertTrue(frozen["success"], frozen)
            with client.get(frozen["download"]) as response:
                self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/submissions/download/config.json").status_code, 404)
            with patch("benchmarks.catalog.load_catalog", return_value=[case]):
                response = client.post("/api/assessment/grade", json={"task_id": frozen["receipt"]["task_id"]})
                self.assertEqual(response.status_code, 200, response.get_json())
                report = response.get_json()
                self.assertEqual(report["report"]["status"], "scored")
            self.assertEqual(json.dumps(view.session.result_delivery, ensure_ascii=False), before)
            view._running = True
            self.assertEqual(client.post("/api/assessment/grade", json={"task_id": frozen["receipt"]["task_id"]}).status_code, 409)
            self.assertEqual(client.post("/api/submissions/freeze").status_code, 409)

    def test_unknown_task_cannot_trigger_grading_and_invalid_json_is_rejected(self):
        from gui.app import create_app
        from tests.test_ui_updates import make_view
        view = make_view(); view._running = False
        client = create_app(controller=view).test_client()
        self.assertEqual(client.post("/api/assessment/grade", json=[]).status_code, 400)
        self.assertEqual(client.post("/api/assessment/grade", json={"task_id": "unknown"}).status_code, 404)
        self.assertEqual(client.post("/api/submissions/freeze").status_code, 400)


if __name__ == "__main__":
    unittest.main()
