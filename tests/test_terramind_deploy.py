"""部署入口离线测试；不下载模型、不连接真实GPU或语言模型。"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/terramind_gpu/deploy.py"
spec = importlib.util.spec_from_file_location("terramind_small_deploy", SCRIPT)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ), redirect_stdout(io.StringIO()):
        root = Path(folder)
        for name in ["HF_ENDPOINT", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"]:
            os.environ.pop(name, None)
        os.environ["MPLCONFIGDIR"] = str(root / "matplotlib-cache")
        payload = b"offline-checkpoint-fixture"
        with patch.object(deploy, "CHECKPOINT_SIZE", len(payload)), patch.object(deploy, "CHECKPOINT_SHA", hashlib.sha256(payload).hexdigest()):
            yield root, payload


class DeploymentTests(unittest.TestCase):
    def test_download_locks_revision_and_identity_and_explicit_endpoint(self):
        with fixture() as (root, payload):
            target = root / deploy.FILENAME
            def download(**kwargs):
                self.assertEqual(kwargs["repo_id"], deploy.MODEL_ID)
                self.assertEqual(kwargs["revision"], "960f7549bfe9d7a08946860042f83badd604c779")
                self.assertEqual(kwargs["endpoint"], "https://hf-mirror.com")
                self.assertIs(kwargs["token"], False)
                target.write_bytes(payload)
                return target
            self.assertEqual(deploy.download_checkpoint(target, download), target)

    def test_verified_cached_checkpoint_does_not_download(self):
        with fixture() as (root, payload):
            target = root / deploy.FILENAME
            target.write_bytes(payload)
            download = Mock()
            deploy.download_checkpoint(target, download)
            download.assert_not_called()

    def test_wrong_hash_preserves_existing_file_and_stops(self):
        with fixture() as (root, payload):
            target = root / deploy.FILENAME
            target.write_bytes(b"x" * len(payload))
            download = Mock()
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                deploy.download_checkpoint(target, download)
            self.assertEqual(target.read_bytes(), b"x" * len(payload))
            download.assert_not_called()

    def test_pointer_file_is_not_a_model(self):
        with fixture() as (root, _):
            target = root / deploy.FILENAME
            target.write_bytes(b"version https://git-lfs.github.com/spec/v1")
            with self.assertRaisesRegex(RuntimeError, "体积"):
                deploy.checked_checkpoint(target)

    def test_low_disk_space_stops_before_download(self):
        with fixture() as (root, _), patch.object(deploy.shutil, "disk_usage", return_value=SimpleNamespace(free=1024)):
            download = Mock()
            with self.assertRaisesRegex(RuntimeError, "磁盘"):
                deploy.download_checkpoint(root / deploy.FILENAME, download)
            download.assert_not_called()

    def test_unknown_endpoint_is_rejected(self):
        with fixture() as (root, _), patch.dict(os.environ, {"HF_ENDPOINT": "https://unapproved.invalid"}):
            download = Mock()
            with self.assertRaisesRegex(RuntimeError, "HF_ENDPOINT"):
                deploy.download_checkpoint(root / deploy.FILENAME, download)
            download.assert_not_called()

    def test_network_failure_retries_same_download_without_changing_version(self):
        with fixture() as (root, payload):
            target = root / deploy.FILENAME
            calls = []
            def download(**kwargs):
                calls.append(kwargs)
                if len(calls) < 3:
                    raise TimeoutError("offline timeout")
                target.write_bytes(payload)
                return target
            sleep = Mock()
            deploy.download_checkpoint(target, download, sleep)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[1], calls[2])
            self.assertEqual(sleep.call_count, 2)

    def test_download_exhaustion_has_bounded_attempts(self):
        with fixture() as (root, _):
            download = Mock(side_effect=TimeoutError("offline timeout"))
            with self.assertRaisesRegex(RuntimeError, "下载未完成"):
                deploy.download_checkpoint(root / deploy.FILENAME, download, Mock())
            self.assertEqual(download.call_count, 3)

    def test_mismatched_download_path_is_rejected(self):
        with fixture() as (root, _):
            with self.assertRaisesRegex(RuntimeError, "位置"):
                deploy.download_checkpoint(root / deploy.FILENAME, Mock(return_value=root / "wrong.pt"))

    def test_complete_encoder_state_keeps_all_required_weights(self):
        a, b = SimpleNamespace(shape=(2, 3)), SimpleNamespace(shape=(3,))
        required = {"encoder.weight": a, "encoder_norm.weight": b}
        checkpoint = {**required, "decoder.extra": SimpleNamespace(shape=(9,))}
        selected = deploy.select_complete_state(checkpoint, required)
        self.assertEqual(set(selected), set(required))
        self.assertIs(selected["encoder.weight"], a)
        self.assertEqual(len(checkpoint), 3)

    def test_missing_weight_is_not_filled_from_random_initialization(self):
        with self.assertRaisesRegex(RuntimeError, "不使用随机"):
            deploy.select_complete_state({}, {"encoder.weight": SimpleNamespace(shape=(2, 3))})

    def test_wrong_weight_shape_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "形状不符1项"):
            deploy.select_complete_state({"encoder.weight": SimpleNamespace(shape=(2, 4))}, {"encoder.weight": SimpleNamespace(shape=(2, 3))})

    def test_invalid_or_empty_model_state_is_rejected(self):
        with self.assertRaises(RuntimeError):
            deploy.select_complete_state([], {"a": SimpleNamespace(shape=(1,))})
        with self.assertRaises(RuntimeError):
            deploy.select_complete_state({}, {})

    def test_offline_guard_blocks_connections_and_restores_after_error(self):
        before = socket.create_connection, socket.socket.connect, socket.socket.connect_ex
        with self.assertRaisesRegex(RuntimeError, "离线"):
            with deploy.offline_network():
                socket.create_connection(("127.0.0.1", 9))
        self.assertEqual(before, (socket.create_connection, socket.socket.connect, socket.socket.connect_ex))
        with deploy.offline_network(), socket.socket() as stream:
            with self.assertRaisesRegex(RuntimeError, "离线"):
                stream.connect(("127.0.0.1", 9))
            with self.assertRaisesRegex(RuntimeError, "离线"):
                stream.connect_ex(("127.0.0.1", 9))

    def test_prepare_configures_mirror_before_importing_runtime_and_reexecs_offline(self):
        with fixture() as (root, payload):
            target, sample = root / deploy.FILENAME, root / "input.tif"
            sample.write_bytes(b"sample")
            def check():
                self.assertEqual(os.environ["HF_ENDPOINT"], "https://hf-mirror.com")
                return {"test": "offline"}
            def download(path):
                path.write_bytes(payload)
            with patch.object(deploy, "check_environment", side_effect=check), patch.object(deploy, "sample_path", return_value=sample), \
                 patch.object(deploy, "check_sample", return_value=sample), patch.object(deploy, "checkpoint_path", return_value=target), \
                 patch.object(deploy, "download_checkpoint", side_effect=download), patch.object(deploy.subprocess, "run") as run:
                deploy.prepare()
            self.assertEqual(run.call_args.args[0], [sys.executable, str(SCRIPT), "--smoke"])
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertNotIn("HF_HUB_OFFLINE", os.environ)
            manifest = json.loads((root / "download-manifest.json").read_text(encoding="utf-8"))
            self.assertIn("尚未执行", manifest["status"])

    def test_failed_gpu_child_does_not_become_deployment_success(self):
        with fixture() as (root, payload):
            target, sample = root / deploy.FILENAME, root / "input.tif"
            target.write_bytes(payload)
            sample.write_bytes(b"sample")
            with patch.object(deploy, "check_environment", return_value={}), patch.object(deploy, "sample_path", return_value=sample), \
                 patch.object(deploy, "check_sample", return_value=sample), patch.object(deploy, "checkpoint_path", return_value=target), \
                 patch.object(deploy.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["offline-smoke"])):
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.prepare()
            self.assertFalse((root / "deployment-result.json").exists())

    def test_sample_mismatch_stops_before_weight_download(self):
        with fixture() as (root, _), patch.object(deploy, "check_environment", return_value={}), \
             patch.object(deploy, "sample_path", return_value=root / "missing.tif"), patch.object(deploy, "download_checkpoint") as download:
            with self.assertRaisesRegex(RuntimeError, "影像缺失"):
                deploy.prepare()
            download.assert_not_called()

    def test_report_contains_actual_feature_picture_and_does_not_claim_segmentation(self):
        import numpy as np
        with fixture() as (root, _):
            raw = np.arange(13*224*224, dtype="float32").reshape(13, 224, 224)
            features = np.random.default_rng(123).normal(size=(14, 14, 384)).astype("float32")
            deploy.write_report(root, raw, np.ones((224, 224), dtype=bool), features, {"status": "offline-synthetic-report-test"})
            report = (root / "result.html").read_text(encoding="utf-8")
            self.assertIn("data:image/png;base64,", report)
            self.assertIn("当前没有安装任务输出模块", report)
            self.assertIn("本次只验证S2L1C", report)
            self.assertGreater((root / "feature-preview.png").stat().st_size, 1000)

    def test_real_cli_help_runs_without_torch_or_network(self):
        result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), "--help"], capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check", result.stdout)
        self.assertIn("--smoke", result.stdout)

    def test_mode_selection_is_explicit_and_exclusive(self):
        with patch.object(deploy, "check_environment") as check, patch.object(deploy, "prepare") as prepare:
            deploy.main(["--check"])
            check.assert_called_once()
            prepare.assert_not_called()
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            deploy.main(["--check", "--smoke"])


if __name__ == "__main__":
    unittest.main()
