"""评价答案只用于Supervisor，拒绝猜配对、丢弃未确认区与伪造精度。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from tools.artifact_paths import file_hash
from tools.water_evaluation import evaluate_known_prediction
from tests.test_benchmarks import raster


class IndependentWaterTests(unittest.TestCase):
    def test_malformed_tool_manifest_has_a_diagnostic_error(self):
        from core.session import Session
        from tools.water_evaluation import attach_water_evaluation
        with TemporaryDirectory() as tmp:
            root = Path(tmp); session = Session()
            session.tool_plan = {"name": "water_mndwi_zero", "inputs": []}
            output = root/session.result_relative_dir
            output.mkdir(parents=True)
            (output/"water_workflow.json").write_text("[]", encoding="utf-8")
            session.result_audit = {"files": [session.result_subdir+"/water_workflow.json"]}
            with self.assertRaisesRegex(ValueError, "清单"):
                attach_water_evaluation(session, root)

    def test_unknown_input_never_reads_a_reference_even_if_filename_matches(self):
        with patch("benchmarks.catalog.reference_root", side_effect=AssertionError("不应读取未配对答案")):
            result = evaluate_known_prediction(Path.cwd(), "not-a-known-hash", Path("water_01_water_mask.tif"))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("scores", result)

    def test_known_input_is_scored_and_unconfirmed_water_is_not_dropped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = raster(root/"answers/water.tif", np.array([[1, 1], [1, 0]], dtype="uint8"), nodata=0)
            pred = raster(root/"prediction.tif", np.array([[1, 255], [0, 0]], dtype="uint8"), nodata=255)
            (root/"benchmark.local.json").write_text(json.dumps({"reference_root": "answers"}), encoding="utf-8")
            case = {"enabled": True, "id": "synthetic", "inputs": [{"sha256": "input-fingerprint"}],
                    "reference": {"path": "water.tif", "sha256": file_hash(ref)}, "source": "https://example.invalid"}
            with patch("benchmarks.catalog.load_catalog", return_value=[case]):
                result = evaluate_known_prediction(root, "input-fingerprint", pred)
                self.assertEqual(result["status"], "incomplete")
                self.assertAlmostEqual(result["scores"]["recall"], 1/3)
                self.assertEqual(result["scores"]["missing_water"], 1)
                case["reference"]["sha256"] = "changed"
                result = evaluate_known_prediction(root, "input-fingerprint", pred)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("scores", result)

    def test_two_tables_are_both_in_plain_delivery_without_a_tool_manifest(self):
        from core.session import Session
        from tools.result_delivery import build_result_delivery
        session = Session(); session.start()
        session.task_contract = {"task_type": "segmentation"}
        session.result_audit = {"status": "needs_review", "files": [], "statistics": [
            {"source": "one.csv", "scheme": "default", "class_name": "水体", "percent": 1.08},
            {"source": "two.csv", "scheme": "default", "class_name": "水体", "percent": 28.99}]}
        result = build_result_delivery(session, Path.cwd())
        # 没有可交付文件时仍失败，但不能把第一张表冒充两张表的整体结果。
        self.assertIn("one.csv", "".join(result["findings"]))
        self.assertIn("two.csv", "".join(result["findings"]))

    def test_plain_delivery_does_not_infer_missed_water_from_legacy_quality_flags(self):
        from core.session import Session
        from tools.result_delivery import build_result_delivery
        from PIL import Image
        with TemporaryDirectory() as tmp:
            root = Path(tmp); session = Session(); session.start()
            output = root/session.result_relative_dir; output.mkdir(parents=True)
            Image.new("RGB", (4, 4)).save(output/"final.png")
            session.task_contract = {"task_type": "segmentation"}
            session.result_audit = {"status": "needs_review", "files": [session.result_subdir+"/final.png"],
                                    "scenes": [{"label": "合成样本", "findings": ["漏检明显"],
                                                "quality_flags": ["majority_water_unconfirmed"],
                                                "evaluation": {"scores": {"missing_predictions": 0}}}]}
            result = build_result_delivery(session, root)
            self.assertNotIn("漏掉多数", result["headline"])
            self.assertIn("尚未验证", result["verification"])
            self.assertEqual(result["completion"], "candidate")

    def test_missing_artifacts_cannot_be_overridden_by_scene_quality_messages(self):
        from core.session import Session
        from tools.result_delivery import build_result_delivery
        session = Session(); session.start()
        session.task_contract = {"task_type": "segmentation"}
        session.result_audit = {"status": "needs_review", "files": [],
                                "scenes": [{"label": "合成样本", "findings": ["漏检明显"],
                                            "quality_flags": ["majority_water_unconfirmed"],
                                            "evaluation": {"scores": {}}}]}
        result = build_result_delivery(session, Path.cwd())
        self.assertEqual(result["completion"], "failed")
        self.assertIn("没有找到", result["headline"])
        self.assertEqual(result["scene_results"], [])


if __name__ == "__main__":
    unittest.main()
