"""模型端/应用端共享契约验收：真实HTTP、隔离身份、队列和输出完整性。"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from model_client import ModelAPIError, ModelClient
from model_service.backends import MockBackend
from model_service.contract import (BANDS, MOCK_TOKEN, APIError, decode_json, validate_job, validate_metadata)
from model_service.prepare_demo import prepare, configure_gis
from model_service.server import APIServer, Service
from model_service.storage import Store

METADATA = {"sensor": "Sentinel-2", "product_level": "L2A", "encoding": "dn_10000", "bands": BANDS}
CAP = "da-v2-field-delineation"


class ContractTests(unittest.TestCase):
    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(APIError):
            decode_json('{"a":1,"a":2}')

    def test_nan_and_nonobject_rejected(self):
        for content in ['{"a":NaN}', '[]', '{"a":Infinity}']:
            with self.subTest(content=content), self.assertRaises(APIError):
                decode_json(content)

    def test_band_normalization_and_reordering(self):
        value = {**METADATA, "bands": ["B01"] + BANDS[1:]}
        self.assertEqual(validate_metadata(value)["bands"], BANDS)

    def test_missing_band_and_wrong_product_rejected(self):
        for value in [{**METADATA, "bands": BANDS[:-1]}, {**METADATA, "product_level": "L1C"}, {**METADATA, "encoding": "guess"}]:
            with self.subTest(value=value), self.assertRaises(APIError):
                validate_metadata(value)

    def test_unregistered_model_and_extra_fields_rejected(self):
        payload = {"capability_id": CAP, "input_id": "a" * 32, "metadata": METADATA}
        for value in [{**payload, "capability_id": []}, {**payload, "shell": "anything"}]:
            with self.assertRaises(APIError):
                validate_job(value)

    def test_remote_plain_http_and_embedded_credentials_rejected(self):
        for url in ["http://example.com", "https://user:password@example.com", "https://example.com?api_key=secret"]:
            with self.assertRaises(ValueError):
                ModelClient(url, "a" * 40)

    def test_no_external_download_or_path_traversal(self):
        client = ModelClient("http://127.0.0.1:1", MOCK_TOKEN)
        for item in [{"name": "../secret", "download_path": "/v1/artifacts/" + "a" * 32},
                     {"name": "result.tif", "download_path": "https://elsewhere.invalid/file"}]:
            with self.assertRaises(ValueError):
                client.download(item, ".")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures_tmp = tempfile.TemporaryDirectory()
        cls.fixtures = prepare(Path(cls.fixtures_tmp.name) / "fixtures")

    @classmethod
    def tearDownClass(cls):
        cls.fixtures_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = MockBackend(self.fixtures, delay=0.12)
        keys = {"alice": hashlib.sha256(MOCK_TOKEN.encode()).hexdigest(), "bob": hashlib.sha256(b"second-developer-token-for-tests-only").hexdigest()}
        self.service = Service(self.tmp.name, self.backend, keys)
        self.server = APIServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = ModelClient(self.url, MOCK_TOKEN)
        self.other = ModelClient(self.url, "second-developer-token-for-tests-only")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.tmp.cleanup()

    def submit(self, capability=CAP, idem=None):
        uploaded = self.client.upload(self.fixtures / "input.tif")
        return self.client.submit(capability, uploaded["input_id"], METADATA, idem)

    def finish(self, job):
        result = self.client.wait(job["job_id"], timeout=5, interval=0.02)
        self.assertEqual(result["status"], "succeeded", result)
        return result

    def test_unauthorized_rejected_and_health_public(self):
        client = ModelClient(self.url, "wrong-key")
        self.assertEqual(client.health()["api_version"], "1.0.0")
        with self.assertRaises(ModelAPIError) as context:
            client.capabilities()
        self.assertEqual((context.exception.status, context.exception.code), (401, "UNAUTHORIZED"))

    def test_capabilities_have_tiers_and_mark_mock(self):
        value = self.client.capabilities()
        self.assertEqual({c["tier"] for c in value["capabilities"]}, {"specialist", "general"})
        self.assertEqual(value["execution_mode"], "mock")
        self.assertFalse(value["implicit_model_fallback"])

    def test_openapi_matches_routes(self):
        spec = self.client.request("GET", "/openapi.json")
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/v1/jobs/{job_id}/cancel", spec["paths"])

    def test_health_reports_stopped_worker(self):
        self.service.stop.set()
        self.service.worker.join(timeout=2)
        with self.assertRaises(ModelAPIError) as context:
            self.client.health()
        self.assertEqual(context.exception.status, 503)

    def test_openapi_references_exist(self):
        spec = self.client.request("GET", "/openapi.json")
        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    name = value["$ref"].split("/")[-1]
                    self.assertIn(name, spec["components"]["schemas"])
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(spec)

    def test_both_capabilities_complete_and_download_hashes(self):
        for capability in [CAP, "terramind-small-landcover"]:
            result = self.finish(self.submit(capability))["result"]
            schemas = self.client.request("GET", "/openapi.json")["components"]["schemas"]
            self.assertFalse(set(schemas["Result"]["required"]) - set(result))
            self.assertEqual(result["quality_status"], "synthetic_demo")
            self.assertFalse(result["accuracy_evaluated"])
            for item in result["artifacts"]:
                self.assertFalse(set(schemas["Artifact"]["required"]) - set(item))
                path = self.client.download(item, Path(self.tmp.name) / "downloads" / capability)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
                if item["role"] == "field_boundaries":
                    boundary = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(len(boundary["features"]), 1)
                    self.assertEqual(boundary["features"][0]["properties"]["quality_status"], "synthetic_demo")

    def test_idempotency_returns_same_job(self):
        uploaded = self.client.upload(self.fixtures / "input.tif")
        idem = uuid.uuid4().hex
        first = self.client.submit(CAP, uploaded["input_id"], METADATA, idem)
        second = self.client.submit(CAP, uploaded["input_id"], METADATA, idem)
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(ModelAPIError) as context:
            self.client.submit("terramind-small-landcover", uploaded["input_id"], METADATA, idem)
        self.assertEqual(context.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_other_identity_cannot_use_input_job_or_artifact(self):
        job = self.submit()
        result = self.finish(job)
        for call in [lambda: self.other.submit(CAP, job["input_id"], METADATA),
                     lambda: self.other.job(job["job_id"]),
                     lambda: self.other.request("GET", result["result"]["artifacts"][0]["download_path"], raw=True)]:
            with self.assertRaises(ModelAPIError) as context:
                call()
            self.assertEqual(context.exception.status, 404)

    def test_input_format_error(self):
        with self.assertRaises(ModelAPIError) as context:
            self.client.request("POST", "/v1/inputs", b"not TIFF", media_type="image/tiff")
        self.assertEqual(context.exception.code, "INPUT_UNSUPPORTED")

    def test_size_limit_enforced_before_reading_body(self):
        with self.assertRaises(ModelAPIError) as context:
            self.client.request("POST", "/v1/inputs", b"x", media_type="image/tiff", headers={"Content-Length": str(17 * 1024 ** 2)})
        self.assertEqual(context.exception.status, 413)

    def test_download_tampering_rejected(self):
        result = self.finish(self.submit())
        item = result["result"]["artifacts"][0]
        path = self.service.store.job_dir(result["job_id"]) / "output" / item["name"]
        path.write_bytes(b"changed")
        with self.assertRaises(ModelAPIError) as context:
            self.client.download(item, Path(self.tmp.name) / "downloads")
        self.assertEqual(context.exception.code, "ARTIFACT_CHANGED")

    def test_existing_local_download_is_not_overwritten(self):
        result = self.finish(self.submit())["result"]
        item = result["artifacts"][0]
        directory = Path(self.tmp.name) / "downloads"
        directory.mkdir()
        (directory / item["name"]).write_bytes(b"my file")
        with self.assertRaises(FileExistsError):
            self.client.download(item, directory)
        self.assertEqual((directory / item["name"]).read_bytes(), b"my file")

    def test_running_task_cancellation(self):
        self.backend.delay = 1.0
        job = self.submit()
        for _ in range(100):
            if self.client.job(job["job_id"])["status"] == "running":
                break
            time.sleep(0.01)
        self.client.cancel(job["job_id"])
        final = self.client.wait(job["job_id"], timeout=3, interval=0.02)
        self.assertEqual(final["status"], "cancelled")

    def test_single_worker_serializes_models(self):
        self.backend.delay = 0.4
        first = self.submit()
        for _ in range(100):
            if self.client.job(first["job_id"])["status"] == "running":
                break
            time.sleep(0.01)
        second = self.submit("terramind-small-landcover")
        self.assertEqual(self.client.job(second["job_id"])["status"], "queued")
        self.finish(first)
        self.finish(second)

    def test_queued_cancellation_does_not_execute(self):
        self.backend.delay = 0.4
        first = self.submit()
        second = self.submit("terramind-small-landcover")
        result = self.client.cancel(second["job_id"])
        self.assertEqual(result["status"], "cancelled")
        self.finish(first)
        self.assertFalse(self.service.store.job_dir(second["job_id"]).exists())


class PersistenceAndInputTests(unittest.TestCase):
    def test_restart_fails_interrupted_and_preserves_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            data = store.add_input("owner", b"II*\x00test")
            payload = {"capability_id": CAP, "input_id": data["input_id"], "metadata": METADATA}
            first, _ = store.submit("owner", payload, "a" * 32)
            second, _ = store.submit("owner", payload, "b" * 32)
            claimed = store.claim()
            self.assertEqual(claimed["id"], first["job_id"])
            store.close()
            restarted = Store(folder)
            self.assertEqual(restarted.public_job("owner", first["job_id"])["error"]["code"], "SERVER_RESTARTED")
            self.assertEqual(restarted.public_job("owner", second["job_id"])["status"], "queued")
            restarted.close()

    def test_storage_limit_does_not_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, max_bytes=1)
            with self.assertRaises(APIError):
                store.add_input("owner", b"II*\x00test")
            self.assertEqual(list((Path(folder) / "inputs").iterdir()), [])
            store.close()

    def test_tiling_preserves_original_grid_and_invalid_pixels(self):
        configure_gis()
        import numpy as np
        from model_service.inference import predict_terramind
        class FakeModel:
            def predict(self, raw, valid):
                prediction = np.full(valid.shape, 4, dtype="uint8")
                prediction[~valid] = 255
                return prediction, {}
        raw = np.full((12, 257, 513), 2000, dtype="float32")
        valid = np.ones((257, 513), dtype=bool)
        valid[:10, :20] = False
        result, info = predict_terramind(FakeModel(), raw, valid)
        self.assertEqual(result.shape, valid.shape)
        self.assertTrue((result[valid] == 4).all())
        self.assertTrue((result[~valid] == 255).all())
        self.assertGreater(info["tile_count"], 1)

    def test_input_encoding_and_band_description_mismatch(self):
        configure_gis()
        from model_service.inference import read_input
        with tempfile.TemporaryDirectory() as folder:
            fixtures = prepare(Path(folder) / "fixtures")
            raw, valid, profile = read_input(fixtures / "input.tif", METADATA)
            self.assertEqual(raw.shape, (12, 32, 32))
            self.assertTrue(valid.all())
            from model_service.backends import BackendError
            with self.assertRaises(BackendError):
                read_input(fixtures / "input.tif", {**METADATA, "bands": list(reversed(BANDS))})
            with self.assertRaises(BackendError):
                read_input(fixtures / "input.tif", {**METADATA, "encoding": "reflectance_0_1"})


if __name__ == "__main__":
    unittest.main()
