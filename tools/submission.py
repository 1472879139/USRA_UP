"""不读答案的答卷封存：复制本任务白名单产物，哈希校验，禁止覆盖已有交卷。"""
import json
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from core.state_machine import ConversationPhase
from .artifact_paths import confined_path, file_hash


def verify_submission(directory, expected_hash):
    """评分前后均核对外部持有的清单摘要与全部文件；这不是数字签名。"""
    root = Path(directory).resolve()
    manifest = root/"submission.json"
    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash or "") or manifest.is_symlink() or file_hash(manifest) != expected_hash:
        raise ValueError("答卷清单哈希不一致或收据无效")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("artifacts"), dict):
        raise ValueError("不支持的答卷结构")
    expected_files = {"submission.json", *data["artifacts"]}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("答卷不允许链接文件")
        if path.is_file() and path.relative_to(root).as_posix() not in expected_files:
            raise ValueError("答卷包含清单外文件")
    for name, digest in data["artifacts"].items():
        path = confined_path(root, name)
        if not path.is_file() or file_hash(path) != digest:
            raise ValueError("答卷产物哈希不一致：" + name)
    input_hashes = {row["sha256"] for row in data.get("inputs", [])}
    for scene in data.get("scenes", []):
        if scene["prediction"] not in data["artifacts"] or scene["input_sha256"] not in input_hashes:
            raise ValueError("答卷逐幅预测或输入不在封存清单内")
    return data


def freeze_submission(session, project_root):
    """终态会话封存一次；失败任务也交卷，正常运行不加载任何答案配置。"""
    if session.state_machine.get_current_phase() not in (ConversationPhase.COMPLETED, ConversationPhase.TERMINATED):
        raise ValueError("任务尚未结束，不能交卷")
    if not session.user_request:
        raise ValueError("没有用户任务可交卷")
    root = Path(project_root).resolve()
    task = confined_path(root, session.result_relative_dir)
    task.relative_to(root/"results")
    target = task/"submission"
    receipt = getattr(session, "submission", {})
    if receipt:
        verify_submission(target, receipt["sha256"])
        return receipt
    if target.exists():
        raise ValueError("答卷已存在，不能重新覆盖；请使用原收据核对")
    task.mkdir(parents=True, exist_ok=True)
    staging = task/("submission_stage_" + uuid.uuid4().hex)
    staging.mkdir()
    artifacts, missing, inputs = {}, [], []
    for name in session.result_audit.get("files", []):
        source = confined_path(root/"results", name)
        source.relative_to(task)
        if source.is_symlink():
            raise ValueError("不能封存链接产物")
        if not source.is_file():
            missing.append(name)
            continue
        relative = "artifacts/" + source.relative_to(task).as_posix()
        copied = confined_path(staging, relative)
        copied.parent.mkdir(parents=True, exist_ok=True)
        digest = file_hash(source)
        shutil.copy2(source, copied)
        if file_hash(copied) != digest or file_hash(source) != digest:
            raise ValueError("封存期间产物发生改变")
        artifacts[relative] = digest
    planned = {row["file_name"]: row["sha256"] for row in session.tool_plan.get("inputs", [])}
    for name in session.task_contract.get("inputs", []):
        path = confined_path(root/"data", name)
        if path.is_file():
            digest = file_hash(path)
            if name in planned and digest != planned[name]:
                raise ValueError("封存时输入与执行计划不一致")
            inputs.append({"name": name, "sha256": digest})
    from .result_delivery import build_result_delivery
    delivery = session.result_delivery or build_result_delivery(session, root)
    outcome = ("not_completed" if session.is_terminated() or delivery.get("completion") in ("failed", "blocked") or missing
               else "completed")
    source_root = Path(__file__).resolve().parents[1]
    code_paths = [*source_root.glob("agents/*.py"), *source_root.glob("core/*.py"), *source_root.glob("tools/*.py"),
                  *source_root.glob("gui/*.py"), *source_root.glob("gui/templates/*.html"), *source_root.glob("prompts/*.txt"),
                  source_root/"main.py", source_root/"environment.yml", source_root/"requirements.txt"]
    scenes = [{"input_sha256": scene["input_sha256"], "label": scene["label"],
               "task_type": scene.get('task_type', 'segmentation'),
               "tool_version": scene.get('tool_version', ''),
               "prediction": "artifacts/" + Path(scene["prediction_path"]).relative_to(session.result_subdir).as_posix()}
              for scene in session.result_audit.get("scenes", [])]
    data = {"schema": 1, "task_id": session.result_subdir, "sealed_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome, "stop_reason": session.get_terminate_reason(),
            "request": session.user_request, "revisions": session.revision_feedback,
            "task_contract": session.task_contract, "inputs": inputs, "tool_plan": session.tool_plan,
            "delivery": delivery, "artifacts": artifacts, "missing_artifacts": missing, "scenes": scenes,
            "trace_summary": session.tracer.get_summary(),
            "execution_trace": [{"type": event.event_type, "agent": event.agent, "duration_ms": event.duration_ms,
                                 "status": event.status, "data": {key: value for key, value in event.data.items()
                                    if key in ("tool_name", "tool_version", "attempt", "max_attempts", "exit_code", "error",
                                               "model", "total_tokens", "prompt_tokens", "completion_tokens")}}
                                for event in session.tracer.get_events()
                                if event.event_type in ("code_execution", "tool_call", "llm_call")],
            "source_hashes": {p.relative_to(source_root).as_posix(): file_hash(p) for p in code_paths},
            "isolation": "application_separation_only",
            "limitation": "本机代码路径分离不等于操作系统权限隔离；严格测试需将干净运行包置于无法访问答案的环境。"}
    for scene in scenes:
        if scene["prediction"] not in artifacts:
            raise ValueError("逐幅预测不在本次封存清单内")
    manifest = staging/"submission.json"
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    digest = file_hash(manifest)
    staging.rename(target)
    verify_submission(target, digest)
    archive = task/"submission.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(target).as_posix())
    receipt = {"directory": target.relative_to(root).as_posix(), "sha256": digest,
               "archive_sha256": file_hash(archive), "task_id": session.result_subdir}
    session.submission = receipt
    return receipt
