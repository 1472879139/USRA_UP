"""由任务与核验事实生成普通结果卡片，不把模型自评作为可信度。"""
from pathlib import Path
import re
from urllib.parse import quote


def has_unsupported_accuracy_claim(text):
    """拦截常见无答案精度数字；不是覆盖所有表述的语义判别器。"""
    metric = r"(?:准确率|精确率|召回率|找回率|漏检率|误报率|F1(?:分数)?|IoU|RMSE|MAE|均方根误差|平均绝对误差|测温误差)"
    return bool(re.search(metric + r"[^\n。；]{0,14}\d+(?:\.\d+)?\s*%?", text, re.I) or
                re.search(r"\d+(?:\.\d+)?\s*%[^\n。；]{0,10}" + metric, text, re.I) or
                re.search(r"(?:已|已经)(?:与|通过)[^\n。；]{0,12}(?:人工答案|参考答案)[^\n。；]{0,8}(?:核对|验证|对照)", text))


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
    scenes = [] if failed or blocked or not files else [dict(scene) for scene in audit.get("scenes", [])]
    rows = audit.get("statistics", [])
    if scenes:
        for scene in scenes:
            findings.append(scene["label"] + "：" + "；".join(scene["findings"]))
        if kind == 'lst':
            from .lst_tool import LIMITATION
            headline = '已生成地表温度图、摄氏度统计和本图相对高温位置。'
            limits.append(LIMITATION)
            if any('incomplete_prediction' in scene.get('quality_flags', []) for scene in scenes):
                headline = '已生成可用区域的地表温度；云、阴影或无效位置留空。'
        else:
            headline = "已逐幅生成水体候选图、面积与占比，请结合每幅图的检查与限制查看。"
            limits = [reason for reason in limits if "专业识别模型" not in reason]
            limits.append("固定指数方法可能受云、阴影、积雪和复杂地表影响；公开回归成绩不能外推到其他地区。")
            if any("incomplete_prediction" in scene.get("quality_flags", []) for scene in scenes):
                headline = "已生成水体候选结果，部分区域尚未确认，请逐幅查看。"
    elif rows and not failed:
        groups = {(row.get("source"), row.get("scheme")) for row in rows}
        for row in rows:
            prefix = Path(row.get("source", "统计表")).name + "：" if len(groups) > 1 else "统计表记录："
            findings.append(f"{prefix}{row['class_name']}占本次统计范围的 {row['percent']:.2f}%。")
        findings = findings[:24]
    verification = "正常分析未读取人工答案，识别准确性尚未验证，需交卷后独立测评；文件与统计一致不能证明识别正确。"
    if kind == 'lst':
        verification = '正常分析未读取参考答案，温度误差尚未验证；单位、物理条件和数值一致性检查不能代替实测验证。'
    next_step = ("请调整需求或补充缺少的数据后再试。" if failed or blocked else
                 "先查看候选图和限制；开发者可封存答卷后进行外部测评。")
    for scene in scenes:
        path = scene.get("preview_path")
        scene["preview"] = next((file for file in previews if path and file["url"] ==
                                "/api/result_file?path=" + quote(path, safe="")), None)
    delivery = {"version": 1, "completion": completion, "headline": headline,
            "goal": (contract.get("original_request") or session.user_request) +
                    ("；已提出的调整：" + "；".join(contract["revisions"]) if contract.get("revisions") else ""),
            "findings": findings, "limitations": list(dict.fromkeys(limits)),
            "artifact_check": "文件或统计检查未通过" if failed else "本次文件与统计已执行检查" if audit else "尚无文件检查证据",
            "verification": verification, "scene_results": scenes,
            "next_step": next_step,
            "files": files, "previews": previews[:4], "professional_report": report,
            "professional_audit": audit.get("facts", ""), "task_contract": contract,
            "capability": capability}
    reason = session.get_terminate_reason()
    if (failed and session.remote_tool_plan and "ModuleNotFoundError" in reason
            and ("No module named 'tools'" in reason or 'No module named "tools"' in reason)):
        delivery.update({
            "completion": "failed",
            "headline": "本机分析工具未能启动，本次没有影像分析结果。",
            "findings": ["本机执行进程没有加载到项目工具，任务在请求GPU之前停止。"],
            "limitations": ["本次尚未处理影像，没有结果可用于判断识别效果。"],
            "artifact_check": "工具启动失败，尚未生成可核验的结果文件",
            "verification": "本次未完成分析，无法评价识别准确性。",
            "next_step": "请重启更新后的本机预览版，再使用同一影像发起新任务。",
            "professional_audit": audit.get("facts") or reason,
        })
    if audit.get("tool_agent"):
        from .tool_agent_workflow import enrich_delivery
        return enrich_delivery(delivery, audit, session.result_subdir)
    if audit.get("remote_water") or audit.get("remote_landcover"):
        from .remote_models import enrich_delivery
        return enrich_delivery(delivery, audit)
    return delivery


def render_plain_delivery(delivery):
    """CLI也使用同一份通俗结果，详细报告仍保留。"""
    return "\n".join([delivery["headline"], *delivery["findings"],
                      "已检查：" + delivery["artifact_check"], "验证情况：" + delivery["verification"],
                      *["需要留意：" + x for x in delivery["limitations"]], delivery["next_step"]])
