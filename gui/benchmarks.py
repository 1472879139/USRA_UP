"""独立验收页面；不改变已有对话、角色与结果交付协议。"""

from pathlib import Path
from threading import Lock

from flask import jsonify, render_template, request, send_file


def register_benchmark_routes(app, ctrl):
    runs = {}
    lock = Lock()

    @app.get("/benchmarks")
    def benchmark_page():
        return render_template("benchmarks.html")

    @app.get("/api/benchmarks")
    def benchmark_catalog():
        from benchmarks.catalog import audit_catalog
        return jsonify(audit_catalog(app.config["PROJECT_ROOT"]))

    @app.post("/api/benchmarks/run")
    def benchmark_run():
        from benchmarks.runner import run_case
        from benchmarks.catalog import load_catalog
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(success=False, message="请选择案例"), 400
        case_id = data.get("case_id")
        if not isinstance(case_id, str) or case_id not in {c["id"] for c in load_catalog() if c["enabled"]}:
            return jsonify(success=False, message="该案例尚未接入自动评分"), 400
        with ctrl._ui_lock:
            worker = getattr(ctrl, "_thread", None)
            if ctrl._running or getattr(ctrl, "_benchmark_running", False) or (worker and worker.is_alive()):
                return jsonify(success=False, message="已有任务正在运行，请完成后再验收"), 409
            ctrl._benchmark_running = True
        try:
            report, output = run_case(Path(app.config["PROJECT_ROOT"]), case_id)
            with lock:
                runs[report["run_id"]] = (output.resolve(), frozenset(report["artifacts"]))
            return jsonify(success=True, report=report)
        except (ValueError, OSError, KeyError) as exc:
            app.logger.warning("验收失败：%s", exc)
            return jsonify(success=False, message="验收失败：" + str(exc)), 400
        except Exception:
            app.logger.exception("验收执行失败")
            return jsonify(success=False, message="验收执行失败，请查看终端日志；未将失败结果标为完成。"), 500
        finally:
            with ctrl._ui_lock:
                ctrl._benchmark_running = False

    @app.get("/api/benchmarks/artifact/<run_id>/<name>")
    def benchmark_artifact(run_id, name):
        with lock:
            record = runs.get(run_id)
        if not record or name not in record[1]:
            return jsonify(success=False, message="本次运行没有此文件"), 404
        root = record[0]
        path = root / name
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            return jsonify(success=False, message="文件不可用"), 404
        return send_file(path, as_attachment=name not in ("preview.png", "comparison.png"))
