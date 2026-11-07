"""内置模型桥接和显式启用的模拟适配器；不下载模型或执行用户代码。"""
import csv
from copy import deepcopy
import io
import math

from .backends import BackendError
from .contract import INPUT_CONTRACT, canonical
from .registry import builtin_definitions, standalone_schema
from .v2_contract import AdapterResult, ProducedArtifact, ToolAdapter, ToolSpec, object_schema
from .v2_registry import AdapterRegistry

MEDIA = {"classification": ["image/tiff"], "field_boundaries": ["application/geo+json"],
         "statistics": ["text/csv"], "preview": ["image/png"], "provenance": ["application/json"]}


class LegacyModelAdapter(ToolAdapter):
    def __init__(self, definition, backend):
        from .openapi import specification
        self.backend = backend
        result = standalone_schema("Result", specification())
        result["properties"].pop("artifacts")
        result["required"].remove("artifacts")
        result.get("$defs", {}).pop("Artifact", None)
        cap = definition.capability
        result["properties"]["capability_id"] = {"const": cap["id"]}
        self.spec = ToolSpec(cap["id"], cap["display_name"], "通过 v2 命名 image 输入调用既有模型；保持原有输入限制和候选结果标记。",
            cap["model_revision"], "ursa_v2_" + definition.mcp_name.removeprefix("ursa_"),
            {"image": {"kind": "file", "media_types": ["image/tiff"], "max_bytes": INPUT_CONTRACT["max_upload_bytes"],
                       "requirements": deepcopy(INPUT_CONTRACT)}}, ("image",), deepcopy(definition.metadata_schema),
            result, {role: MEDIA[role] for role in cap["outputs"]}, backend.mode if backend is not None else "gpu")

    def availability(self):
        return self.backend.availability().get(self.spec.tool_id, "unavailable") if self.backend is not None else "unavailable"

    def execute(self, context, inputs, parameters):
        context.check_active()
        if self.backend is None:
            raise BackendError("MODEL_UNAVAILABLE", "既有模型执行器未配置")
        image = inputs["image"]
        task = {"id": context.job_id, "capability_id": self.spec.tool_id, "input_id": image.file_id,
                "input": {"sha": image.sha256}, "payload": {"metadata": parameters}}
        result = self.backend.run(task, context.job_dir, image.path, context.cancelled)
        files = result.pop("files")
        context.check_active()
        return AdapterResult(result, [ProducedArtifact(f["name"], f["role"], f["media_type"]) for f in files])


class JSONSummaryExample(ToolAdapter):
    spec = ToolSpec("demo-json-summary", "模拟 JSON 数组统计", "仅用于本机协议验收：统计 values 数组，生成合成演示记录，不调用模型。",
        "1.0.0", "ursa_v2_demo_json_summary",
        {"values": {"kind": "json", "schema": {"type": "array", "minItems": 1, "maxItems": 1024,
                                             "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}}}},
        ("values",), object_schema({"scale": {"type": "number", "minimum": -100, "maximum": 100}}),
        object_schema({"count": {"type": "integer", "minimum": 1}, "sum": {"type": "number"},
                       "mean": {"type": "number"}, "synthetic_demo": {"const": True}},
                      ["count", "sum", "mean", "synthetic_demo"]),
        {"report": ["application/json"], "statistics": ["text/csv"]}, "mock")

    def execute(self, context, inputs, parameters):
        context.check_active()
        values = [float(x) * parameters.get("scale", 1) for x in inputs["values"]]
        total = math.fsum(values)
        outputs = {"count": len(values), "sum": total, "mean": total / len(values), "synthetic_demo": True}
        context.results_dir.mkdir(exist_ok=True)
        (context.results_dir / "summary.json").write_text(canonical(outputs), encoding="utf-8")
        (context.results_dir / "summary.csv").write_text("count,sum,mean\n" + f"{len(values)},{total},{outputs['mean']}\n", encoding="utf-8")
        return AdapterResult(outputs, [ProducedArtifact("summary.json", "report", "application/json"),
                                       ProducedArtifact("summary.csv", "statistics", "text/csv")])


class CSVCompareExample(ToolAdapter):
    spec = ToolSpec("demo-csv-compare", "模拟双 CSV 对比", "仅用于本机协议验收：对比 before 和 after 两份 CSV 指定数值列的合计，不代表遥感分析。",
        "1.0.0", "ursa_v2_demo_csv_compare",
        {name: {"kind": "file", "media_types": ["text/csv"], "max_bytes": 1024 * 1024} for name in ["before", "after"]},
        ("before", "after"), object_schema({"column": {"type": "string", "minLength": 1, "maxLength": 64}}, ["column"]),
        object_schema({"column": {"type": "string"}, "before_total": {"type": "number"},
                       "after_total": {"type": "number"}, "delta": {"type": "number"}, "synthetic_demo": {"const": True}},
                      ["column", "before_total", "after_total", "delta", "synthetic_demo"]),
        {"comparison": ["text/csv"], "report": ["application/json"]}, "mock")

    @staticmethod
    def total(file, column, context):
        try:
            reader = csv.DictReader(io.StringIO(file.path.read_text(encoding="utf-8-sig")))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or column not in reader.fieldnames:
                raise ValueError("CSV 缺少指定列或列名重复")
            values = []
            for index, row in enumerate(reader):
                context.check_active()
                if index >= 10000:
                    raise ValueError("CSV 超过 10000 行")
                value = float(row[column])
                if not math.isfinite(value) or abs(value) > 1000000000:
                    raise ValueError("CSV 数值不受支持")
                values.append(value)
            return math.fsum(values)
        except (ValueError, KeyError, TypeError, UnicodeError, csv.Error) as exc:
            raise BackendError("INPUT_UNSUPPORTED", "CSV 须为 UTF-8，指定列须为有限数值且不超过 10000 行") from exc

    def execute(self, context, inputs, parameters):
        context.check_active()
        column = parameters["column"]
        before = self.total(inputs["before"], column, context)
        after = self.total(inputs["after"], column, context)
        outputs = {"column": column, "before_total": before, "after_total": after, "delta": after - before, "synthetic_demo": True}
        context.results_dir.mkdir(exist_ok=True)
        (context.results_dir / "comparison.json").write_text(canonical(outputs), encoding="utf-8")
        (context.results_dir / "comparison.csv").write_text(f"before_total,after_total,delta\n{before},{after},{after-before}\n", encoding="utf-8")
        return AdapterResult(outputs, [ProducedArtifact("comparison.json", "report", "application/json"),
                                       ProducedArtifact("comparison.csv", "comparison", "text/csv")])


def build_registry(backend=None, state_file=None, examples=False, extra_adapters=()):
    adapters = [LegacyModelAdapter(definition, backend) for definition in builtin_definitions()]
    if examples:
        adapters.extend([JSONSummaryExample(), CSVCompareExample()])
    adapters.extend(extra_adapters)
    return AdapterRegistry(adapters, state_file)
