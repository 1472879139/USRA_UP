"""v2 使用小型合成输入和模拟适配器验证真实 HTTP，不访问公网或真实模型。"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from model_client import ModelClient, ModelAPIError
from model_service.contract import APIError, MOCK_TOKEN
from model_service.server import APIServer, Service
from test_tool_registry import OfflineBackend, METADATA, CAP
from tool_client_v2 import ToolClientV2

try:
    from model_service.v2_adapters import build_registry, JSONSummaryExample
    from model_service.v2_contract import AdapterResult, ProducedArtifact, ToolAdapter, ToolSpec, object_schema, schema
    V2_AVAILABLE = True
except ImportError:
    V2_AVAILABLE = False


class LocalAdapter(ToolAdapter if V2_AVAILABLE else object):
    def __init__(self, behavior="normal"):
        self.behavior = behavior
        self.started, self.release = threading.Event(), threading.Event()
        self.spec = ToolSpec("test-local", "隔离测试", "模拟适配器边界测试", "1.0.0", "ursa_v2_test_local",
            {}, (), object_schema(), {"type": "array", "items": {"type": "integer"}}, {"report": ["application/json"]}, "mock")

    def execute(self, context, inputs, parameters):
        self.started.set()
        if self.behavior == "gate":
            while not self.release.wait(0.005):
                context.check_active()
        context.check_active()
        if self.behavior == "invalid-output":
            return AdapterResult({"unexpected": True})
        if self.behavior == "large-output":
            return AdapterResult("x" * (1024 ** 2 + 1))
        if self.behavior == "invalid-path":
            return AdapterResult([1], [ProducedArtifact("../outside.json", "report", "application/json")])
        if self.behavior == "invalid-media":
            context.results_dir.mkdir()
            (context.results_dir / "report.json").write_text("{}", encoding="utf-8")
            return AdapterResult([1], [ProducedArtifact("report.json", "report", "text/html")])
        return AdapterResult([1, 2, 3])


@unittest.skipUnless(V2_AVAILABLE, "v2 需独立环境中的 requirements-tools-v2.lock")
class V2HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = OfflineBackend()
        self.local = LocalAdapter()
        self.registry = build_registry(self.backend, self.root / "v2-tools.local.json", True, [self.local])
        self.registry.initialize()
        self.start(self.registry)
        self.before = self.root / "before.csv"
        self.after = self.root / "after.csv"
        self.before.write_text("value\n1\n2\n", encoding="utf-8")
        self.after.write_text("value\n4\n8\n", encoding="utf-8")

    def start(self, registry):
        self.service = Service(self.root / "data", self.backend,
            {"alice": hashlib.sha256(MOCK_TOKEN.encode()).hexdigest(),
             "bob": hashlib.sha256(b"v2-offline-bob").hexdigest()}, v2_registry=registry)
        self.server = APIServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = ToolClientV2(self.url, MOCK_TOKEN)
        self.other = ToolClientV2(self.url, "v2-offline-bob")
        self.v1 = ModelClient(self.url, MOCK_TOKEN)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.close()

    def tearDown(self):
        self.local.release.set()
        self.backend.release.set()
        self.stop()
        self.tmp.cleanup()

    def finish(self, job):
        return self.client.wait(job["job_id"], timeout=4, interval=0.01)

    def summary(self, key=None, values=None):
        return self.client.submit("demo-json-summary", {"values": [1, 2, 3] if values is None else values}, {}, key)

    def compare_inputs(self):
        return {name: {"file_id": self.client.upload(path, "text/csv")["file_id"]}
                for name, path in [("before", self.before), ("after", self.after)]}

    def test_catalog_has_independent_inputs_outputs_and_pagination(self):
        first = self.client.tools(limit=2)
        second = self.client.tools(limit=2, offset=first["next_offset"])
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["registry_revision"], second["registry_revision"])
        scalar = self.client.tool_contract("demo-json-summary")
        csv = self.client.tool_contract("demo-csv-compare")
        self.assertEqual(scalar["input_slots"]["values"]["kind"], "json")
        self.assertEqual(set(csv["input_schema"]["required"]), {"before", "after"})
        self.assertEqual(csv["parameters_schema"]["required"], ["column"])
        self.assertNotEqual(csv["output_schema"], scalar["output_schema"])
        for contract in first["tools"] + second["tools"]:
            self.assertNotIn("/v1/artifacts/", json.dumps(contract["output_schema"]))
            for name in ["input_schema", "parameters_schema", "output_schema", "job_schema"]:
                schema(contract[name])
        with self.assertRaises(ModelAPIError):
            ToolClientV2(self.url, "invalid").tools()

    def test_json_only_job_downloads_json_and_csv_with_hashes(self):
        result = self.finish(self.summary())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["outputs"]["sum"], 6)
        self.assertEqual(result["result"]["execution_mode"], "mock")
        self.assertEqual(len(result["result"]["artifacts"]), 2)
        for artifact in result["result"]["artifacts"]:
            self.assertTrue(artifact["download_path"].startswith("/v2/artifacts/"))
            self.assertTrue(self.client.download(artifact, self.root / "downloads").is_file())

    def test_multiple_csv_files_have_named_slots_and_independent_parameters(self):
        result = self.finish(self.client.submit("demo-csv-compare", self.compare_inputs(), {"column": "value"}))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["outputs"]["delta"], 9)
        self.assertTrue(result["result"]["outputs"]["synthetic_demo"])

    def test_input_schema_and_media_reject_wrong_slots_types_urls_and_paths(self):
        cases = [
            ("demo-json-summary", {"values": "not-array"}, {}),
            ("demo-json-summary", {"values": [True]}, {}),
            ("demo-json-summary", {"values": [1], "extra": 2}, {}),
            ("demo-json-summary", {"values": [1]}, {"scale": 1000}),
            ("demo-csv-compare", {"before": {"file_id": "a" * 32}}, {"column": "value"}),
            ("demo-csv-compare", {"before": {"path": "/etc/passwd"}, "after": {"url": "https://invalid/"}}, {"column": "value"}),
        ]
        for tool, inputs, parameters in cases:
            with self.subTest(tool=tool, inputs=inputs), self.assertRaises(ModelAPIError):
                self.client.submit(tool, inputs, parameters)
        with self.assertRaises(ModelAPIError) as caught:
            self.client.upload(self.before, "text/html")
        self.assertEqual(caught.exception.status, 415)
        image = self.client.request("POST", "/v2/files", b"II*\x00offline", media_type="image/tiff")
        with self.assertRaises(ModelAPIError) as caught:
            self.client.submit("demo-csv-compare", {"before": {"file_id": image["file_id"]}, "after": {"file_id": image["file_id"]}}, {"column": "value"})
        self.assertEqual(caught.exception.code, "INPUT_UNSUPPORTED")

    def test_cross_identity_files_jobs_cancel_and_download_are_isolated(self):
        inputs = self.compare_inputs()
        with self.assertRaises(ModelAPIError) as caught:
            self.other.submit("demo-csv-compare", inputs, {"column": "value"})
        self.assertEqual(caught.exception.status, 404)
        result = self.finish(self.summary())
        for operation in [
            lambda: self.other.job(result["job_id"]), lambda: self.other.cancel(result["job_id"]),
            lambda: self.other.request("GET", result["result"]["artifacts"][0]["download_path"], raw=True),
        ]:
            with self.assertRaises(ModelAPIError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 404)

    def test_idempotency_and_soft_removal_preserve_receipts_and_results(self):
        key = uuid.uuid4().hex
        old = self.finish(self.summary(key))
        self.registry.change("demo-json-summary", "remove")
        self.assertEqual(self.summary(key)["job_id"], old["job_id"])
        with self.assertRaises(ModelAPIError) as caught:
            self.summary(key, [4])
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(ModelAPIError) as caught:
            self.summary()
        self.assertEqual(caught.exception.code, "TOOL_REMOVED")
        self.assertEqual(self.client.job(old["job_id"])["status"], "succeeded")
        self.assertTrue(self.client.download(old["result"]["artifacts"][0], self.root / "old").is_file())

    def test_no_artifact_and_array_outputs_are_valid(self):
        result = self.finish(self.client.submit("test-local", {}, {}))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["outputs"], [1, 2, 3])
        self.assertEqual(result["result"]["artifacts"], [])

    def test_invalid_output_or_artifacts_never_publish_success(self):
        for behavior in ["invalid-output", "invalid-path", "invalid-media"]:
            self.local.behavior = behavior
            result = self.finish(self.client.submit("test-local", {}, {}))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "RESULT_INVALID")
            self.assertIsNone(result["result"])

    def test_running_and_queued_cancellation(self):
        self.local.behavior = "gate"
        running = self.client.submit("test-local", {}, {})
        self.assertTrue(self.local.started.wait(1))
        queued = self.summary()
        self.assertEqual(self.client.cancel(queued["job_id"])["status"], "cancelled")
        self.client.cancel(running["job_id"])
        self.assertEqual(self.finish(running)["status"], "cancelled")

    def test_v1_and_v2_share_one_worker_and_v1_contract_stays_intact(self):
        self.local.behavior = "gate"
        active = self.client.submit("test-local", {}, {})
        self.assertTrue(self.local.started.wait(1))
        uploaded = self.v1.request("POST", "/v1/inputs", b"II*\x00offline", media_type="image/tiff")
        old = self.v1.submit(CAP, uploaded["input_id"], METADATA)
        self.assertFalse(self.backend.started.wait(0.04))
        self.local.release.set()
        self.assertEqual(self.finish(active)["status"], "succeeded")
        result = self.v1.wait(old["job_id"], timeout=3, interval=0.01)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["result"]["artifacts"][0]["download_path"].startswith("/v1/"))
        self.assertEqual(len(self.v1.capabilities()["capabilities"]), 2)

    def test_legacy_models_are_reused_through_v2_image_slot_in_mock_mode(self):
        image = self.client.request("POST", "/v2/files", b"II*\x00offline", media_type="image/tiff")
        result = self.finish(self.client.submit(CAP, {"image": {"file_id": image["file_id"]}}, METADATA))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["outputs"]["capability_id"], CAP)
        self.assertEqual(result["result"]["execution_mode"], "mock")

    def test_cooperative_timeout_and_input_tampering_are_detected(self):
        self.local.behavior = "gate"
        self.service.v2.max_seconds = 0.02
        timed = self.finish(self.client.submit("test-local", {}, {}))
        self.assertEqual(timed["error"]["code"], "JOB_TIMEOUT")
        self.service.v2.max_seconds = 3
        self.local.started.clear()
        running = self.client.submit("test-local", {}, {})
        self.assertTrue(self.local.started.wait(1))
        inputs = self.compare_inputs()
        queued = self.client.submit("demo-csv-compare", inputs, {"column": "value"})
        self.service.v2.store.input_path(inputs["before"]["file_id"]).write_bytes(b"changed")
        self.local.release.set()
        self.finish(running)
        self.assertEqual(self.finish(queued)["error"]["code"], "INPUT_CHANGED")

    def test_restart_preserves_results_and_refuses_changed_queued_contract(self):
        old = self.finish(self.summary())
        self.local.behavior = "gate"
        self.client.submit("test-local", {}, {})
        self.assertTrue(self.local.started.wait(1))
        queued = self.summary()
        self.stop()
        revised = JSONSummaryExample()
        revised.spec = replace(revised.spec, version="2.0.0")
        registry = build_registry(self.backend, self.registry.state_file, False, [self.local, revised])
        self.start(registry)
        self.assertEqual(self.client.job(old["job_id"])["status"], "succeeded")
        self.assertEqual(self.finish(queued)["error"]["code"], "TOOL_CONTRACT_CHANGED")

    def test_schema_local_references_work_and_external_references_are_rejected(self):
        self.local.spec = replace(self.local.spec, inputs={"value": {"kind": "json",
            "schema": {"$defs": {"value": {"type": "integer"}}, "$ref": "#/$defs/value"}}}, required_inputs=("value",))
        with self.assertRaises(ValueError):
            schema({"type": "object", "properties": {"x": {"$ref": "https://invalid/schema"}}})
        contract = self.local.spec.contract()
        from model_service.v2_contract import validate
        validate({"value": 3}, contract["input_schema"])
        with self.assertRaises(APIError):
            validate({"value": "wrong"}, contract["input_schema"])

    def test_v2_is_optional_and_old_service_returns_explicit_disabled_error(self):
        self.stop()
        self.start(None)
        self.assertFalse(self.v1.health()["v2_enabled"])
        with self.assertRaises(ModelAPIError) as caught:
            self.client.tools()
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "V2_DISABLED"))
        self.assertEqual(self.v1.capabilities()["api_version"], "1.0.0")

    def test_documented_adapter_template_can_be_registered_without_handler_changes(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "examples/tool_v2/adapter_template.py"
        spec = importlib.util.spec_from_file_location("v2_template_for_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.registry.register(module.TextLengthAdapter())
        completed = self.finish(self.client.submit("example-text-length", {"text": "工具模板"}, {}))
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["outputs"]["length"], 4)
        artifact = completed["result"]["artifacts"][0]
        self.assertTrue(self.client.download(artifact, self.root / "template-output").is_file())

    def test_literal_reference_fields_are_data_and_large_outputs_use_artifacts(self):
        from model_service.v2_contract import mounted_schema, validate
        contract = {"type": "object", "properties": {"$ref": {"type": "string"}},
                    "const": {"$ref": "literal"}, "default": {"$id": "literal"}}
        schema(contract)
        nested = mounted_schema(contract, "properties/inputs")
        self.assertEqual(nested["const"], contract["const"])
        validate({"$ref": "literal"}, nested)
        large = LocalAdapter("large-output")
        large.spec = replace(large.spec, tool_id="large-output", mcp_name="ursa_v2_large_output", output_schema={"type": "string"})
        self.registry.register(large)
        completed = self.finish(self.client.submit("large-output", {}, {}))
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["error"]["code"], "RESULT_INVALID")


if __name__ == "__main__":
    unittest.main()
