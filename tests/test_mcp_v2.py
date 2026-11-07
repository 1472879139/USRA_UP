"""真实 MCP SDK → 本机 HTTP v2 API → 模拟适配器，不运行真实模型。"""
from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

try:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from model_service.v2_adapters import build_registry
    from model_service.server import APIServer, Service
    from ursa_mcp.server import create_app
    from tool_client_v2 import ToolClientV2
    from test_tool_registry import OfflineBackend
    from test_tools_v2 import LocalAdapter
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, "请使用独立 MCP 环境运行 v2 SDK 测试")
class MCPV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        backend = OfflineBackend()
        self.registry = build_registry(backend, self.root / "v2-tools.local.json", True)
        self.registry.initialize()
        self.tokens = {owner: "v2-mcp-" + owner + "-" + "a" * 40 for owner in ["alice", "bob"]}
        api_keys = {owner: "v2-offline-api-" + owner for owner in self.tokens}
        self.service = Service(self.root / "data", backend,
            {owner: hashlib.sha256(key.encode()).hexdigest() for owner, key in api_keys.items()}, v2_registry=self.registry)
        self.server = APIServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.api = ToolClientV2(self.url, api_keys["alice"])
        self.identity_file = self.root / "identities.local.json"
        self.records = {"identities": [{"owner": owner, "api_key": key, "enabled": True,
            "token_sha256": hashlib.sha256(self.tokens[owner].encode()).hexdigest(),
            "scopes": ["read", "submit", "cancel"]} for owner, key in api_keys.items()]}
        self.save_identities()
        self.app = create_app({"api_base_url": self.url, "public_api_base_url": "https://example.invalid/api",
            "public_mcp_url": "http://localhost/mcp", "identities_file": str(self.identity_file), "enable_v2": True})
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        async def lifespan():
            async with self.app.app.router.lifespan_context(self.app.app):
                self.started.set()
                await self.stopped.wait()
        self.lifespan = asyncio.create_task(lifespan())
        await self.started.wait()

    async def asyncTearDown(self):
        self.stopped.set()
        await self.lifespan
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.close()

    def save_identities(self):
        self.identity_file.write_text(json.dumps(self.records), encoding="utf-8")

    @asynccontextmanager
    async def client(self, owner="alice", mode="auto"):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
            headers={"Authorization": "Bearer " + self.tokens[owner]}, trust_env=False) as http:
            async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode, read_timeout_seconds=10) as client:
                yield client

    async def summary(self, client, key="v2-mcp-summary-key-1234"):
        return await client.call_tool("ursa_v2_demo_json_summary",
            {"inputs": {"values": [1, 2, 3]}, "parameters": {"scale": 2}, "idempotency_key": key})

    async def finish(self, client, job_id):
        for _ in range(60):
            response = await client.call_tool("ursa_v2_get_job", {"job_id": job_id})
            self.assertFalse(response.is_error)
            if response.structured_content["status"] in {"succeeded", "failed", "cancelled"}:
                return response.structured_content
            await asyncio.sleep(0.02)
        self.fail("隔离任务未及时终结")

    async def test_modern_and_legacy_discover_both_api_versions_with_typed_inputs(self):
        for mode in ["auto", "legacy"]:
            async with self.client(mode=mode) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools.tools), 15)
                model = next(t for t in tools.tools if t.name == "ursa_v2_classify_landcover")
                self.assertEqual(model.input_schema["properties"]["inputs"]["required"], ["image"])
                csv = next(t for t in tools.tools if t.name == "ursa_v2_demo_csv_compare")
                self.assertEqual(set(csv.input_schema["properties"]["inputs"]["required"]), {"before", "after"})
                detail = await client.call_tool("ursa_v2_get_tool_contract", {"tool_id": "demo-json-summary"})
                self.assertFalse(detail.is_error)
                self.assertEqual(detail.structured_content["input_slots"]["values"]["kind"], "json")

    async def test_json_submit_query_idempotency_and_artifact_download(self):
        async with self.client() as client:
            first = await self.summary(client)
            self.assertFalse(first.is_error)
            repeated = await self.summary(client)
            self.assertEqual(first.structured_content["job_id"], repeated.structured_content["job_id"])
            completed = await self.finish(client, first.structured_content["job_id"])
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["result"]["outputs"]["sum"], 12)
            files = await client.call_tool("ursa_v2_get_artifacts", {"job_id": completed["job_id"]})
            self.assertFalse(files.is_error)
            for artifact in files.structured_content["artifacts"]:
                self.assertTrue(artifact["download_url"].startswith("https://example.invalid/api/v2/artifacts/"))
                self.assertTrue(self.api.download(artifact, self.root / "downloads").is_file())

    async def test_file_uploads_and_csv_schema_are_used_through_mcp(self):
        inputs = {}
        for name, text in [("before", "value\n1\n"), ("after", "value\n5\n")]:
            path = self.root / (name + ".csv")
            path.write_text(text, encoding="utf-8")
            inputs[name] = {"file_id": self.api.upload(path, "text/csv")["file_id"]}
        async with self.client() as client:
            invalid = await client.call_tool("ursa_v2_demo_csv_compare",
                {"inputs": inputs, "parameters": {}, "idempotency_key": "csv-invalid-key-12345"})
            self.assertTrue(invalid.is_error)
            valid = await client.call_tool("ursa_v2_demo_csv_compare",
                {"inputs": inputs, "parameters": {"column": "value"}, "idempotency_key": "csv-valid-key-123456"})
            completed = await self.finish(client, valid.structured_content["job_id"])
            self.assertEqual(completed["result"]["outputs"]["delta"], 4)

    async def test_query_cancel_and_artifacts_preserve_owner_after_tool_removal(self):
        async with self.client() as alice:
            submitted = await self.summary(alice)
            job_id = submitted.structured_content["job_id"]
            await self.finish(alice, job_id)
            self.registry.change("demo-json-summary", "remove")
            self.assertNotIn("ursa_v2_demo_json_summary", [t.name for t in (await alice.list_tools()).tools])
            self.assertFalse((await alice.call_tool("ursa_v2_get_job", {"job_id": job_id})).is_error)
            self.assertFalse((await alice.call_tool("ursa_v2_get_artifacts", {"job_id": job_id})).is_error)
        async with self.client("bob") as bob:
            for name in ["ursa_v2_get_job", "ursa_v2_cancel_job", "ursa_v2_get_artifacts"]:
                denied = await bob.call_tool(name, {"job_id": job_id})
                self.assertTrue(denied.is_error)
                self.assertIn("NOT_FOUND", denied.content[0].text)

    async def test_read_only_identity_cannot_submit_or_cancel_v2(self):
        self.records["identities"][0]["scopes"] = ["read"]
        self.save_identities()
        async with self.client() as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools.tools), 7)
            self.assertTrue((await self.summary(client)).is_error)

    async def test_local_refs_inside_named_json_slots_are_mounted_correctly(self):
        adapter = LocalAdapter()
        adapter.spec = replace(adapter.spec, inputs={"value": {"kind": "json",
            "schema": {"$defs": {"number": {"type": "integer"}}, "$ref": "#/$defs/number"}}},
            required_inputs=("value",))
        self.registry.register(adapter)
        async with self.client() as client:
            response = await client.call_tool("ursa_v2_test_local",
                {"inputs": {"value": 3}, "parameters": {}, "idempotency_key": "local-refs-valid-1234"})
            self.assertFalse(response.is_error)
            completed = await self.finish(client, response.structured_content["job_id"])
            self.assertEqual(completed["result"]["outputs"], [1, 2, 3])
            invalid = await client.call_tool("ursa_v2_test_local",
                {"inputs": {"value": "invalid"}, "parameters": {}, "idempotency_key": "local-refs-invalid-123"})
            self.assertTrue(invalid.is_error)


if __name__ == "__main__":
    unittest.main()
