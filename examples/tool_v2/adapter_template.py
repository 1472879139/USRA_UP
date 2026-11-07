"""可运行的接入模板：将命名 JSON 文本转换为长度结果和 JSON 文件。"""
from model_service.contract import canonical
from model_service.v2_contract import AdapterResult, ProducedArtifact, ToolAdapter, ToolSpec, object_schema


class TextLengthAdapter(ToolAdapter):
    spec = ToolSpec(
        tool_id="example-text-length",
        title="文本长度接入模板",
        description="本地模拟示例，统计 text 的字符数；不调用模型。",
        version="1.0.0",
        mcp_name="ursa_v2_example_text_length",
        inputs={"text": {"kind": "json", "schema": {"type": "string", "maxLength": 4096}}},
        required_inputs=("text",),
        parameters_schema=object_schema(),
        output_schema=object_schema({"length": {"type": "integer", "minimum": 0}, "synthetic_demo": {"const": True}},
                                    ["length", "synthetic_demo"]),
        artifact_types={"report": ["application/json"]},
        execution_mode="mock",
    )

    def execute(self, context, inputs, parameters):
        context.check_active()
        outputs = {"length": len(inputs["text"]), "synthetic_demo": True}
        context.results_dir.mkdir(exist_ok=True)
        (context.results_dir / "length.json").write_text(canonical(outputs), encoding="utf-8")
        return AdapterResult(outputs, [ProducedArtifact("length.json", "report", "application/json")])
