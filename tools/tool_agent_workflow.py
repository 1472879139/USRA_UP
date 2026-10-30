"""DeepSeek工具Agent与现有Message/Session/执行核验链的兼容适配。"""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote

from core.message import Message, MessageType
from core.tool_call_loop import ToolCallLoop, ToolLoopError
from .agent_tools import AgentTools, TOOL_SPECS, input_identity
from .artifact_paths import confined_path, file_hash

DEFAULTS = {"enabled": False, "autonomous": False, "max_rounds": 12, "max_calls": 24, "max_seconds": 300, "max_tokens": 4096}
MANIFEST = "tool_agent_manifest.json"


def options(config: dict | None = None, *, enabled: bool = False, autonomous: bool = False) -> dict:
    config = config or {}
    if not isinstance(config, dict) or set(config)-set(DEFAULTS):
        raise ValueError("tool_agent配置字段不符")
    result = {**DEFAULTS, **config}
    if type(result["enabled"]) is not bool:
        raise ValueError("tool_agent.enabled必须为布尔值")
    if type(result["autonomous"]) is not bool:
        raise ValueError("tool_agent.autonomous必须为布尔值")
    result["autonomous"] = autonomous or result["autonomous"]
    result["enabled"] = enabled or result["enabled"] or result["autonomous"]
    for key, maximum in [("max_rounds", 30), ("max_calls", 60), ("max_seconds", 1800), ("max_tokens", 16384)]:
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError("tool_agent."+key+"超出允许范围")
    return result


def enabled(session) -> bool:
    return bool(getattr(session, "tool_agent_options", {}).get("enabled"))


def make_plan(session) -> dict:
    """锁定目标、显式选择输入和运行设置；不按关键词选择处理模型。"""
    from .io_kit import _get_project_root
    root = Path(session.data_context.get("data_root") or (Path(_get_project_root())/"data")).resolve().parent
    names = session.task_contract.get("inputs", [])
    if not names or len(names) > 8 or session.task_contract.get("input_ambiguities"):
        raise ValueError("请先在上方选择1～8幅本次分析的影像，不能由模型换用其他文件")
    if session.task_contract.get("needs_goal"):
        raise ValueError("请先说明需要解决的遥感问题")
    inputs = {f"input_{i}": {"name": name, "identity": input_identity(confined_path(root/"data", name))}
              for i, name in enumerate(names, 1)}
    settings = root/"gpu.local.json"
    return {"version": 1, "request": session.user_request, "revisions": list(session.revision_feedback),
            "inputs": inputs, "gpu_settings_sha256": file_hash(settings) if settings.is_file() else None,
            "limits": options(session.tool_agent_options), "task_subdir": session.result_subdir}


def confirmation(session) -> str:
    plan = make_plan(session)
    return ("将使用你选中的影像："+"、".join(item["name"] for item in plan["inputs"].values())+
            "。\n你的需求："+session.user_request+
            ("；调整："+"；".join(session.revision_feedback) if session.revision_feedback else "")+
            "\n确认后，由DeepSeek检查已安装工具并自主选择、组合步骤；每步接收真实结果或错误后再决定下一步。"
            "只处理本次数据，不自动安装、下载模型或读取答案。条件不足会说明未完成项。"
            f"最多{plan['limits']['max_rounds']}轮模型请求、{plan['limits']['max_calls']}次工具调用；按原API计费。"
            "\n\n回复approve确认，或提出修改意见。")


def make_message(session) -> Message:
    plan = make_plan(session)
    session.tool_plan, session.remote_tool_plan = {}, {}
    session.tool_agent_plan = deepcopy(plan)
    return Message(sender="Scientist", receiver="Engineer", content="已锁定需求和输入，交由DeepSeek工具调用循环选择步骤。",
                   metadata={"tool_agent_request": deepcopy(plan)})


def code_message(session, request: dict) -> Message:
    if request != session.tool_agent_plan:
        return Message(sender="Engineer", receiver="Manager", msg_type=MessageType.ERROR, content="工具Agent计划与本次确认不一致")
    return Message(sender="Engineer", receiver="Executor", msg_type=MessageType.CODE,
                   content="受控工具调用任务：只执行注册工具，不执行模型生成代码。",
                   metadata={"tool_agent_request": deepcopy(request)})


def execute_message(executor, message: Message) -> Message:
    """执行器持有原API客户端；密钥不放入请求计划、子脚本或产物。"""
    session = executor.session
    plan = message.metadata.get("tool_agent_request")
    if not enabled(session) or plan != session.tool_agent_plan:
        return Message(sender="Executor", receiver="Manager", msg_type=MessageType.ERROR, content="未批准的工具Agent计划")
    root = Path(executor._project_root).resolve()
    output = confined_path(root/"results", session.result_subdir)
    if output.exists() and any(output.iterdir()):
        return Message(sender="Executor", receiver="Manager", msg_type=MessageType.ERROR, content="本次工具任务目录已有内容，保留现场，不覆盖或自动重跑")
    output.mkdir(parents=True, exist_ok=True)
    runtime = AgentTools(root, output, plan)
    request_token = None
    def notify(event):
        nonlocal request_token
        if event["event"] == "request_started":
            request_token = session.begin_llm_call("DeepSeek工具Agent")
            return
        if event["event"] == "request_finished":
            session.end_llm_call(request_token)
            request_token = None
            return
        session.tracer.add_event("tool_call" if event["event"].startswith("tool_") else "llm_call",
                                agent="DeepSeek工具Agent", data=event)
        session.emit_event("tool_agent_progress", event)
    limits = {key: value for key, value in plan["limits"].items() if key not in {"enabled", "autonomous"}}
    loop = ToolCallLoop(executor._client, executor.model, active=session.is_active, notify=notify, **limits)
    from .registry import get_agent_tool_schemas
    prompt = (root/"prompts/tool_agent_prompt.txt").read_text(encoding="utf-8")
    user = {"original_request": plan["request"], "ordered_revisions": plan["revisions"],
            "approved_inputs": [{"input_id": key, "file_name": item["name"]} for key, item in plan["inputs"].items()]}
    result, failure = None, None
    try:
        runtime.verify_inputs()
        result = loop.run([{"role": "system", "content": prompt},
                           {"role": "user", "content": json.dumps(user, ensure_ascii=False)}], get_agent_tool_schemas(), runtime.execute)
    except (ToolLoopError, ValueError, OSError) as exc:
        failure = str(exc)
    (output/"tool_agent_calls.json").write_text(json.dumps({"calls": loop.events, "usage": loop.usage, "error": failure}, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.relative_to(output).as_posix(): file_hash(p) for p in output.rglob("*") if p.is_file()}
    payload = {"version": 1, "plan": plan, "completion": result, "error": failure,
               "artifacts": runtime.artifacts, "usage": loop.usage, "files": files}
    manifest = output/MANIFEST
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    session.tool_agent_receipt = {"manifest_sha256": file_hash(manifest)}
    if failure or not result or result["status"] == "blocked":
        reason = failure or "；".join(result["unfulfilled"])
        return Message(sender="Executor", receiver="Manager", msg_type=MessageType.ERROR,
                       content="工具Agent尚未完成："+reason+"；过程和已有文件保留在"+session.result_relative_dir)
    return Message(sender="Executor", receiver="Manager", msg_type=MessageType.RESULT,
                   content="DeepSeek工具执行完成：\n"+"\n".join(result["facts"]),
                   metadata={"execution_success": True, "files_generated": [session.result_subdir+"/"+name for name in [*files, MANIFEST]]})


def attach_audit(session, project_root: str | Path) -> None:
    """Supervisor重新核对执行器收据、输入与当前文件，不发起模型推理。"""
    root = Path(project_root).resolve();output = confined_path(root/"results", session.result_subdir)
    manifest = output/MANIFEST
    if file_hash(manifest) != session.tool_agent_receipt.get("manifest_sha256"):
        raise ValueError("工具Agent交付清单已改变")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if value["plan"] != session.tool_agent_plan or value["error"] or not value["completion"]:
        raise ValueError("工具Agent任务清单或完成状态不符")
    allowed = set(session.result_audit["files"])
    expected = {session.result_subdir+"/"+name for name in [*value["files"], MANIFEST]}
    if allowed != expected:
        raise ValueError("工具Agent文件不在本次实际生成清单中")
    for name, digest in value["files"].items():
        if file_hash(confined_path(output, name)) != digest:
            raise ValueError("工具Agent实际产物哈希不符")
    runtime = AgentTools(root, output, value["plan"])
    runtime.artifacts = value["artifacts"]
    runtime.verify_inputs();runtime.tool_validate_artifacts()
    completion = value["completion"]
    for key in completion["artifact_ids"]:
        runtime.artifact(key)
    audit = session.result_audit
    audit["tool_agent"] = {"completion": completion, "artifacts": value["artifacts"]}
    audit["status"] = "needs_review"
    audit["facts"] += "\n[DeepSeek工具Agent本次事实]\n"+"\n".join(completion["facts"])
    audit["warnings"].append("已核对本次工具调用和产物；没有参考答案，识别语义仍待独立评价。")


def enrich_delivery(delivery: dict, audit: dict, subdir: str) -> dict:
    value = audit["tool_agent"];result = value["completion"]
    delivery["completion"] = "candidate"
    delivery["headline"] = "已生成本次计算结果和图示，识别含义尚需核实。" if result["status"] == "completed" else "本次已完成部分处理，仍有需求未完成。"
    delivery["findings"] = result["facts"]
    delivery["limitations"] += result["unfulfilled"]
    delivery["verification"] = "DeepSeek根据工具返回选择步骤；已复核本次输入、工具产物、统计和执行收据，未读取参考答案。"
    delivery["professional_report"] = "DeepSeek补充说明（仍须结合本次事实）：\n"+result["summary"]
    for item in value["artifacts"].values():
        if item["kind"] == "image":
            url = "/api/result_file?path=" + quote(subdir+"/"+item["path"], safe="")
            for file in delivery["files"]:
                if file["url"] == url:
                    file["name"] = item.get("label", file["name"])
    delivery["scene_results"] = []
    delivery["next_step"] = "先查看结果图和未完成项；语义精度需封存后由独立评价端核对。"
    return delivery
