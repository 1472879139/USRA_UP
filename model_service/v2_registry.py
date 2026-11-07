"""v2 已安装适配器目录；复用 v1 状态文件语义，使用独立状态文件。"""
from copy import deepcopy
from pathlib import Path

from .contract import APIError, canonical, sha256
from .registry import ToolRegistry
from .v2_contract import ToolAdapter, job_schema

CONTROL_NAMES = {"ursa_v2_get_tool_contract", "ursa_v2_get_job", "ursa_v2_cancel_job", "ursa_v2_get_artifacts"}


class AdapterRegistry(ToolRegistry):
    def __init__(self, adapters=(), state_file=None):
        self.state_file = Path(state_file) if state_file is not None else None
        self._tools, self._contracts = {}, {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter):
        if not isinstance(adapter, ToolAdapter):
            raise ValueError("适配器须继承 ToolAdapter")
        contract = adapter.spec.contract()
        if (contract["tool_id"] in self._tools or contract["mcp_name"] in CONTROL_NAMES
                or any(c["mcp_name"] == contract["mcp_name"] for c in self._contracts.values())):
            raise ValueError("v2 工具 ID 或 MCP 名称重复")
        if len(self._tools) >= 128:
            raise ValueError("本服务最多登记 128 个 v2 工具")
        self._tools[contract["tool_id"]] = adapter
        self._contracts[contract["tool_id"]] = contract

    def definition(self, tool_id):
        if not isinstance(tool_id, str) or tool_id not in self._tools:
            raise APIError(404, "TOOL_NOT_FOUND", "v2 工具未注册")
        return self._tools[tool_id]

    def snapshot(self, tool_id):
        self.definition(tool_id)
        return deepcopy(self._contracts[tool_id])

    def catalog(self, mode=None, availability=None):
        states = self.states()
        items = []
        for tool_id, adapter in self._tools.items():
            state = states.get(tool_id, {"enabled": True, "removed": False})
            if state["removed"]:
                continue
            item = self.snapshot(tool_id)
            item.update(enabled=state["enabled"], state="enabled" if state["enabled"] else "disabled",
                        availability=adapter.availability() if state["enabled"] else "unavailable")
            item["job_schema"] = job_schema(item)
            items.append(item)
        return {"api_version": "2.0.0", "tools": items,
                "registry_revision": sha256(canonical({"tools": items, "states": states}).encode("utf-8"))}

    def contract(self, tool_id, mode=None, availability=None):
        return next((t for t in self.catalog()["tools"] if t["tool_id"] == tool_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise APIError(404, "TOOL_NOT_FOUND", "v2 工具未注册或已移出目录")

    def list_contracts(self, mode=None, availability=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise APIError(400, "INVALID_PAGINATION", "limit 须为 1～50，offset 须为非负整数")
        value = self.catalog()
        total = len(value["tools"])
        value["tools"] = value["tools"][offset:offset + limit]
        more = offset + len(value["tools"]) < total
        return {**value, "total": total, "limit": limit, "offset": offset, "has_more": more,
                "next_offset": offset + len(value["tools"]) if more else None}
