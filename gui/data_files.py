"""普通用户的文件选择、有限缩略预览及本任务结果下载。"""
from io import BytesIO
from pathlib import Path
import re
import uuid

from flask import jsonify, request, send_file


def data_path(project_root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("请选择数据列表中的影像")
    root = (Path(project_root) / "data").resolve()
    root.relative_to(Path(project_root).resolve())
    candidate = root / name
    path = candidate.resolve()
    path.relative_to(root)
    if candidate.is_symlink() or path.suffix.lower() not in (".tif", ".tiff") or not path.is_file():
        raise ValueError("影像不存在或格式不支持")
    return path


def validate_selected_inputs(project_root, names):
    if not isinstance(names, list) or len(names) > 20:
        raise ValueError("请从列表选择最多20幅影像")
    selected = []
    for name in names:
        try:
            path = data_path(project_root, name)
            selected.append(path.relative_to((Path(project_root) / "data").resolve()).as_posix())
        except (ValueError, OSError, TypeError):
            raise ValueError("选择的文件不在数据目录中或已不可用") from None
    return list(dict.fromkeys(selected))


def register_data_routes(app, ctrl):
    """只暴露data/内的TIFF和当前任务核验清单内的结果；不启动模型。"""
    @app.get("/api/input_files")
    def input_files():
        from tools.data_context import load_data_context
        snapshot = load_data_context(Path(app.config["PROJECT_ROOT"]) / "data")
        return jsonify({"files": [{key: item.get(key) for key in
                                  ("file_name", "readable", "width", "height", "band_count")}
                                 for item in snapshot["files"]]})

    @app.get("/api/input_preview")
    def input_preview():
        try:
            path = data_path(app.config["PROJECT_ROOT"], request.args.get("path", ""))
            import numpy as np
            import rasterio
            from PIL import Image
            with rasterio.open(path) as src:
                height = max(1, round(src.height * min(1, 512 / max(src.width, src.height))))
                width = max(1, round(src.width * min(1, 512 / max(src.width, src.height))))
                names = [str(n).lower() for n in src.descriptions]
                indexes = [names.index(n) + 1 for n in ("red", "green", "blue")] if all(n in names for n in ("red", "green", "blue")) else list(range(1, min(3, src.count) + 1))
                values = src.read(indexes, out_shape=(len(indexes), height, width), masked=True)
                rendered = []
                for band in values:
                    valid = band.compressed()
                    valid = valid[np.isfinite(valid)]
                    low, high = np.percentile(valid, [2, 98]) if valid.size else (0, 1)
                    scaled = np.clip((band.filled(low).astype(float) - low) / max(high - low, 1e-9), 0, 1)
                    rendered.append(np.nan_to_num(scaled * 255).astype("uint8"))
                while len(rendered) < 3:
                    rendered.append(rendered[0])
                image = Image.fromarray(np.stack(rendered, axis=-1))
                stream = BytesIO()
                image.save(stream, format="PNG")
                stream.seek(0)
            return send_file(stream, mimetype="image/png")
        except (ValueError, OSError, TypeError):
            return jsonify({"success": False, "message": "无法生成该影像的预览"}), 404

    @app.post("/api/upload_inputs")
    def upload_inputs():
        with ctrl._ui_lock:
            worker = getattr(ctrl, "_thread", None)
            if ctrl._running or (worker and worker.is_alive()):
                return jsonify({"success": False, "message": "分析正在进行，请完成后再添加数据。"}), 409
            uploads = request.files.getlist("files")
            if not uploads or len(uploads) > 8:
                return jsonify({"success": False, "message": "每次请选择1至8个TIFF文件，总计不超过128MB。"}), 400
            root = (Path(app.config["PROJECT_ROOT"]) / "data").resolve()
            folder = root / "uploads" / uuid.uuid4().hex
            folder.resolve().relative_to(root)
            created, names = [], []
            try:
                folder.mkdir(parents=True, exist_ok=False)
                for upload in uploads:
                    suffix = Path(upload.filename or "").suffix.lower()
                    if suffix not in (".tif", ".tiff"):
                        raise ValueError("目前支持TIFF影像，请选择.tif或.tiff文件。")
                    stem = re.sub(r"[^\w.-]", "_", Path(upload.filename).stem)[:70]
                    path = folder / (uuid.uuid4().hex[:10] + "_" + stem + suffix)
                    created.append(path)
                    upload.save(path)
                    with path.open("rb") as stream:
                        if stream.read(4) not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                            raise ValueError("文件内容不是TIFF")
                    import rasterio
                    with rasterio.open(path) as src:
                        if src.driver != "GTiff" or src.count < 1:
                            raise ValueError("文件内容不是可读取的TIFF影像。")
                    names.append(path.relative_to(root).as_posix())
            except Exception:
                for path in created:
                    if path.exists():
                        path.resolve().relative_to(folder.resolve())
                        path.unlink()
                if folder.exists():
                    folder.rmdir()
                return jsonify({"success": False, "message": "上传失败：请检查文件确为可读取的TIFF影像；原有数据未改动。"}), 400
            return jsonify({"success": True, "files": names, "message": "数据已添加，请选择想了解的内容。"})

    @app.get("/api/result_file")
    def result_file():
        from tools.result_delivery import result_path
        if ctrl.session is None or ctrl.session.result_audit.get("status") not in ("pass", "needs_review"):
            return jsonify({"success": False, "message": "当前没有可下载的核验结果"}), 404
        try:
            path = result_path(ctrl.session, app.config["PROJECT_ROOT"], request.args.get("path", ""))
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".csv", ".md", ".txt", ".json"):
                raise ValueError("文件类型不支持")
            is_image = path.suffix.lower() in (".png", ".jpg", ".jpeg")
            if is_image:
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
            return send_file(path, as_attachment=not is_image or request.args.get("download") == "1")
        except (ValueError, OSError):
            return jsonify({"success": False, "message": "结果文件不可用或不属于本次任务"}), 404
