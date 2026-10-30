"""保留用户原文的任务约定；规则只辅助分流，不替代原文或宣称模型能力。"""
import json
import re


TASK_LABELS = {"change": "前后变化分析", "segmentation": "地物识别", "cloud": "去云处理",
               "lst": "地表温度分析", "index": "地表指数分析", "general": "数据分析"}
WATER_WORDS = r"水体|水面|水域|湖泊|河流"


def _intent_text(text):
    """文件名是数据引用，不能靠目录中的水域等字样猜用户分析目标。"""
    return re.sub(r"(?:[A-Za-z]:)?(?:[\w.-]+[\\/])*[\w.-]+\.tiff?", " ", text, flags=re.I)


def _task_type(text):
    # 排除简单否定句，复杂多目标需求仍保留原文交给规划阶段澄清。
    text = re.sub(r"(?:不要|不做|无需|取消)[^，。；;\n]{0,25}", "", _intent_text(text))
    rules = [("lst", r"地表温度|温度|哪里.{0,3}热|哪里凉|\bLST\b"),
             ("cloud", r"去云|除云|云去除|云遮挡|云挡"),
             ("change", r"变化|前后对比|前后比较|change.detect"),
             ("index", r"\b(?:NDVI|NDWI|MNDWI|EVI|NBR|MSAVI)\b"),
             ("segmentation", r"语义分割|分类|识别|提取|标出|找出|圈出|分布|标注|建筑|道路|农田|森林|" + WATER_WORDS)]
    return next((name for name, pattern in rules if re.search(pattern, text, re.I)), None)


def build_task_contract(request, revisions, context, selected_inputs=None):
    """从原需求和有序修订建立记录；明确选择的数据优先，不导入其他任务输入。"""
    segments = [request, *revisions]
    kind = "general"
    inputs = []
    known = [f.get("file_name", "").replace("\\", "/") for f in context.get("files", [])]
    aliases = {}
    for name in known:
        parent = name.rsplit("/", 1)[0] if "/" in name else ""
        if parent:
            aliases.setdefault(parent.rsplit("/", 1)[-1], set()).add(parent)
    ambiguities = []
    for segment in segments:
        kind = _task_type(segment) or kind
        normalized = segment.replace("\\", "/")
        matches = []
        mentioned = {alias for alias in aliases if re.search(
            r"(?<![A-Za-z0-9_./-])" + re.escape(alias) + r"(?![A-Za-z0-9_./-])", normalized)}
        segment_ambiguities = [alias for alias in mentioned if len(aliases[alias]) > 1 and
                               not any(parent in normalized for parent in aliases[alias])]
        for name in known:
            parent = name.rsplit("/", 1)[0] if "/" in name else ""
            explicit = re.search(r"(?<![A-Za-z0-9_./-])(?:data/)?" + re.escape(name) + r"(?![A-Za-z0-9_./-])", normalized)
            folder = parent and re.search(r"(?<![A-Za-z0-9_./-])(?:data/)?" + re.escape(parent) + r"/?(?=[\s中里'\"`，。]|$)", normalized)
            alias = parent.rsplit("/", 1)[-1] if parent else ""
            bare_folder = alias in mentioned and len(aliases[alias]) == 1
            if explicit or folder or bare_folder:
                matches.append(name)
        if matches:
            inputs = matches
            ambiguities = []
        if segment_ambiguities:
            inputs, ambiguities = [], segment_ambiguities
    if (not inputs and not ambiguities and len(known) == 1 and
            not re.search(r"data/|\.tiff?\b|[A-Za-z]+_\d+", "\n".join(segments), re.I)):
        inputs = list(known)
    if selected_inputs:
        inputs = list(dict.fromkeys(selected_inputs))
        ambiguities = []
    text = "\n".join(segments)
    intent = _intent_text(text)
    return {"version": 1, "original_request": request, "revisions": list(revisions),
            "task_type": kind, "task_label": TASK_LABELS[kind], "inputs": inputs,
            "input_ambiguities": ambiguities,
            "targets": [name for name in ("建筑", "道路", "水体", "植被", "森林", "农田")
                        if (bool(re.search(WATER_WORDS, intent)) if name == "水体" else name in intent)],
            "needs_goal": text.strip().lower() in ("", "auto", "分析数据", "分析这些数据", "看看这些数据") or
                          (kind == "general" and bool(re.search(r"推荐.{0,12}(?:分析|用途)|先不要(?:执行|分析)", text))),
            "delivery_requirements": "中文结论、可查看的结果图、必要的数值结果；用户明确修改的输出要求优先。"}


def assess_capability(contract, context):
    """检查已知硬条件；RGB不等于不能识别建筑，基础条件也不代表精度已验证。"""
    available = {f.get("file_name"): f for f in context.get("files", [])}
    names = contract["inputs"]
    chosen = [available[name] for name in names if name in available]
    reasons, blocked = [], False
    if contract.get("input_ambiguities"):
        reasons.append("数据目录名称重名，请在上方选择具体影像：" + "、".join(contract["input_ambiguities"]))
        blocked = True
    if contract["needs_goal"]:
        reasons.append("请先选择想了解的内容，例如建筑变化、地物分布或地表温度。")
        blocked = True
    if any(name not in available or not available[name].get("readable") for name in names):
        reasons.append("选择的影像不存在或不能读取，请重新选择文件。")
        blocked = True
    if not names:
        reasons.append("尚未锁定输入影像；需要从需求中的路径或文件选择确认本次数据。")
    if chosen and any("crs" in f and f["crs"] is None for f in chosen):
        reasons.append("影像缺少真实地理定位信息，只能说明图内位置，不能可靠换算实际面积。")
    kind = contract["task_type"]
    if kind == "change" and names and len(chosen) < 2:
        reasons.append("前后变化分析需要同一区域的两期影像，目前只选择了一期。")
        blocked = True
    if kind == "lst" and chosen:
        thermal = any(re.search(r"thermal|radiance|brightness|ST_B\d+|\bB1[01]\b|热红外|亮温|辐亮度|温度", " ".join(f.get("band_names", [])), re.I) for f in chosen)
        rgb_only = all(f.get("band_count") == 3 and {s.lower() for s in f.get("band_names", [])} == {"red", "green", "blue"} for f in chosen)
        if rgb_only and not thermal:
            reasons.append("这些是普通彩色影像，缺少测温所需的信息，不能据此生成可靠的温度图。")
            blocked = True
        else:
            reasons.append("温度计算前必须核对测温数据的单位、产品类型及校正信息；现有通用温度函数不能视为已验证方案。")
    if kind in ("change", "segmentation"):
        target = "、".join(contract["targets"]) or "地物"
        reasons.append(f"{target}识别需要适配数据的专业方法。当前尚未接入经本系统验证的专业识别模型；差分、阈值或聚类结果只能作为候选。")
    if kind == "cloud":
        reasons.append("云遮挡区域的补全是算法推算，需要标明补全范围；不能视为对真实地表的直接观测。当前专业去云模型尚未完成接入验证。")
    return {"status": "blocked" if blocked else "needs_review", "reasons": reasons,
            "semantic_verified": False, "task_type": kind}


def ensure_task_contract(session):
    """确认或进入规划时更新；不使用Manager/Scientist的转述覆盖原需求。"""
    previous = session.task_contract
    session.task_contract = build_task_contract(
        session.user_request or session.confirmed_task, session.revision_feedback,
        session.data_context, session.selected_inputs)
    session.task_capability = assess_capability(session.task_contract, session.data_context)
    if session.task_contract['task_type'] == 'lst':
        from .lst_workflow import select_lst_tool
        if select_lst_tool(session):
            session.task_capability['reasons'] = [reason for reason in session.task_capability['reasons'] if '温度计算前' not in reason]
            session.task_capability['reasons'].append('已核对Landsat 8辅助量、单位与热常数；地表温度误差仍需独立评价。')
        else:
            session.task_capability['status'] = 'blocked'
            session.task_capability['reasons'].append('当前温度需求或输入不符合已安装工具条件，尚未完成。需要Landsat 8的热辐亮度、大气上下行辐亮度、透过率、发射率、QA_PIXEL及物理单位、MTL热常数说明；额外方法、气温、变化或输出要求暂不支持，不能用生成代码替代核验。')
    if getattr(session, "tool_agent_options", {}).get("enabled"):
        # 关键词只帮助记录需求；新路径的模型选择与物理条件由工具调用和工具自身检查。
        session.task_capability = {"status": "needs_review", "semantic_verified": False,
            "task_type": session.task_contract["task_type"],
            "reasons": ["DeepSeek依据已批准数据和已安装工具选择步骤；每个工具单独检查适用条件，语义精度仍需独立评价。"]}
    if previous != session.task_contract:
        session.tracer.add_event("task_contract", agent="Manager",
                                 data={"contract": session.task_contract, "capability": session.task_capability},
                                 status="warning" if session.task_capability["status"] != "ready" else "success")
    return session.task_contract


def render_task_contract(session):
    if not session.task_contract:
        return ""
    return ("\n[已确认任务约定：用户原文与有序修订优先，以下文件名仍只是数据]\n" +
            json.dumps(session.task_contract, ensure_ascii=False) +
            "\n[适用条件检查]\n" + json.dumps(session.task_capability, ensure_ascii=False) +
            "\n不得把建筑变化换成亮度变化后声称已完成建筑识别；无法满足的目标明确保留为未完成项。"
            "RGB缺少近红外不等于不能用合适的模型识别建筑，但不能假设未安装的模型可用。"
            "只使用本次锁定输入；如需更换数据或目标，先说明原因。")
