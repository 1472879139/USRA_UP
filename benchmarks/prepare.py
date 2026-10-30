"""准备可搬到独立环境的干净运行包；不复制答案、评分模块或历史记忆。"""
import argparse
import json
import shutil
from pathlib import Path

from tools.artifact_paths import confined_path, file_hash


def prepare_runtime(project_root, inputs, destination):
    """输入为相对data/的影像路径；目标必须不存在，不改变原始数据。"""
    root, destination = Path(project_root).resolve(), Path(destination).resolve()
    if not inputs or destination.exists() or root == destination or root.is_relative_to(destination):
        raise ValueError("请选择输入和不存在的新目录，不能覆盖项目")
    if any(destination.is_relative_to(root/name) for name in ("agents", "core", "tools", "gui", "prompts", "data")):
        raise ValueError("运行包不能建在原源码或输入目录内")
    source_paths = []
    for name in inputs:
        path = confined_path(root/"data", name)
        if path.suffix.lower() not in (".tif", ".tiff") or not path.is_file():
            raise ValueError("输入必须为存在的TIFF影像")
        source_paths.append(path)
        metadata = path.parent/"metadata.json"
        if metadata.is_symlink():
            raise ValueError("元数据不能是链接")
        if metadata.is_file():
            source_paths.append(metadata)
    destination.mkdir(parents=True)
    # 明确列出运行依赖目录，禁止整仓库复制导致答案、旧结果或本机配置混入。
    for folder, suffixes in [("agents", {".py"}), ("core", {".py"}), ("tools", {".py"}),
                             ("gui", {".py", ".html"}), ("prompts", {".txt"})]:
        for source in (root/folder).rglob("*"):
            if source.is_file() and source.suffix in suffixes and "__pycache__" not in source.parts:
                if source.relative_to(root).as_posix() in {"tools/water_evaluation.py", "gui/benchmarks.py", "gui/templates/benchmarks.html"}:
                    continue
                if source.is_symlink():
                    raise ValueError("运行源码不允许链接")
                target = destination/source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for name in ("main.py", "environment.yml", "requirements.txt", "config.example.json"):
        shutil.copy2(root/name, destination/name)
    for source in set(source_paths):
        target = destination/source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.name == "metadata.json":
            original = json.loads(source.read_text(encoding="utf-8-sig"))
            allowed = {k: original[k] for k in ("dataset", "bands", "encoding", "sensor", "units", "acquisition_date",
                       "platform", "scene", "date", "bands_in_order", "band_units", "thermal_constants_from_scene_MTL", "grid") if k in original}
            target.write_text(json.dumps(allowed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            shutil.copy2(source, target)
        if source.name != "metadata.json" and file_hash(target) != file_hash(source):
            raise ValueError("运行包输入复制校验失败")
    return destination


def main():
    parser = argparse.ArgumentParser(description="准备不含答案和历史记录的运行包，不调用模型")
    parser.add_argument("--input", action="append", required=True, help="相对data/的影像路径，可多次指定")
    parser.add_argument("--destination", required=True, help="不存在的新目录")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    result = prepare_runtime(args.project_root, args.input, args.destination)
    print("干净运行包已准备：", result)
    print("本包不含密钥，请单独安全配置；严格测试需在无法访问原项目与答案的独立环境运行。")


if __name__ == "__main__":
    main()
