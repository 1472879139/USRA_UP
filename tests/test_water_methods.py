"""两种水体方法与输入选路的工程测试，不使用参考标签或模型。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import rasterio

from tests.test_benchmarks import raster
from tools.artifact_paths import file_hash


class MethodTests(unittest.TestCase):
    def test_two_methods_share_a_plan_without_changing_user_requirements(self):
        from tests.test_water_agent import sample, session_for
        from tools.water_workflow import select_water_tool, run_water_workflow
        from tools.result_audit import audit_execution_results
        from tools.water_audit import attach_water_audit
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root)
            yy,xx = np.mgrid[:64,:64];green = np.where(xx<32,100,180).astype("uint16")
            swir = np.where(xx<32,400,120).astype("uint16")
            image = raster(root/"data/water_02/image.tif",np.stack([green,green,green,swir,swir,swir]),("B2","B3","B4","B8","B11","B12"))
            (image.parent/"metadata.json").write_text(json.dumps({"dataset":"合成Sentinel2","bands":["B2","B3","B4","B8","B11","B12"],"encoding":"反射率DN/10000"}),encoding="utf-8")
            session = session_for(root); original = session.user_request
            plan = select_water_tool(session,policy="auto");session.tool_plan=plan
            self.assertEqual(plan["name"],"water_mndwi_auto")
            self.assertEqual([row["method"] for row in plan["inputs"]],["mndwi_zero","mndwi_otsu"])
            with patch("tools.water_workflow.Path.cwd",return_value=root):
                run_water_workflow(plan,root/session.result_relative_dir)
            files=[p.relative_to(root/"results").as_posix() for p in (root/session.result_relative_dir).rglob("*") if p.is_file()]
            session.result_audit=audit_execution_results(root/"results",files,session.user_request,task_subdir=session.result_subdir)
            attach_water_audit(session,root)
            self.assertEqual(session.user_request,original)
            self.assertEqual(len(session.result_audit["scenes"]),2)
            manifest_path=root/session.result_relative_dir/"water_workflow.json"
            manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
            entry=manifest["scenes"][1];prediction=root/session.result_relative_dir/entry["prediction"]
            with rasterio.open(prediction,"r+") as ds:
                data=ds.read(1);data[0,0]=1-data[0,0];ds.write(data,1)
            entry["prediction_sha256"]=file_hash(prediction)
            manifest_path.write_text(json.dumps(manifest),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"阈值"):
                attach_water_audit(session,root)

    def test_router_ignores_filenames_and_rejects_a_forged_selection(self):
        from tests.test_water_agent import sample,session_for
        from tools.water_workflow import select_water_tool,run_water_workflow
        with TemporaryDirectory() as tmp:
            root=Path(tmp);sample(root);session=session_for(root)
            plan=select_water_tool(session,policy="auto")
            plan["inputs"][0]["selection"]["reason"]="伪造的理由"
            with patch("tools.water_workflow.Path.cwd",return_value=root):
                with self.assertRaisesRegex(ValueError,"选路事实"):
                    run_water_workflow(plan,root/session.result_relative_dir)

    def test_otsu_separates_two_clusters_and_reports_no_accuracy(self):
        from tools.water_methods import otsu_diagnostics, choose_from_diagnostics
        index = np.r_[np.full(2048, -.6), np.full(2048, .2)].reshape(64, 64)
        result = otsu_diagnostics(index, np.ones(index.shape, dtype=bool))
        self.assertGreater(result["threshold"], -.6)
        self.assertLess(result["threshold"], .2)
        self.assertGreater(result["separability"], .99)
        self.assertEqual(choose_from_diagnostics(result)["method"], "mndwi_otsu")
        self.assertNotIn("precision", result)

    def test_empty_constant_small_or_outside_domain_data_keep_baseline(self):
        from tools.water_methods import otsu_diagnostics, choose_from_diagnostics
        for data in (np.zeros((64, 64)), np.full((64, 64), np.nan),
                     np.array([[-.5, .5]]), np.r_[np.full(2048, -2.), np.full(2048, .2)].reshape(64, 64)):
            with self.subTest(shape=data.shape):
                diag = otsu_diagnostics(data, np.isfinite(data))
                self.assertEqual(choose_from_diagnostics(diag)["method"], "mndwi_zero")

    def test_weak_or_unstable_input_groups_do_not_trigger_the_candidate(self):
        from tools.water_methods import choose_from_diagnostics
        good = {"threshold": -.1, "separability": .9, "minority_fraction": .2, "threshold_shift": .01}
        for changes in ({"separability": .6}, {"minority_fraction": .001}, {"threshold_shift": .2}):
            self.assertEqual(choose_from_diagnostics({**good, **changes})["method"], "mndwi_zero")

    def test_promotion_rejects_regressions_and_missing_trials(self):
        from benchmarks.compare import promotion_result
        protocol = {"development_cases": ["old"], "samples": [{"id": "new"}],
                    "promotion_gate": {"new_macro_f1_min_improvement": .02,
                                       "max_individual_f1_drop_all_cases": .05}}
        records = [{"case": cid, "policy": policy, "phrase_index": 1,
                    "evaluation": {"status": "scored", "scenes": [{"scores": {"f1": score}}]}}
                   for cid in ("old", "new") for policy, score in (("mndwi_zero", .8), ("auto", .85))]
        report = {"records": records, "recorded_trials": 4, "expected_trials": 4}
        self.assertTrue(promotion_result(report, protocol)["passed"])
        records[1]["evaluation"]["scenes"][0]["scores"]["f1"] = .7
        self.assertFalse(promotion_result(report, protocol)["passed"])
        records[1]["evaluation"]["scenes"][0]["scores"]["f1"] = .85
        report["expected_trials"] = 5
        self.assertFalse(promotion_result(report, protocol)["passed"])

    def test_alternative_keeps_nodata_grid_and_statistical_consistency(self):
        from tools.water_methods import run_method
        with TemporaryDirectory() as tmp:
            root = Path(tmp); yy, xx = np.mgrid[:64, :64]
            green = np.where(xx<32, 100, 180).astype("uint16"); swir = np.where(xx<32, 400, 120).astype("uint16")
            green[0, 0] = swir[0, 0] = 0
            image = raster(root/"input.tif", np.stack([np.ones_like(green), green, np.ones_like(green),
                            np.ones_like(green), swir, np.ones_like(green)]), ("B2", "B3", "B4", "B8", "B11", "B12"))
            before = file_hash(image)
            with patch("benchmarks.catalog.load_catalog", side_effect=AssertionError("预测不能读取答案清单")):
                result = run_method("mndwi_otsu", image, root/"output")
            with rasterio.open(root/"output/water_mask.tif") as ds, rasterio.open(image) as src:
                mask = ds.read(1)
                self.assertEqual(ds.transform, src.transform)
                self.assertEqual(ds.crs, src.crs)
            self.assertEqual(int(mask[0, 0]), 255)
            self.assertEqual(result["water_pixels"], int((mask==1).sum()))
            self.assertEqual(result["valid_pixels"], 4095)
            self.assertEqual(result["tool_version"], "mndwi-otsu-v1")
            self.assertEqual(file_hash(image), before)
            self.assertTrue((root/"output/preview.png").is_file())

    def test_baseline_registry_calls_unchanged_implementation(self):
        from tools.water_methods import run_method
        from tools.water_tool import run_water_baseline
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = raster(root/"input.tif", np.array([[[100, 180]], [[400, 120]], [[1, 1]], [[2, 2]]], dtype="uint16"),
                           ("B3", "B11", "B4", "B2"))
            run_method("mndwi_zero", image, root/"one")
            run_water_baseline(image, root/"two")
            self.assertEqual(file_hash(root/"one/water_mask.tif"), file_hash(root/"two/water_mask.tif"))
            with self.assertRaisesRegex(ValueError, "方法"):
                run_method("invented", image, root/"bad")


if __name__ == "__main__":
    unittest.main()
