"""由任务与核验事实生成普通结果卡片，不把模型自评作为可信度。"""
from pathlib import Path
import re
from urllib.parse import quote


def result_path(session, project_root, name):
    """只允许当前任务核验清单内的实际文件，不能借链接读取配置或旧任务。"""
    if name not in session.result_audit.get("files", []):
        raise ValueError("文件不在本次核验清单中")
    results_root = (Path(project_root) / "results").resolve()
    results_root.relative_to(Path(project_root).resolve())
    root = (Path(project_root) / session.result_relative_dir).resolve()
    root.relative_to(results_root)
    candidate = Path(project_root) / "results" / name
    if candidate.is_symlink():
        raise ValueError("不提供链接文件")
    path = candidate.resolve()
    path.relative_to(root)
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("文件不存在或为空")
    return path


def build_result_delivery(session, project_root, report=""):
    """专业报告保留原文；默认展示的结论、状态和链接只取自确定性检查。"""
    audit = session.result_audit
    contract = session.task_contract
    capability = session.task_capability
    failed = audit.get("status") == "failed" or session.is_terminated()
    blocked = capability.get("status") == "blocked"
    kind = contract.get("task_type", "general")
    files = []
    if not failed and not blocked:
        for name in audit.get("files", []):
            try:
                path = result_path(session, project_root, name)
                suffix = path.suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".csv", ".txt", ".md", ".json"):
                    continue
                files.append({"name": path.name, "url": "/api/result_file?path=" + quote(name, safe=""),
                              "kind": "image" if suffix in (".jpg", ".jpeg", ".png") else "data"})
            except (OSError, ValueError):
                continue
    previews = [f for f in files if f["kind"] == "image"]
    # 优先专题成图和前后对照，中间图仍可在全部文件中下载。
    previews.sort(key=lambda f: (not f["name"].startswith("final"), "compare" not in f["name"], f["name"]))
    limits = []
    for reason in capability.get("reasons", []):
        if "专业识别模型" in reason:
            reason = "图中标记是初步识别结果，目标的位置和边界还需要核实。"
        elif "温度计算前" in reason:
            reason = "需要确认影像带有正确的测温信息，并完成相应校正，才能判断温度数值是否可信。"
        elif "云遮挡区域" in reason:
            reason = "云挡住的内容来自算法推算，可能与实际情况不同。"
        elif "尚未锁定输入" in reason:
            reason = "请确认本次使用的是哪些影像，避免分析到其他数据。"
        limits.append(reason)
    requests = "\n".join([contract.get("original_request", ""), *contract.get("revisions", [])])
    picture_events = [(m.end(), False) for m in re.finditer(r"(?:不要|无需|取消|不输出|不生成).{0,6}(?:图片|图像|图表|JPG|PNG)|(?:只要|仅需|仅要)\s*(?:CSV|统计表)", requests, re.I)]
    picture_events += [(m.end(), True) for m in re.finditer(r"(?:需要|增加|保留|输出|生成).{0,5}(?:结果图|变化图|图片|图像|JPG|PNG)", requests, re.I)
                       if not re.search(r"不.{0,6}$", requests[max(0, m.start()-6):m.start()])]
    preview_required = sorted(picture_events)[-1][1] if picture_events else True
    if not previews and preview_required:
        limits.append("本次没有可展示的结果图，不能仅凭文字说明认定已完成制图。")
    if audit.get("warnings"):
        limits.append("部分结果或统计仍需核对，详细原因可在专业详情中查看。")
    descriptions = {
        "change": "已标出可能发生变化的位置，" + ("是否属于" + "、".join(contract.get("targets", [])) + "变化还需要核实。" if contract.get("targets") else "这些变化的实际含义还需要核实。"),
        "segmentation": "已标出初步识别的区域，哪些地方识别正确还需要核实。",
        "cloud": "已生成影像处理结果，补全区域的真实性仍需核实。",
        "lst": "已生成温度分析相关结果，数值的单位与物理含义仍需核实。",
        "index": "已生成分析结果图，可用于查看不同区域的差别；实际含义还需要核对。",
        "general": "已生成分析结果，请结合图示查看；结果含义仍需核对。"}
    completion = "candidate" if kind in ("change", "segmentation", "cloud", "lst") else "generated"
    headline = descriptions[kind]
    if failed or blocked:
        completion = "blocked" if blocked else "failed"
        headline = "当前数据条件不足，尚未完成分析。" if blocked else "本次分析未完成，没有可确认的最终结果。"
    elif not files:
        completion, headline = "failed", "没有找到本次可交付的结果文件，不能确认任务完成。"
    elif not previews and preview_required:
        headline = "已生成部分文件，但尚未提供可查看的结果图。"
    findings = []
    rows = audit.get("statistics", [])
    if rows and not failed:
        first = (rows[0].get("source"), rows[0].get("scheme"))
        for row in rows:
            if (row.get("source"), row.get("scheme")) == first:
                findings.append(f"统计表记录：{row['class_name']}占本次统计范围的 {row['percent']:.2f}%。")
        findings = findings[:8]
    return {"version": 1, "completion": completion, "headline": headline,
            "goal": (contract.get("original_request") or session.user_request) +
                    ("；已提出的调整：" + "；".join(contract["revisions"]) if contract.get("revisions") else ""),
            "findings": findings, "limitations": list(dict.fromkeys(limits)),
            "artifact_check": "文件或统计检查未通过" if failed else "本次文件与统计已执行检查" if audit else "尚无文件检查证据",
            "verification": "还没有与已知正确的结果进行独立对照，准确性尚未验证。",
            "next_step": "请调整需求或补充缺少的数据后再试。" if failed or blocked else "先查看结果图；需要精确识别时，继续进行独立验证。",
            "files": files, "previews": previews[:4], "professional_report": report,
            "professional_audit": audit.get("facts", ""), "task_contract": contract,
            "capability": capability}


def render_plain_delivery(delivery):
    """CLI也使用同一份通俗结果，详细报告仍保留。"""
    return "\n".join([delivery["headline"], *delivery["findings"],
                      "已检查：" + delivery["artifact_check"], "验证情况：" + delivery["verification"],
                      *["需要留意：" + x for x in delivery["limitations"]], delivery["next_step"]])
