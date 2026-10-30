"""地物生成与独立评价回归；只用合成影像，不连接GPU、模型仓库或付费API。"""
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[1] / "deploy/terramind_gpu"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("landcover_deploy_test", "deploy.py")
with patch.dict(sys.modules, {"deploy": deploy}):
    landcover = load("landcover_test", "landcover.py")
    builder = load("landcover_builder_test", "build_landcover.py")
with patch.dict(sys.modules, {"deploy": deploy, "landcover": landcover}):
    evaluator = load("landcover_evaluator_test", "evaluate_landcover.py")


def write_tif(path, value, nodata=None):
    with rasterio.open(path, "w", driver="GTiff", height=value.shape[-2], width=value.shape[-1],
                       count=1 if value.ndim == 2 else value.shape[0], dtype=value.dtype,
                       crs="EPSG:32632", transform=from_origin(500000, 6000000, 10, 10), nodata=nodata) as dst:
        dst.write(value, 1) if value.ndim == 2 else dst.write(value)


def repack(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


class LandcoverTests(unittest.TestCase):
    def test_generation_state_loads_shared_aliases_and_tokenizer(self):
        value = SimpleNamespace(shape=(2, 3))
        backbone = {"encoder_embeddings.x": value, "decoder_embeddings.y": value, "encoder.z": value}
        tokenizer = {"decoder.weight": value}
        required = {name: value for name in ("encoder_embeddings.x", "decoder_embeddings.y",
                     "sampler.model.encoder_embeddings.x", "sampler.model.decoder_embeddings.y",
                     "sampler.model.encoder.z", "tokenizer.tok_lulc@224.decoder.weight")}
        selected = landcover.select_generation_state(backbone, tokenizer, required)
        self.assertEqual(len(selected), 6)
        self.assertIs(selected["encoder_embeddings.x"], selected["sampler.model.encoder_embeddings.x"])

    def test_missing_decoder_and_bad_shape_refuse_random_fallback(self):
        for backbone in ({}, {"decoder.weight": SimpleNamespace(shape=(3,))}):
            with self.assertRaisesRegex(RuntimeError, "不使用随机"):
                landcover.select_generation_state(backbone, {}, {"sampler.model.decoder.weight": SimpleNamespace(shape=(2,))})

    def test_unknown_required_component_refused(self):
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            landcover.select_generation_state({}, {}, {"new_module.x": SimpleNamespace(shape=(2,))})

    def test_decode_requires_real_ten_class_finite_output(self):
        logits = np.zeros((1, 10, 224, 224), dtype="float32")
        logits[:, 5] = 1
        self.assertTrue((landcover.decode_labels(logits) == 5).all())
        logits[0, 0, 0, 0] = np.nan
        with self.assertRaisesRegex(RuntimeError, "形状或数值"):
            landcover.decode_labels(logits)
        with self.assertRaises(RuntimeError):
            landcover.decode_labels(np.zeros((1, 9, 224, 224)))

    def test_reference_zero_ignored_but_prediction_zero_and_255_penalized(self):
        truth = np.array([[0, 1, 1, 2]], dtype="uint8")
        prediction = np.array([[9, 0, 255, 2]], dtype="uint8")
        matrix = evaluator.confusion(truth, prediction, np.ones_like(truth, dtype=bool))
        stats = evaluator.metrics_from_matrix(matrix)
        self.assertEqual(stats["evaluation_pixels"], 3)
        self.assertAlmostEqual(stats["pixel_agreement"], 1/3)
        self.assertEqual(stats["abstention_pixels"], 2)
        self.assertEqual(stats["per_class"][0]["recall"], 0)
        self.assertEqual(stats["per_class"][1]["iou"], 1)
        self.assertEqual(stats["mean_iou"], 0.5)

    def test_all_failed_case_is_counted_as_full_abstention(self):
        truth = np.array([[1, 2], [1, 2]], dtype="uint8")
        matrix = evaluator.confusion(truth, np.full_like(truth, 255), np.ones_like(truth, dtype=bool))
        stats = evaluator.metrics_from_matrix(matrix)
        self.assertEqual(stats["evaluation_pixels"], 4)
        self.assertEqual(stats["pixel_agreement"], 0)
        self.assertEqual(stats["mean_iou"], 0)

    def test_absent_classes_undefined_not_fabricated_as_perfect(self):
        matrix = np.zeros((10, 11), dtype="int64")
        matrix[1, 1] = 2
        stats = evaluator.metrics_from_matrix(matrix)
        self.assertEqual(stats["classes_in_macro"], [1])
        self.assertIsNone(stats["per_class"][1]["iou"])

    def test_bad_category_encoding_rejected_without_guessing_remap(self):
        for reference, prediction in [(11, 1), (1, 99)]:
            with self.assertRaises(RuntimeError):
                evaluator.confusion(np.array([[reference]]), np.array([[prediction]]), np.ones((1, 1), dtype=bool))

    def test_real_geotiff_csv_preview_and_self_contained_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = dict(driver="GTiff", height=224, width=224, count=1, dtype="uint8",
                           crs=rasterio.crs.CRS.from_epsg(32632), transform=from_origin(0, 2240, 10, 10), nodata=255)
            labels = np.ones((224, 224), dtype="uint8")
            labels[:10] = 0
            stats = landcover.write_products(root / "case", np.ones((12, 224, 224), dtype="float32"),
                                            labels, np.ones_like(labels, dtype=bool), profile)
            self.assertEqual(sum(row["pixels"] for row in stats), 224*224)
            with rasterio.open(root / "case/landcover.tif") as src:
                self.assertEqual(src.nodata, 255)
                self.assertEqual(int((src.read(1) == 0).sum()), 2240)
            summary = dict(cases=[dict(id="case", status="completed", statistics=stats),
                                 dict(id="<failed>", status="failed", error="<script>")], completed=1)
            archive = landcover.seal_results(root, summary)
            files, actual = evaluator.load_archive(archive)
            self.assertEqual(actual["completed"], 1)
            doc = files["result.html"].decode()
            self.assertIn("data:image/png;base64,", doc)
            self.assertIn("&lt;script&gt;", doc)
            self.assertNotIn("<script>", doc)
            self.assertIn("不能代替", doc)

    def test_seal_tampering_and_duplicate_members_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.zip"
            data = b"original"
            files = {"summary.json": data, "seal.json": json.dumps({"schema": "ursa.seal.v1",
                     "files": {"summary.json": hashlib.sha256(data).hexdigest()}}).encode()}
            files["summary.json"] = b"tampered"
            repack(target, files)
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                evaluator.load_archive(target)

    def test_dependency_install_only_missing_fixed_hash_no_dependency_upgrades(self):
        with patch.object(landcover, "version", side_effect=landcover.PackageNotFoundError), patch.object(landcover.subprocess, "run") as run:
            landcover.ensure_generation_dependency()
            command = run.call_args_list[0].args[0]
            self.assertIn("--no-deps", command)
            self.assertIn("--require-hashes", command)
            self.assertEqual(run.call_count, 2)

    def test_existing_other_diffusers_version_preserved(self):
        with patch.object(landcover, "version", return_value="0.35.0"), patch.object(landcover.subprocess, "run") as run:
            self.assertEqual(landcover.ensure_generation_dependency(), "0.35.0")
            run.assert_called_once_with([sys.executable, "-m", "pip", "check"], check=True)

    def test_tokenizer_download_pinned_and_cached_hash_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "tokenizer.pt"
            payload = b"test-tokenizer"
            def download(**kwargs):
                self.assertEqual(kwargs["revision"], landcover.TOKENIZER_REVISION)
                self.assertFalse(kwargs["token"])
                target.write_bytes(payload)
                return target
            with patch.object(landcover, "tokenizer_path", return_value=target), patch.object(landcover, "TOKENIZER_SIZE", len(payload)), patch.object(landcover, "TOKENIZER_SHA", hashlib.sha256(payload).hexdigest()), patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}):
                landcover.download_tokenizer(download)
                target.write_bytes(b"x"*len(payload))
                with self.assertRaisesRegex(RuntimeError, "SHA256"):
                    landcover.download_tokenizer(Mock())

    def test_package_whitelist_excludes_evaluator_and_references_and_runs_help(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = root / "inputs"
            inputs.mkdir()
            data = b"synthetic-input"
            (inputs / "case.tif").write_bytes(data)
            (inputs / "reference.tif").write_bytes(b"never include")
            payload = root / "source"
            payload.mkdir()
            for name in ["deploy.py", "landcover.py"]:
                (payload / name).write_bytes((ROOT / name).read_bytes())
            protocol = {"samples": [{"id": "case", "sha256": hashlib.sha256(data).hexdigest()}]}
            (payload / "landcover-manifest.json").write_text(json.dumps(protocol))
            target = root / "job.pyz"
            with patch.object(builder, "HERE", payload):
                builder.build(inputs, target)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(set(archive.namelist()), {"__main__.py", "payload-sha256.json", "deploy.py",
                                 "landcover.py", "landcover-manifest.json", "inputs/case.tif"})
            env = {**os.environ, "USERPROFILE": str(root / "home"), "HOME": str(root / "home"), "PYTHONPATH": ""}
            proc = subprocess.run([sys.executable, "-X", "utf8", str(target), "--help"], env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("无参考标签", proc.stdout)

    def test_fixed_input_precheck_rejects_hash_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "inputs").mkdir()
            (root / "inputs/a.tif").write_bytes(b"wrong")
            with patch.object(landcover, "HERE", root), self.assertRaisesRegex(RuntimeError, "SHA256"):
                landcover.read_input({"id": "a", "sha256": "0"*64})

    def test_full_independent_evaluation_includes_failed_case_and_refuses_wrong_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for directory in ["inputs", "references", "run"]:
                (root / directory).mkdir()
            package = root / "job.pyz"
            package.write_bytes(b"synthetic-package")
            protocol = {"schema": "ursa.terramind.landcover.v1", "settings": {"seed": 123}, "samples": []}
            refs, cases = [], []
            for index, name in enumerate(["complete", "failed"]):
                image = root / "inputs" / (name + ".tif")
                ref = root / "references" / (name + ".tif")
                write_tif(image, np.ones((12, 2, 2), dtype="int16"))
                write_tif(ref, np.ones((2, 2), dtype="uint8"))
                sha = deploy.digest_file(image)
                protocol["samples"].append({"id": name, "sha256": sha})
                refs.append({"id": name, "sha256": deploy.digest_file(ref)})
                case = {"id": name, "status": "completed" if index == 0 else "failed",
                        "input_sha256": sha, "seed": 123 + index}
                if index == 0:
                    (root / "run" / name).mkdir()
                    write_tif(root / "run" / name / "landcover.tif", np.ones((2, 2), dtype="uint8"), nodata=255)
                else:
                    case["error"] = "合成执行失败"
                cases.append(case)
            (root / "landcover-manifest.json").write_text(json.dumps(protocol), encoding="utf-8")
            (root / "evaluation-manifest.json").write_text(json.dumps({"reference": "合成参考", "samples": refs}), encoding="utf-8")
            summary = dict(schema=protocol["schema"], settings=protocol["settings"],
                           protocol_sha256=deploy.digest_file(root / "landcover-manifest.json"),
                           package_sha256=deploy.digest_file(package), checkpoint_sha256=deploy.CHECKPOINT_SHA,
                           tokenizer_sha256=landcover.TOKENIZER_SHA, ground_truth_accessed=False,
                           paid_api_calls=0, completed=1, cases=cases)
            run_root = root / "run"
            (run_root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            hashes = {p.relative_to(run_root).as_posix(): deploy.digest_file(p) for p in run_root.rglob("*") if p.is_file()}
            (run_root / "seal.json").write_text(json.dumps({"schema": "ursa.seal.v1", "files": hashes}))
            archive = root / "result.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                for path in run_root.rglob("*"):
                    if path.is_file():
                        zipped.write(path, path.relative_to(run_root).as_posix())
            with patch.object(evaluator, "HERE", root):
                out, report = evaluator.score(archive, root / "inputs", root / "references", package, root / "out")
                self.assertTrue((out / "evaluation.html").is_file())
                self.assertEqual(report["valid_artifact_cases"], 1)
                stats = report["aggregate_including_failures"]
                self.assertEqual(stats["evaluation_pixels"], 8)
                self.assertEqual(stats["pixel_agreement"], 0.5)
                self.assertEqual(stats["mean_iou"], 0.5)
                summary["ground_truth_accessed"] = True
                with self.assertRaisesRegex(RuntimeError, "隔离声明"):
                    evaluator.check_run(summary, protocol, package)


    def test_capability_probe_checks_actual_interfaces_and_records_existing_version(self):
        modules = [
            SimpleNamespace(vqvae_available=True, terramind_v1_tokenizer_lulc=lambda: None),
            SimpleNamespace(VQVAE=SimpleNamespace(decode_tokens=lambda: None)),
            SimpleNamespace(TerraMindGeneration=SimpleNamespace(forward=lambda: None)),
        ]
        with patch.object(landcover, "version", return_value="0.35.0"), patch.object(landcover, "import_module", side_effect=modules) as importer:
            result = landcover.check_generation_capability()
            self.assertEqual(result["diffusers"], "0.35.0")
            self.assertEqual(result["status"], "required_imports_passed")
            self.assertEqual(importer.call_count, 3)

    def test_capability_import_failure_keeps_actual_version_and_does_not_install(self):
        with patch.object(landcover, "version", return_value="0.35.0"), patch.object(landcover, "import_module", side_effect=ImportError("missing interface")), patch.object(landcover.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "0.35.0.*missing interface"):
                landcover.check_generation_capability()
            run.assert_not_called()

    def test_capability_probe_catches_cached_failure_and_missing_decode(self):
        good = SimpleNamespace(vqvae_available=True, terramind_v1_tokenizer_lulc=lambda: None)
        broken = SimpleNamespace(vqvae_available=False, import_error=ImportError("cached missing dependency"))
        generation = SimpleNamespace(TerraMindGeneration=SimpleNamespace(forward=lambda: None))
        for tokenizer, decoder in [(broken, SimpleNamespace(VQVAE=None)), (good, SimpleNamespace(VQVAE=None))]:
            with patch.object(landcover, "version", return_value="0.35.0"), patch.object(landcover, "import_module", side_effect=[tokenizer, decoder, generation]):
                with self.assertRaisesRegex(RuntimeError, "导入预检失败"):
                    landcover.check_generation_capability()

    def test_capability_probe_blocks_network_and_restores_socket(self):
        import socket
        original = socket.create_connection
        def attempted_network(name):
            socket.create_connection(("127.0.0.1", 9))
        with patch.object(landcover, "version", return_value="0.35.0"), patch.object(landcover, "import_module", side_effect=attempted_network):
            with self.assertRaisesRegex(RuntimeError, "禁止联网"):
                landcover.check_generation_capability()
        self.assertIs(socket.create_connection, original)

    @contextmanager
    def prepared_fixture(self):
        with patch.object(deploy, "configure_download_environment"), patch.object(deploy, "check_environment"), patch.object(deploy, "checked_checkpoint"), patch.object(landcover, "load_protocol", return_value={"samples": []}), patch.object(landcover, "ensure_generation_dependency", return_value="0.35.0"), patch.object(landcover, "download_tokenizer") as download, patch.object(landcover.subprocess, "run") as child:
            yield download, child

    def test_preflight_is_new_offline_process_before_download_and_gpu_run(self):
        with self.prepared_fixture() as (download, child):
            events = []
            def execute(command, **kwargs):
                self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
                self.assertEqual(kwargs["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
                self.assertTrue(kwargs["check"])
                events.append(command[-1])
            child.side_effect = execute
            download.side_effect = lambda: events.append("download")
            landcover.prepare()
            self.assertEqual(events, ["--check-generation", "download", "--run"])

    def test_failed_preflight_stops_before_download_or_gpu_run(self):
        with self.prepared_fixture() as (download, child):
            child.side_effect = subprocess.CalledProcessError(1, ["check"])
            with self.assertRaises(subprocess.CalledProcessError):
                landcover.prepare()
            download.assert_not_called()
            self.assertEqual(child.call_count, 1)

    def test_check_generation_cli_only_probes_without_installing(self):
        with patch.object(sys, "argv", ["landcover.py", "--check-generation"]), patch.object(landcover, "check_generation_capability") as probe, patch.object(landcover, "prepare") as prepare, patch.object(landcover, "run") as run, patch.dict(os.environ):
            self.assertEqual(landcover.main(), 0)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            probe.assert_called_once()
            prepare.assert_not_called()
            run.assert_not_called()



if __name__ == "__main__":
    unittest.main()
