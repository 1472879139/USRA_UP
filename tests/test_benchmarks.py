"""验收工具使用合成数据，禁止依赖真实影像与模型余额。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from threading import RLock
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from benchmarks.metrics import binary_metrics, score_rasters
from benchmarks.water import run_water_baseline
from benchmarks.catalog import audit_case, file_hash


def raster(path, values, names=None, nodata=None, x=100):
    values = np.asarray(values)
    if values.ndim == 2:
        values = values[None]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=values.shape[2],
                       height=values.shape[1], count=len(values), dtype=values.dtype,
                       crs="EPSG:32647", transform=from_origin(x, 200, 10, 10),
                       nodata=nodata) as ds:
        ds.write(values)
        if names:
            ds.descriptions = names
    return path


class MetricsTests(unittest.TestCase):
    def test_missing_background_is_penalized_and_not_hidden(self):
        score = binary_metrics(np.array([255, 1]), np.array([0, 1]))
        self.assertEqual(score["missing_background"], 1)
        self.assertEqual(score["fp"], 1)
        self.assertEqual(score["precision"], .5)

    def test_confusion_and_missing_predictions_do_not_disappear(self):
        score = binary_metrics(np.array([1, 1, 0, 255]), np.array([1, 0, 1, 1]))
        self.assertEqual([score[k] for k in ("tp", "fp", "fn", "tn")], [1, 1, 2, 0])
        self.assertEqual(score["prediction_coverage"], .75)
        self.assertAlmostEqual(score["iou"], .25)
        self.assertEqual(score["missing_predictions"], 1)
        self.assertEqual(score["status"], "incomplete")

    def test_all_background_is_not_perfect_water_detection(self):
        score = binary_metrics(np.zeros(4), np.zeros(4))
        self.assertIsNone(score["precision"])
        self.assertIsNone(score["recall"])
        self.assertIsNone(score["f1"])

    def test_unknown_labels_are_rejected(self):
        with self.assertRaises(ValueError):
            binary_metrics(np.array([1]), np.array([2]))
        with self.assertRaises(ValueError):
            binary_metrics(np.array([8]), np.array([1]))

    def test_grid_mismatch_is_rejected_and_zero_label_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = raster(Path(tmp)/"p.tif", [[1, 0]], nodata=255)
            r = raster(Path(tmp)/"r.tif", [[0, 1]], nodata=0)
            score = score_rasters(p, r)
            self.assertEqual(score["fp"], 1)
            self.assertEqual(score["fn"], 1)
            raster(r, [[0, 1]], nodata=0, x=110)
            with self.assertRaisesRegex(ValueError, "网格"):
                score_rasters(p, r)


class WaterTests(unittest.TestCase):
    def test_nodata_threshold_boundary_and_geographic_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = raster(Path(tmp)/"input.tif", np.array([
                [[10, 0, 20]], [[10, 10, 10]], [[10, 10, 10]], [[10, 10, 10]]
            ], dtype="uint16"), ("B3", "B11", "B4", "B2"), nodata=0)
            with rasterio.open(image, "r+") as ds:
                ds.crs = "EPSG:4326"
            result = run_water_baseline(image, Path(tmp)/"out")
            with rasterio.open(Path(tmp)/"out/water_mask.tif") as ds:
                self.assertEqual(ds.read(1).tolist(), [[0, 255, 1]])
            self.assertIsNone(result["area_m2"])
            self.assertEqual(result["water_fraction"], .5)

    def test_named_bands_uint16_zero_denominator_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = raster(Path(tmp)/"input.tif", np.array([
                [[20, 10, 0]], [[10, 20, 0]], [[1, 1, 1]], [[3, 3, 3]]
            ], dtype="uint16"), ("B11", "B03", "B04", "B02"))
            output = Path(tmp)/"out"
            result = run_water_baseline(image, output)
            with rasterio.open(output/"water_mask.tif") as ds:
                self.assertEqual(ds.read(1).tolist(), [[0, 1, 255]])
            self.assertEqual(result["water_pixels"], 1)
            self.assertEqual(result["valid_pixels"], 2)
            self.assertEqual(result["area_m2"], 100)
            self.assertEqual(result["threshold"], 0)
            self.assertTrue((output/"preview.png").is_file())
            self.assertTrue((output/"statistics.csv").is_file())
            self.assertTrue((output/"method.json").is_file())

    def test_band_names_required_and_input_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = raster(Path(tmp)/"input.tif", np.ones((2, 2, 2), dtype="uint16"))
            before = file_hash(image)
            with self.assertRaisesRegex(ValueError, "波段"):
                run_water_baseline(image, Path(tmp)/"out")
            self.assertEqual(file_hash(image), before)


class CatalogTests(unittest.TestCase):
    def test_invalid_configuration_has_a_readable_error(self):
        from benchmarks.catalog import reference_root
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)/"benchmark.local.json"
            config.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON对象"):
                reference_root(tmp)

    def test_hash_and_missing_configuration_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = raster(root/"data/i.tif", [[0, 1]])
            case = {"id": "water_01", "inputs": [{"path": "data/i.tif", "sha256": file_hash(image)}],
                    "reference": {"path": "water_01/water.tif", "sha256": "unused"},
                    "metadata": None, "extras": []}
            status = audit_case(root, case, None)
            self.assertTrue(status["inputs_ok"])
            self.assertFalse(status["reference_ok"])
            image.write_bytes(b"changed")
            self.assertFalse(audit_case(root, case, None)["inputs_ok"])

    def test_path_escape_is_rejected(self):
        from benchmarks.catalog import confined_path
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                confined_path(Path(tmp), "../config.json")


class ApiTests(unittest.TestCase):
    def test_success_has_isolated_outputs_and_whitelisted_downloads(self):
        from gui.app import create_app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = raster(root/"data/input.tif", np.array([
                [[30, 10]], [[10, 20]], [[10, 10]], [[10, 10]]
            ], dtype="uint16"), ("B3", "B11", "B4", "B2"))
            answer = raster(root/"answers/water.tif", [[1, 0]], nodata=0)
            (root/"benchmark.local.json").write_text(json.dumps({"reference_root": "answers"}), encoding="utf-8-sig")
            case = {"id": "water_01", "enabled": True, "inputs": [{"path": "data/input.tif", "sha256": file_hash(image)}],
                    "metadata": None, "reference": {"path": "water.tif", "sha256": file_hash(answer)}, "extras": [],
                    "source": "https://example.org/source", "limitation": "合成测试", "reference_encoding": "0/1"}
            original_session = object()
            ctrl = SimpleNamespace(_ui_lock=RLock(), _running=False, _thread=None,
                                   session=original_session, start_analysis=unittest.mock.Mock())
            app = create_app(controller=ctrl)
            app.config["PROJECT_ROOT"] = root
            with patch("benchmarks.catalog.load_catalog", return_value=[case]), patch("benchmarks.runner.load_catalog", return_value=[case]):
                client = app.test_client()
                response = client.post("/api/benchmarks/run", json={"case_id": "water_01"})
                self.assertEqual(response.status_code, 200, response.get_json())
                report = response.get_json()["report"]
                self.assertEqual(report["scores"]["f1"], 1)
                output = root/report["result_directory"]
                self.assertTrue(output.is_relative_to(root/"results"))
                for name in report["artifacts"]:
                    with client.get(f"/api/benchmarks/artifact/{report['run_id']}/{name}") as download:
                        self.assertEqual(download.status_code, 200)
                self.assertEqual(client.get(f"/api/benchmarks/artifact/{report['run_id']}/config.json").status_code, 404)
                again = client.post("/api/benchmarks/run", json={"case_id": "water_01"}).get_json()["report"]
                self.assertNotEqual(report["run_id"], again["run_id"])
                self.assertFalse(ctrl._benchmark_running)
                self.assertIs(ctrl.session, original_session)
                ctrl.start_analysis.assert_not_called()

    def test_run_failure_releases_busy_flag(self):
        from gui.app import create_app
        ctrl = SimpleNamespace(_ui_lock=RLock(), _running=False, _thread=None)
        app = create_app(controller=ctrl)
        with patch("benchmarks.runner.run_case", side_effect=ValueError("模拟网格不一致")):
            response = app.test_client().post("/api/benchmarks/run", json={"case_id": "water_01"})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(ctrl._benchmark_running)

    def test_chat_cannot_start_during_fixed_tool_run(self):
        from tests.test_ui_updates import make_view
        view = make_view()
        view._running = False
        view._benchmark_running = True
        result = view.start_analysis("提取水体")
        self.assertFalse(result["success"])

    def test_busy_and_unknown_cases_do_not_run_or_call_model(self):
        from gui.app import create_app
        ctrl = SimpleNamespace(_ui_lock=RLock(), _running=True, _thread=None)
        app = create_app(controller=ctrl)
        with patch("benchmarks.runner.run_case") as run:
            client = app.test_client()
            response = client.post("/api/benchmarks/run", json={"case_id": "water_01"})
            self.assertEqual(response.status_code, 409)
            ctrl._running = False
            response = client.post("/api/benchmarks/run", json={"case_id": "../config.json"})
            self.assertEqual(response.status_code, 400)
            run.assert_not_called()
            self.assertEqual(client.get("/api/benchmarks/artifact/x/config.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
