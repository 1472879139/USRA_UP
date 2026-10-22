"""Execution artifacts and report consistency, with no model/network requests."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from PIL import Image

from agents.manager_agent import ManagerAgent
from agents.supervisor_agent import SupervisorAgent
from agents.executor_agent import ExecutorAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from tests.test_ui_updates import make_view


def make_outputs(root, water_count=1, water_area=0.0001, subdir=None):
    results = Path(root) / "results"
    if subdir:
        results = results / subdir
    results.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(results / "final_map_landcover_test.jpg")
    with rasterio.open(results / "final_map_landcover_test.tif", "w", driver="GTiff",
                       width=2, height=2, count=1, dtype="uint8", nodata=0,
                       crs="EPSG:32650", transform=from_origin(447020, 4425040, 10, 10)) as dst:
        dst.write(np.array([[1, 2], [2, 4]], dtype="uint8"), 1)
    (results / "landcover_stats_test.csv").write_text(
        "类别,像元数,面积_km2,占比_%\n"
        f"水体,{water_count},{water_area},25\n植被,2,0.0002,50\n其他,1,0.0001,25\n",
        encoding="utf-8-sig",
    )
    return results, [(Path(subdir) / p.name).as_posix() if subdir else p.name for p in results.iterdir()]


class ResultAuditTests(unittest.TestCase):
    def test_executor_result_reaches_supervisor_validation(self):
        with TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            results.mkdir()
            (results / "old.jpg").touch()
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor._project_root = tmp
            supervisor.session = Session()
            supervisor.session.start()
            response = supervisor.handle_message(Message(
                sender="Executor", receiver="Manager", content="执行成功",
                msg_type=MessageType.RESULT, metadata={"files_generated": []},
            ))
            self.assertIsNotNone(response)
            self.assertTrue(supervisor.session.is_terminated())

    def test_real_statistics_match_raster_but_semantics_stay_unverified(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            audit = audit_execution_results(results, names, "输出 JPG")
            self.assertEqual(audit["errors"], [])
            self.assertEqual(audit["status"], "needs_review")
            self.assertEqual(audit["statistics"][1]["class_name"], "植被")
            self.assertIn("植被", audit["facts"])
            self.assertIn("最高", audit["facts"])

    def test_csv_pixel_count_disagreement_is_not_accepted(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp, water_count=2)
            audit = audit_execution_results(results, names)
            self.assertEqual(audit["status"], "failed")
            self.assertTrue(any("像元" in problem for problem in audit["errors"]))

    def test_area_disagreement_is_not_accepted(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp, water_area=9.2)
            audit = audit_execution_results(results, names)
            self.assertEqual(audit["status"], "failed")
            self.assertTrue(any("面积" in problem for problem in audit["errors"]))

    def test_explicit_class_ids_are_checked_not_just_total_counts(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            (results / "landcover_stats_test.csv").write_text(
                "类别编号,类别,像元数,面积_km2,占比_%\n"
                "2,水体,1,0.0001,25\n1,植被,2,0.0002,50\n4,其他,1,0.0001,25\n", encoding="utf-8-sig")
            audit = audit_execution_results(results, names)
            self.assertEqual(audit["status"], "failed")
            self.assertTrue(any("编号" in error for error in audit["errors"]))

    def test_missing_class_ids_do_not_imply_verified_label_correspondence(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            audit = audit_execution_results(results, names)
            self.assertTrue(any("编号" in warning for warning in audit["warnings"]))

    def test_existing_unlisted_results_do_not_count_as_this_run(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, _ = make_outputs(tmp)
            audit = audit_execution_results(results, [], "输出 JPG")
            self.assertEqual(audit["status"], "failed")

    def test_requested_jpg_must_exist_in_this_run(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            names = [name for name in names if not name.endswith(".jpg")]
            audit = audit_execution_results(results, names, "输出 jpg")
            self.assertEqual(audit["status"], "failed")

    def test_png_content_renamed_to_jpg_is_not_a_jpeg(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            Image.new("RGB", (8, 8), "white").save(results / "final_map_landcover_test.jpg", format="PNG")
            self.assertEqual(audit_execution_results(results, names, "输出 JPG")["status"], "failed")

    def test_chinese_attached_jpg_requirement_still_requires_image(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            names = [name for name in names if not name.endswith(".jpg")]
            self.assertEqual(audit_execution_results(results, names, "输出JPG图片")["status"], "failed")

    def test_truncated_jpeg_is_not_accepted_as_readable_output(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            path = results / "final_map_landcover_test.jpg"
            Image.new("RGB", (128, 128), "red").save(path)
            path.write_bytes(path.read_bytes()[:-20])
            self.assertEqual(audit_execution_results(results, names)["status"], "failed")

    def test_same_csv_basename_in_separate_subfolders_is_not_merged(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            names.remove("landcover_stats_test.csv")
            for folder in ("scheme_a", "scheme_b"):
                (results / folder).mkdir()
                (results / folder / "stats.csv").write_bytes((results / "landcover_stats_test.csv").read_bytes())
                names.append(folder + "/stats.csv")
            self.assertEqual(audit_execution_results(results, names)["errors"], [])

    def test_duplicate_class_ids_do_not_pass_by_reusing_same_raster_label(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            (results / "landcover_stats_test.csv").write_text(
                "类别编号,类别,像元数,面积_km2,占比_%\n"
                "1,水体,1,0.0001,25\n2,植被,2,0.0002,50\n1,其他,1,0.0001,25\n", encoding="utf-8-sig")
            self.assertEqual(audit_execution_results(results, names)["status"], "failed")

    def test_reported_area_rounding_does_not_create_false_failure(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            path = results / "landcover_stats_test.csv"
            text = path.read_text(encoding="utf-8-sig").replace("0.0001", "0.000").replace("0.0002", "0.000")
            path.write_text(text, encoding="utf-8-sig")
            self.assertEqual(audit_execution_results(results, names)["errors"], [])

    def test_cancelling_images_in_later_feedback_does_not_require_old_format(self):
        from tools.result_audit import audit_execution_results
        with TemporaryDirectory() as tmp:
            results, names = make_outputs(tmp)
            names = [name for name in names if not name.endswith(".jpg")]
            self.assertNotEqual(audit_execution_results(results, names, "输出 JPG\n不需要图片，只要 CSV")["status"], "failed")

    def test_report_rank_check_does_not_confuse_adjacent_categories(self):
        from tools.result_audit import report_conflicts
        audit = {"statistics": [{"class_name": "植被", "percent": 28.72},
                                 {"class_name": "其他", "percent": 36.91}]}
        self.assertEqual(report_conflicts("植被其次，其他占比最高。", audit), [])

    def test_overwritten_result_is_tracked_by_executor(self):
        with TemporaryDirectory() as tmp:
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test", code_timeout=20)
            executor._project_root = tmp
            executor.session = Session()
            executor.session.start()
            results = Path(tmp) / "results" / executor.session.result_subdir
            results.mkdir(parents=True)
            (results / "existing.txt").write_text("old", encoding="utf-8")
            result = executor.handle_message(Message(
                sender="Engineer", receiver="Executor", msg_type=MessageType.CODE,
                content="import os\nfrom pathlib import Path\n(Path(os.environ['URSA_RESULTS_DIR']) / 'existing.txt').write_text('new result', encoding='utf-8')\nprint('ok')",
            ))
            self.assertEqual(result.msg_type, MessageType.RESULT)
            self.assertIn(executor.session.result_subdir + "/existing.txt", result.metadata.get("files_generated", []))

    def test_manager_does_not_publish_highest_share_claim_that_conflicts_with_table(self):
        session = Session()
        session.start()
        session.result_audit = {
            "status": "needs_review", "warnings": ["语义待复核"], "errors": [],
            "statistics": [
                {"class_name": "植被", "percent": 28.72, "pixels": 120569, "area_km2": 12.0569, "scheme": "默认"},
                {"class_name": "其他", "percent": 36.91, "pixels": 154948, "area_km2": 15.4948, "scheme": "默认"},
            ], "facts": "核验统计：其他占比最高（36.91%）；类别语义待复核。",
        }
        manager = object.__new__(ManagerAgent)
        manager.name, manager.session = "Manager", session
        manager._call_llm = lambda prompt: "植被占比最高（28.72%）。"
        response = manager.handle_message(Message(sender="Executor", receiver="Manager", content="执行完成", msg_type=MessageType.RESULT))
        self.assertNotIn("植被占比最高", response.content)
        self.assertIn("其他占比最高", response.content)
        self.assertIn("待复核", response.content)

    def test_ui_completion_distinguishes_results_needing_review(self):
        view = make_view()
        view._running = False
        view.session.state_machine.force_transition(ConversationPhase.COMPLETED)
        view.session.result_audit = {"status": "needs_review"}
        self.assertEqual(view.get_updates()["status"], "分析结束（结果待复核）")

    def test_gui_does_not_report_success_when_artifact_audit_fails(self):
        with TemporaryDirectory() as tmp:
            view = make_view()
            _, names = make_outputs(tmp, water_count=2, subdir=view.session.result_subdir)
            view.config = {"executor_max_retries": 1}
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = view.session, tmp
            supervisor.select_relevant_files = lambda *args: {"relevant_files": []}
            reports = []
            with patch("tools.io_kit.scan_data_directory", return_value={"data": {"files": []}}):
                view._run_analysis_pipeline(
                    manager=SimpleNamespace(handle_message=lambda message: reports.append(message)),
                    scientist=SimpleNamespace(handle_message=lambda message: Message(sender="Scientist", receiver="Engineer", content="计划")),
                    engineer=SimpleNamespace(handle_message=lambda message: Message(sender="Engineer", receiver="Executor", content="code")),
                    executor=SimpleNamespace(handle_message=lambda message: Message(sender="Executor", receiver="Manager", content="成功", msg_type=MessageType.RESULT, metadata={"files_generated": names})),
                    supervisor=supervisor, sm=view.session.state_machine,
                )
            self.assertEqual(reports, [])
            self.assertTrue(view.session.is_terminated())
            self.assertTrue(any(event["type"] == "error" for event in view.ui_queue.queue))


if __name__ == "__main__":
    unittest.main()
