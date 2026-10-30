"""可迁移的验收清单与只读完整性检查。"""

import json
from pathlib import Path
from tools.artifact_paths import file_hash, confined_path


def load_catalog():
    return json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))["cases"]


def reference_root(project_root, key="reference_root"):
    config = Path(project_root) / "benchmark.local.json"
    if not config.is_file():
        return None
    settings = json.loads(config.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise ValueError("benchmark.local.json 必须是JSON对象")
    value = settings.get(key)
    if value is None and key != "reference_root":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("benchmark.local.json 中的 reference_root 不能为空")
    return (Path(project_root) / value).resolve()


def audit_case(project_root, case, answers, comparisons=None):
    checks = []

    def check(item, root, role):
        if not item:
            return True
        try:
            if root is None:
                raise ValueError("尚未配置参考答案目录 benchmark.local.json")
            path = confined_path(root, item["path"])
            if not path.is_file():
                raise ValueError("文件缺失")
            if file_hash(path) != item["sha256"]:
                raise ValueError("SHA256与冻结清单不一致")
            checks.append({"role": role, "path": item["path"], "ok": True, "message": "文件完整"})
            return True
        except (OSError, ValueError) as exc:
            checks.append({"role": role, "path": item["path"], "ok": False, "message": str(exc)})
            return False

    inputs_ok = all([check(item, project_root, "input") for item in case["inputs"]])
    metadata_ok = check(case.get("metadata"), project_root, "metadata")
    ref_ok = check(case["reference"], answers, "reference")
    extras_ok = all([check(item, comparisons if item.get("pack_relative") else answers,
                           "auxiliary") for item in case.get("extras", [])])
    return {"inputs_ok": inputs_ok and metadata_ok, "reference_ok": ref_ok,
            "extras_ok": extras_ok, "checks": checks}


def audit_catalog(project_root):
    error = ""
    try:
        answers = reference_root(project_root)
        comparisons = reference_root(project_root, "comparison_root")
    except (OSError, ValueError, KeyError) as exc:
        answers, comparisons, error = None, None, "参考答案配置无效：" + str(exc)
    cases = []
    for case in load_catalog():
        status = audit_case(project_root, case, answers, comparisons)
        cases.append({**case, **status, "runnable": bool(case.get("enabled") and
                      status["inputs_ok"] and status["reference_ok"] and status["extras_ok"])})
    return {"cases": cases, "configuration_error": error,
            "reference_configured": answers is not None}
