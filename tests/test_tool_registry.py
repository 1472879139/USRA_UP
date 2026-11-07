"""注册、逐工具契约和生命周期：合成字节与真实本机 HTTP，不运行模型。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from model_client import ModelAPIError, ModelClient
from model_service.contract import APIError, BANDS, INPUT_CONTRACT, MOCK_TOKEN, sha256
from model_service.registry import ToolDefinition, ToolRegistry, builtin_definitions
from model_service.server import APIServer, Service

CAP = "terramind-small-landcover"
METADATA = {"sensor": "Sentinel-2", "product_level": "L2A", "encoding": "dn_10000", "bands": BANDS}


def threshold_metadata(value):
    if (not isinstance(value, dict) or set(value) != {"threshold"}
            or type(value["threshold"]) not in {float, int} or not 0 <= value["threshold"] <= 1):
        raise APIError(400, "INVALID_METADATA", "需要 0～1 的 threshold")
    return dict(value)


def extra_definition():
    capability = deepcopy(builtin_definitions()[0].capability)
    capability.update(id="offline-threshold", display_name="隔离测试适配器")
    schema = {"type": "object", "additionalProperties": False, "required": ["threshold"],
              "properties": {"threshold": {"type": "number", "minimum": 0, "maximum": 1}}}
    return ToolDefinition(capability, "ursa_test_threshold", deepcopy(INPUT_CONTRACT), schema, threshold_metadata)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tools.local.json"
        self.registry = ToolRegistry(self.path)
        self.registry.initialize()

    def test_registration_rejects_duplicate_id_alias_and_reserved_name(self):
        original = builtin_definitions()[0]
        with self.assertRaises(ValueError):
            self.registry.register(original)
        capability = {**original.capability, "id": "new-tool"}
        for alias in [original.mcp_name, "ursa_get_job"]:
            with self.assertRaises(ValueError):
                self.registry.register(ToolDefinition(capability, alias, original.input_contract,
                                                      original.metadata_schema, original.metadata_validator))
        changed = self.registry.definition(original.capability["id"])
        changed.capability["model"] = "不能影响目录"
        self.assertNotEqual(self.registry.definition(original.capability["id"]).capability["model"], "不能影响目录")

    def test_each_tool_has_its_own_result_roles_and_metadata_schema(self):
        tm = self.registry.contract(CAP, "mock")
        da = self.registry.contract("da-v2-field-delineation", "mock")
        self.assertNotIn("field_boundaries", tm["result_schema"]["$defs"]["Artifact"]["properties"]["role"]["enum"])
        self.assertIn("field_boundaries", da["result_schema"]["$defs"]["Artifact"]["properties"]["role"]["enum"])
        self.registry.register(extra_definition())
        custom = self.registry.contract("offline-threshold", "mock")
        self.assertEqual(custom["metadata_schema"]["required"], ["threshold"])
        self.assertEqual(tm["metadata_schema"]["required"], ["sensor", "product_level", "encoding", "bands"])

    def test_lifecycle_revision_and_restore_stays_disabled(self):
        before = self.registry.catalog("mock")["registry_revision"]
        self.registry.change(CAP, "disable")
        self.assertNotEqual(before, self.registry.catalog("mock")["registry_revision"])
        with self.assertRaises(APIError) as caught:
            self.registry.require_enabled(CAP)
        self.assertEqual(caught.exception.code, "TOOL_DISABLED")
        self.registry.change(CAP, "remove")
        self.assertNotIn(CAP, [x["id"] for x in self.registry.catalog("mock")["capabilities"]])
        self.assertEqual(next(x for x in self.registry.management_status() if x["id"] == CAP)["state"], "removed")
        with self.assertRaises(ValueError):
            self.registry.change(CAP, "enable")
        self.registry.change(CAP, "restore")
        with self.assertRaises(APIError):
            self.registry.require_enabled(CAP)
        self.registry.change(CAP, "enable")
        self.registry.require_enabled(CAP)

    def test_invalid_or_missing_state_fails_closed(self):
        for value in [
            {"version": 1, "tools": {CAP: {"enabled": "false", "removed": False}}},
            {"version": True, "tools": {}},
            {"version": 1, "tools": {"not-installed": {"enabled": False, "removed": False}}},
            {"version": 1, "tools": {CAP: {"enabled": True, "removed": True}}},
        ]:
            self.path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(APIError) as caught:
                self.registry.catalog("mock")
            self.assertEqual(caught.exception.code, "TOOL_REGISTRY_UNAVAILABLE")
        self.path.unlink()
        with self.assertRaises(APIError):
            self.registry.require_enabled(CAP)

    def test_no_overwrite_or_concurrent_state_update(self):
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.registry.initialize()
        with self.assertRaises(APIError):
            self.registry.change("unknown", "disable")
        lock = self.path.with_name(self.path.name + ".lock")
        lock.write_text("已存在的维护锁", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.registry.change(CAP, "disable")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(lock.exists())

    def test_cli_uses_same_file_without_installing_or_deleting_weights(self):
        result = subprocess.run([sys.executable, "-m", "model_service.tools", "--registry-file", str(self.path),
                                 "disable", CAP], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["enabled"])
        self.assertEqual(set(self.tmp_path_names()), {"tools.local.json"})

    def tmp_path_names(self):
        return [p.name for p in Path(self.tmp.name).iterdir()]


class OfflineBackend:
    mode = "mock"

    def __init__(self):
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.ids = ["da-v2-field-delineation", CAP]

    def availability(self):
        return {key: "ready" for key in self.ids}

    def run(self, task, folder, input_path, cancelled):
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("隔离测试等待超时")
        output = folder / "output"
        output.mkdir()
        content = b"offline-preview"
        (output / "preview.png").write_bytes(content)
        return {"execution_mode": "mock", "quality_status": "synthetic_demo", "accuracy_evaluated": False,
                "capability_id": task["capability_id"], "model": "offline-fixture", "model_revision": "fixture",
                "input_sha256": task["input"]["sha"], "width": 1, "height": 1, "crs": "EPSG:32650",
                "transform": [1, 0, 0, 0, -1, 0, 0, 0, 1], "valid_pixels": 1, "pixel_area_m2": 1,
                "classes": {"0": "合成"}, "statistics": [], "warnings": ["隔离测试，不分析影像"],
                "model_run": {"metadata": task["payload"]["metadata"]},
                "files": [{"name": "preview.png", "size_bytes": len(content), "sha256": sha256(content),
                           "media_type": "image/png", "role": "preview"}]}


class RegistryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = ToolRegistry(self.root / "tools.local.json")
        self.registry.initialize()
        self.backend = OfflineBackend()
        self.service = Service(self.root / "data", self.backend,
                               {"test": hashlib.sha256(MOCK_TOKEN.encode()).hexdigest()}, registry=self.registry)
        self.server = APIServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.client = ModelClient("http://127.0.0.1:" + str(self.server.server_port), MOCK_TOKEN)
        self.input_id = self.client.request("POST", "/v1/inputs", b"II*\x00offline", media_type="image/tiff")["input_id"]

    def tearDown(self):
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.close()
        self.tmp.cleanup()

    def submit(self, key=None, capability=CAP, metadata=None):
        return self.client.submit(capability, self.input_id, METADATA if metadata is None else metadata, key or uuid.uuid4().hex)

    def finish(self, job):
        return self.client.wait(job["job_id"], timeout=4, interval=0.02)

    def test_authenticated_pagination_contract_and_openapi(self):
        first = self.client.tools(limit=1)
        second = self.client.tools(limit=1, offset=first["next_offset"])
        self.assertEqual(first["total"], 2)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(first["registry_revision"], second["registry_revision"])
        self.assertNotEqual(first["tools"][0]["tool_id"], second["tools"][0]["tool_id"])
        self.assertEqual(self.client.tool_contract(CAP)["mcp_name"], "ursa_classify_landcover")
        spec = self.client.request("GET", "/openapi.json")
        self.assertIn("/v1/tools", spec["paths"])
        bad = ModelClient(self.client.base_url, "invalid")
        with self.assertRaises(ModelAPIError) as caught:
            bad.tools()
        self.assertEqual(caught.exception.status, 401)
        for query in ["limit=0", "limit=51", "offset=-1", "limit=x", "limit=1&limit=2", "other=1"]:
            with self.assertRaises(ModelAPIError):
                self.client.request("GET", "/v1/tools?" + query)

    def test_disable_rejects_new_jobs_but_preserves_idempotent_receipt(self):
        key = uuid.uuid4().hex
        old = self.finish(self.submit(key))
        self.registry.change(CAP, "disable")
        self.assertEqual(self.submit(key)["job_id"], old["job_id"])
        with self.assertRaises(ModelAPIError) as caught:
            self.submit()
        self.assertEqual(caught.exception.code, "TOOL_DISABLED")
        value = self.client.tool_contract(CAP)
        self.assertFalse(value["capability"]["enabled"])
        self.assertEqual(value["capability"]["availability"], "unavailable")
        self.registry.change(CAP, "enable")
        self.assertEqual(self.finish(self.submit())["status"], "succeeded")

    def test_removed_tool_preserves_queued_running_jobs_and_downloads(self):
        self.backend.release.clear()
        first = self.submit()
        self.assertTrue(self.backend.started.wait(1))
        key = uuid.uuid4().hex
        queued = self.submit(key)
        self.registry.change(CAP, "remove")
        self.assertNotIn(CAP, [t["tool_id"] for t in self.client.tools()["tools"]])
        with self.assertRaises(ModelAPIError):
            self.client.tool_contract(CAP)
        self.assertEqual(self.submit(key)["job_id"], queued["job_id"])
        with self.assertRaises(ModelAPIError) as caught:
            self.submit()
        self.assertEqual(caught.exception.code, "TOOL_REMOVED")
        self.backend.release.set()
        for job in [first, queued]:
            completed = self.finish(job)
            self.assertEqual(completed["status"], "succeeded")
            path = self.client.download(completed["result"]["artifacts"][0], self.root / job["job_id"])
            self.assertEqual(path.read_bytes(), b"offline-preview")

    def test_runtime_unavailable_never_implicitly_routes_to_another_model(self):
        self.registry.register(extra_definition())
        with self.assertRaises(ModelAPIError) as caught:
            self.submit(capability="offline-threshold", metadata={"threshold": 0.4})
        self.assertEqual(caught.exception.code, "MODEL_UNAVAILABLE")
        self.assertFalse(self.backend.started.is_set())

    def test_registered_adapter_uses_its_own_metadata_contract(self):
        self.registry.register(extra_definition())
        self.backend.ids.append("offline-threshold")
        contract = self.client.tool_contract("offline-threshold")
        self.assertEqual(contract["metadata_schema"]["required"], ["threshold"])
        spec = self.client.request("GET", "/openapi.json")
        custom = next(x for x in spec["components"]["schemas"]["JobRequest"]["oneOf"]
                      if x["properties"]["capability_id"]["const"] == "offline-threshold")
        self.assertEqual(custom["properties"]["metadata"], contract["metadata_schema"])
        with self.assertRaises(ModelAPIError):
            self.submit(capability="offline-threshold")
        completed = self.finish(self.submit(capability="offline-threshold", metadata={"threshold": 0.4}))
        self.assertEqual(completed["result"]["model_run"]["metadata"], {"threshold": 0.4})

    def test_corrupted_state_stops_new_submissions_without_blocking_existing_jobs(self):
        old = self.finish(self.submit())
        self.registry.state_file.write_text("{}", encoding="utf-8")
        for operation in [self.client.tools, lambda: self.submit()]:
            with self.assertRaises(ModelAPIError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "TOOL_REGISTRY_UNAVAILABLE")
        self.assertEqual(self.client.job(old["job_id"])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
