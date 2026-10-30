"""答卷交接与开发者评分入口；评分不会调用模型或写回分析会话。"""
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

from flask import jsonify, render_template, request, send_file

from core.state_machine import ConversationPhase
from tools.submission import freeze_submission, verify_submission
from tools.artifact_paths import file_hash


def register_assessment_routes(app, ctrl):
    receipts, reports = {}, {}
    lock = Lock()

    @app.get("/assessment")
    def page():
        return render_template("assessment.html")

    @app.post("/api/submissions/freeze")
    def freeze():
        with ctrl._ui_lock:
            worker = getattr(ctrl, "_thread", None)
            session = getattr(ctrl, "session", None)
            if ctrl._running or getattr(ctrl, "_benchmark_running", False) or (worker and worker.is_alive()):
                return jsonify(success=False, message="任务仍在运行，请结束后交卷"), 409
            if not session or session.state_machine.get_current_phase() not in (ConversationPhase.COMPLETED, ConversationPhase.TERMINATED):
                return jsonify(success=False, message="没有已结束的任务可交卷"), 400
            try:
                receipt = freeze_submission(session, app.config["PROJECT_ROOT"])
                with lock:
                    receipts[receipt["task_id"]] = receipt
                return jsonify(success=True, receipt=receipt, download="/api/submissions/download/" + receipt["task_id"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return jsonify(success=False, message="交卷失败：" + str(exc)), 400

    @app.get("/api/submissions")
    def listing():
        with lock:
            return jsonify(receipts=list(receipts.values()),
                           scorer_available=(Path(__file__).resolve().parents[1]/"benchmarks/assessment.py").is_file())

    @app.get("/api/submissions/download/<task_id>")
    def download(task_id):
        with lock:
            receipt = receipts.get(task_id)
        if not receipt:
            return jsonify(success=False, message="没有此交卷收据"), 404
        try:
            folder = Path(app.config["PROJECT_ROOT"])/receipt["directory"]
            verify_submission(folder, receipt["sha256"])
            if file_hash(folder.parent/"submission.zip") != receipt["archive_sha256"]:
                raise ValueError("答卷ZIP哈希不一致")
            return send_file(folder.parent/"submission.zip", as_attachment=True, download_name=task_id+"_submission.zip")
        except (OSError, ValueError):
            return jsonify(success=False, message="答卷完整性检查失败"), 400

    @app.post("/api/assessment/grade")
    def grade():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("task_id"), str):
            return jsonify(success=False, message="请选择已交卷任务"), 400
        with lock:
            receipt = receipts.get(data["task_id"])
        if not receipt:
            return jsonify(success=False, message="没有此交卷收据"), 404
        with ctrl._ui_lock:
            worker = getattr(ctrl, "_thread", None)
            if ctrl._running or getattr(ctrl, "_benchmark_running", False) or (worker and worker.is_alive()):
                return jsonify(success=False, message="分析仍在运行，暂不读取答案"), 409
            ctrl._benchmark_running = True
        try:
            from benchmarks.assessment import grade_submission
            root = Path(app.config["PROJECT_ROOT"])
            report = grade_submission(root/receipt["directory"], receipt["sha256"], root)
            report_id = uuid4().hex
            output = root/"exports"/"assessments"/(report_id+".json")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            with lock:
                reports[report_id] = output
            return jsonify(success=True, report=report, download="/api/assessment/report/"+report_id)
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            return jsonify(success=False, message="外部评分失败："+str(exc)), 400
        finally:
            with ctrl._ui_lock:
                ctrl._benchmark_running = False

    @app.get("/api/assessment/report/<report_id>")
    def report_file(report_id):
        with lock:
            path = reports.get(report_id)
        if not path:
            return jsonify(success=False, message="报告不存在"), 404
        return send_file(path, as_attachment=True)
