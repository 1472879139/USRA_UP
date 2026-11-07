"""MCP专用环境运行真实SDK协议测试；使用隔离模拟API，不访问真实GPU或LLM。"""
from contextlib import asynccontextmanager
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

try:
    import httpx
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from ursa_mcp.issue_key import issue
    from ursa_mcp.server import create_app
    from ursa_mcp.service import ModelAPI, Identity, ServiceError
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_METADATA = {"sensor": "Sentinel-2", "product_level": "L2A", "encoding": "dn_10000",
                   "bands": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12"]}


class FakeAPI:
    def __init__(self):
        self.calls, self.jobs, self.keys = [], {}, {}
        self.spec = json.loads((ROOT / "examples/mcp/model-api-v1.openapi.json").read_text(encoding="utf-8"))
        self.capabilities = [{"id": name, "display_name": name, "availability": "configured", "input": {"band_count": 12},
            "outputs": ["classification", "statistics", "preview", "provenance"]}
            for name in ["da-v2-field-delineation", "terramind-small-landcover"]]
        self.bad_result = False
        self.secret_result = False

    def __call__(self, request):
        owner = {"Bearer api-alice": "alice", "Bearer api-bob": "bob"}.get(request.headers.get("Authorization"))
        path = request.url.path
        self.calls.append((request.method, path, owner))

        def error(code, message, status=400):
            return httpx.Response(status, json={"error": {"code": code, "message": message, "retryable": False}})

        if owner is None:
            return error("UNAUTHORIZED", "认证失败", 401)
        if path == "/health":
            value = {"status": "ready", "api_version": "1.0.0", "execution_mode": "mock"}
        elif path == "/openapi.json":
            value = self.spec
        elif path == "/v1/capabilities":
            value = {"api_version": "1.0.0", "execution_mode": "mock", "capabilities": self.capabilities}
        elif path == "/v1/jobs" and request.method == "POST":
            data = json.loads(request.content)
            if data["input_id"] != ("a" if owner == "alice" else "b") * 32:
                return error("NOT_FOUND", "本身份不存在该输入", 404)
            key = (owner, request.headers["Idempotency-Key"])
            if key in self.keys:
                old_data, job_id = self.keys[key]
                if old_data != data:
                    return error("IDEMPOTENCY_CONFLICT", "同一键不能提交不同请求", 409)
                value = self.jobs[job_id][1]
            else:
                job_id = f"{len(self.jobs) + 1:032x}"
                result = {"execution_mode": "mock", "quality_status": "synthetic_demo", "accuracy_evaluated": False,
                    "capability_id": data["capability_id"], "model": "offline-fixture", "model_revision": "offline",
                    "input_sha256": "0" * 64, "width": 32, "height": 32, "crs": "EPSG:32650", "transform": [1] * 9,
                    "valid_pixels": 1024, "pixel_area_m2": 100, "classes": {"0": "合成类别"}, "statistics": [],
                    "model_run": {"execution": "offline_fixture"}, "warnings": [], "artifacts": [{
                        "artifact_id": "c" * 32, "name": "preview.png", "size_bytes": 3, "sha256": hashlib.sha256(b"png").hexdigest(),
                        "download_path": "/v1/artifacts/" + "c" * 32, "media_type": "image/png", "role": "preview"}]}
                if self.bad_result:
                    result["accuracy_evaluated"] = True
                if self.secret_result:
                    result["model_run"]["unexpected"] = "api-" + owner
                value = {"job_id": job_id, "status": "succeeded", "capability_id": data["capability_id"],
                         "input_id": data["input_id"], "error": None, "result": result}
                self.jobs[job_id] = (owner, value)
                self.keys[key] = (data, job_id)
        elif path.startswith("/v1/jobs/"):
            job_id = path.split("/")[3]
            entry = self.jobs.get(job_id)
            if entry is None or entry[0] != owner:
                return error("NOT_FOUND", "本身份不存在该任务", 404)
            value = entry[1]
        else:
            return error("NOT_FOUND", "接口不存在", 404)
        return httpx.Response(200, json=value)


@unittest.skipUnless(SDK_AVAILABLE, "请使用独立MCP Python3.12环境运行本文件；主应用GIS环境无需安装SDK")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity_file = self.root / "identities.local.json"
        self.tokens = {"alice": "alice-offline-mcp-token-" + "a" * 32, "bob": "bob-offline-mcp-token-" + "b" * 32}
        self.records = {"identities": [{"owner": owner, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "api_key": "api-" + owner, "scopes": ["read", "submit", "cancel"]} for owner, token in self.tokens.items()]}
        self.save_records()
        self.fake = FakeAPI()
        self.config = {"api_base_url": "http://127.0.0.1:18767", "public_api_base_url": "https://example.invalid/ursa-model-api",
                       "public_mcp_url": "https://example.invalid/ursa-mcp/mcp", "identities_file": str(self.identity_file)}
        self.app = create_app(self.config, ModelAPI(self.config["api_base_url"], httpx.MockTransport(self.fake)))
        started, stopped = asyncio.Event(), asyncio.Event()
        async def lifespan():
            async with self.app.app.router.lifespan_context(self.app.app):
                started.set()
                await stopped.wait()
        task = asyncio.create_task(lifespan())
        await started.wait()
        async def cleanup():
            stopped.set()
            await task
        self.addAsyncCleanup(cleanup)

    def save_records(self):
        self.identity_file.write_text(json.dumps(self.records), encoding="utf-8")

    @asynccontextmanager
    async def client(self, owner="alice", mode="auto"):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                headers={"Authorization": "Bearer " + self.tokens[owner]}, trust_env=False) as http:
            async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode, read_timeout_seconds=5) as client:
                yield client

    async def submit(self, client, input_id="a" * 32, key="offline-submission-1234"):
        return await client.call_tool("ursa_classify_landcover", {"input_id": input_id,
            "metadata": deepcopy(SAMPLE_METADATA), "idempotency_key": key})

    async def raw(self, method="POST", path="/mcp", headers=None, content=b"{}"):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url="http://localhost") as http:
            return await http.request(method, path, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}, content=content)

    async def test_modern_discovery_and_structured_contract(self):
        async with self.client() as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools.tools), 7)
            model = next(t for t in tools.tools if t.name == "ursa_classify_landcover")
            self.assertIn("idempotency_key", model.input_schema["required"])
            self.assertIn("result", model.output_schema["properties"])
            result = await client.call_tool("ursa_get_tool_contract", {"tool_id": "terramind-small-landcover"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["capability"]["input"]["band_count"], 12)
            self.assertIn("artifacts", result.structured_content["result_schema"]["required"])

    async def test_legacy_initialization_is_supported(self):
        async with self.client(mode="legacy") as client:
            self.assertEqual(len((await client.list_tools()).tools), 7)
            result = await client.call_tool("ursa_get_service_info", {})
            self.assertEqual(result.structured_content["execution_mode"], "mock")

    async def test_per_tool_metadata_alias_and_output_contract(self):
        cap = self.fake.capabilities[1]
        cap["mcp_name"] = "ursa_registered_landcover"
        cap["metadata_schema"] = {"type": "object", "additionalProperties": False,
            "required": ["threshold"], "properties": {"threshold": {"type": "number", "minimum": 0, "maximum": 1}}}
        from ursa_mcp.service import contract_schema
        cap["result_schema"] = contract_schema("Result", self.fake.spec)
        cap["result_schema"]["properties"]["capability_id"] = {"const": cap["id"]}
        async with self.client() as client:
            tools = await client.list_tools()
            tool = next(t for t in tools.tools if t.name == "ursa_registered_landcover")
            self.assertEqual(tool.input_schema["properties"]["metadata"], cap["metadata_schema"])
            contract = await client.call_tool("ursa_get_tool_contract", {"tool_id": cap["id"]})
            self.assertEqual(contract.structured_content["metadata_schema"], cap["metadata_schema"])
            old = await self.submit(client)
            self.assertTrue(old.is_error)
            invalid = await client.call_tool(tool.name, {"input_id": "a" * 32, "metadata": SAMPLE_METADATA,
                                                        "idempotency_key": "custom-schema-key-1234"})
            self.assertTrue(invalid.is_error)
            valid = await client.call_tool(tool.name, {"input_id": "a" * 32, "metadata": {"threshold": 0.4},
                                                      "idempotency_key": "custom-schema-key-1234"})
            self.assertFalse(valid.is_error)

    async def test_disabled_or_removed_models_leave_job_queries_available(self):
        async with self.client() as client:
            old = await self.submit(client)
            job_id = old.structured_content["job_id"]
            cap = self.fake.capabilities[1]
            cap["enabled"] = False
            tools = await client.list_tools()
            self.assertNotIn("ursa_classify_landcover", [t.name for t in tools.tools])
            self.assertTrue((await self.submit(client)).is_error)
            detail = await client.call_tool("ursa_get_tool_contract", {"tool_id": cap["id"]})
            self.assertFalse(detail.is_error)
            self.assertFalse(detail.structured_content["capability"]["enabled"])
            self.fake.capabilities = []
            remaining = await client.list_tools()
            self.assertEqual(len(remaining.tools), 5)
            queried = await client.call_tool("ursa_get_job", {"job_id": job_id})
            files = await client.call_tool("ursa_get_artifacts", {"job_id": job_id})
            self.assertFalse(queried.is_error)
            self.assertFalse(files.is_error)

    async def test_per_tool_schema_cannot_trigger_external_reference_fetches(self):
        async with self.client() as client:
            for invalid in [
                {"type": "object", "properties": {"value": {"$ref": "https://untrusted.invalid/schema"}}},
                {"type": "object", "properties": {"value": {"$ref": "#/$defs/missing"}}},
                {"type": "not-a-json-schema-type"},
            ]:
                self.fake.capabilities[1]["metadata_schema"] = invalid
                response = await client.call_tool("ursa_get_service_info", {})
                self.assertTrue(response.is_error)
                self.assertIn("CONTRACT_INVALID", response.content[0].text)

    async def test_submit_retries_once_and_rejects_changed_body(self):
        async with self.client() as client:
            first, second = await self.submit(client), await self.submit(client)
            self.assertFalse(first.is_error)
            self.assertEqual(first.structured_content["job_id"], second.structured_content["job_id"])
            changed = {"input_id": "a" * 32, "metadata": {**SAMPLE_METADATA, "encoding": "reflectance_0_1"},
                       "idempotency_key": "offline-submission-1234"}
            conflict = await client.call_tool("ursa_classify_landcover", changed)
            self.assertTrue(conflict.is_error)
            self.assertIn("IDEMPOTENCY_CONFLICT", conflict.content[0].text)
            self.assertEqual(len(self.fake.jobs), 1)

    async def test_cross_identity_query_cancel_and_artifacts_are_rejected(self):
        async with self.client() as alice:
            job_id = (await self.submit(alice)).structured_content["job_id"]
        async with self.client("bob") as bob:
            for tool in ["ursa_get_job", "ursa_cancel_job", "ursa_get_artifacts"]:
                denied = await bob.call_tool(tool, {"job_id": job_id})
                self.assertTrue(denied.is_error)
                self.assertIn("NOT_FOUND", denied.content[0].text)
            other_input = await self.submit(bob)
            self.assertTrue(other_input.is_error)
            self.assertIn("NOT_FOUND", other_input.content[0].text)

    async def test_artifact_urls_and_mock_quality_remain_explicit(self):
        async with self.client() as client:
            job_id = (await self.submit(client)).structured_content["job_id"]
            result = (await client.call_tool("ursa_get_artifacts", {"job_id": job_id})).structured_content
            self.assertEqual(result["execution_mode"], "mock")
            self.assertFalse(result["accuracy_evaluated"])
            self.assertEqual(result["artifacts"][0]["download_url"], self.config["public_api_base_url"] + "/v1/artifacts/" + "c" * 32)
            text = json.dumps(result)
            self.assertNotIn("api-alice", text)
            self.assertNotIn(self.tokens["alice"], text)

    async def test_identity_scopes_filter_discovery_and_execution(self):
        self.records["identities"][0]["scopes"] = ["read"]
        self.save_records()
        async with self.client() as client:
            self.assertEqual(len((await client.list_tools()).tools), 4)
            result = await self.submit(client)
            self.assertTrue(result.is_error)
            self.assertEqual(len(self.fake.jobs), 0)

    async def test_disabled_capability_disappears_but_existing_job_is_queryable(self):
        async with self.client() as client:
            job_id = (await self.submit(client)).structured_content["job_id"]
            self.fake.capabilities[1]["availability"] = "unavailable"
            names = [t.name for t in (await client.list_tools(cache_mode="refresh")).tools]
            self.assertNotIn("ursa_classify_landcover", names)
            self.assertTrue((await self.submit(client)).is_error)
            self.assertFalse((await client.call_tool("ursa_get_job", {"job_id": job_id})).is_error)

    async def test_unexpected_arguments_are_not_forwarded(self):
        async with self.client() as client:
            before = len(self.fake.jobs)
            result = await client.call_tool("ursa_classify_landcover", {"input_id": "a" * 32, "metadata": SAMPLE_METADATA,
                "idempotency_key": "offline-submission-1234", "owner": "bob", "file_path": "/etc/passwd"})
            self.assertTrue(result.is_error)
            self.assertEqual(len(self.fake.jobs), before)

    async def test_bad_upstream_result_cannot_be_delivered_as_success(self):
        self.fake.bad_result = True
        async with self.client() as client:
            result = await self.submit(client)
            self.assertTrue(result.is_error)
            self.assertIn("RESULT_INVALID", result.content[0].text)

    async def test_authentication_precedes_mcp_and_revocation_is_immediate(self):
        self.assertEqual((await self.raw()).status_code, 401)
        self.assertEqual(self.fake.calls, [])
        headers = {"Authorization": "Bearer " + self.tokens["alice"]}
        self.assertNotEqual((await self.raw(headers=headers)).status_code, 401)
        self.records["identities"][0]["enabled"] = False
        self.save_records()
        self.assertEqual((await self.raw(headers=headers)).status_code, 401)

    async def test_accidental_upstream_secret_is_not_returned(self):
        self.fake.secret_result = True
        async with self.client() as client:
            result = await self.submit(client)
            self.assertTrue(result.is_error)
            self.assertNotIn("api-alice", result.content[0].text)
            self.assertIn("RESULT_CONTAINS_SECRET", result.content[0].text)

    async def test_distinct_identities_cannot_share_internal_api_key(self):
        self.records["identities"][1]["api_key"] = "api-alice"
        self.save_records()
        response = await self.raw(headers={"Authorization": "Bearer " + self.tokens["alice"]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.fake.calls, [])

    async def test_origin_host_and_forwarded_header_spoofing(self):
        headers = {"Authorization": "Bearer " + self.tokens["alice"]}
        self.assertEqual((await self.raw(headers={**headers, "Host": "attacker.invalid"})).status_code, 421)
        self.assertEqual((await self.raw(headers={**headers, "Origin": "https://attacker.invalid"})).status_code, 403)
        result = await self.raw(headers={**headers, "Host": "attacker.invalid", "X-Forwarded-Host": "example.invalid"})
        self.assertEqual(result.status_code, 421)

    async def test_public_health_does_not_claim_gpu_verification(self):
        result = await self.raw(method="GET", path="/health")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["gpu_inference_verified_by_health"])

    async def test_rate_and_message_size_are_bounded(self):
        self.app.limit = 2
        headers = {"Authorization": "Bearer " + self.tokens["alice"]}
        await self.raw(headers=headers)
        await self.raw(headers=headers)
        self.assertEqual((await self.raw(headers=headers)).status_code, 429)
        self.app.limit = 120
        result = await self.raw("POST", headers={**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, content=b"x" * 70000)
        self.assertEqual(result.status_code, 413)

    async def test_redirect_never_sends_secret_to_other_origin(self):
        calls = []
        def redirect(request):
            calls.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://attacker.invalid/mcp"})
        backend = ModelAPI("https://example.invalid/api", httpx.MockTransport(redirect))
        with self.assertRaises(ServiceError) as caught:
            await backend.request(Identity("alice", "api-alice"), "GET", "/v1/capabilities")
        self.assertEqual(caught.exception.code, "UPSTREAM_REDIRECT")
        self.assertEqual(len(calls), 1)

    async def test_credential_issuance_preserves_existing_identities(self):
        api = self.root / "api.local.json"
        api.write_text(json.dumps({"base_url": "https://example.invalid/api", "api_key": "api-new"}), encoding="utf-8")
        output = self.root / "new.local.json"
        issue(self.identity_file, api, output, "new", "https://example.invalid/mcp")
        client = json.loads(output.read_text())
        store = json.loads(self.identity_file.read_text())
        self.assertEqual(len(store["identities"]), 3)
        self.assertNotIn(client["mcp_token"], self.identity_file.read_text())
        with self.assertRaises(FileExistsError):
            issue(self.identity_file, api, output, "new", "https://example.invalid/mcp")


if __name__ == "__main__":
    unittest.main()
