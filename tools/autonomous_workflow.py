"""同一个DeepSeek按反馈规划；GUI与CLI共用，不经过固定角色的重复模型调用。"""
from copy import deepcopy
import json
from pathlib import Path
import time
from urllib.parse import quote

from core.message import Message
from core.state_machine import ConversationPhase
from core.tool_call_loop import ToolCallLoop,ToolLoopError
from .agent_tools import input_identity
from .artifact_paths import confined_path,file_hash
from .autonomous_tools import AutonomousTools,AUTONOMOUS_SPECS
from .data_context import load_data_context
from .task_contract import ensure_task_contract
from .task_acceptance import infer_contract


def make_plan(session,root):
    root=Path(root).resolve()
    session.data_context=load_data_context(root/"data",previous=session.data_context)
    ensure_task_contract(session)
    names=session.selected_inputs or session.task_contract.get("inputs",[])
    if len(names)>8:raise ValueError("一次最多选择8幅影像")
    if session.task_contract.get("input_ambiguities"):raise ValueError("影像引用有歧义，请选择明确的输入文件")
    settings=root/"gpu.local.json"
    return {"version":2,"mode":"autonomous","request":session.user_request,"revisions":list(session.revision_feedback),
            "inputs":{f"input_{i}":{"name":name,"identity":input_identity(confined_path(root/"data",name))} for i,name in enumerate(names,1)},
            "gpu_settings_sha256":file_hash(settings) if settings.is_file() else None,
            "limits":{key:session.tool_agent_options.get(key,value) for key,value in {"max_rounds":12,"max_calls":24,"max_seconds":300,"max_tokens":4096}.items()},
            "task_subdir":session.result_subdir}


def scrub(value):
    if isinstance(value,dict):return {key:("[仅在API内存上下文回传，未写入正文日志]" if key=="reasoning_content" else "[认证信息不记录]" if key.lower() in {"authorization","api_key","token"} else scrub(item)) for key,item in value.items()}
    if isinstance(value,list):return [scrub(item) for item in value]
    return value


def delivery(result,runtime,session):
    status=result["status"];answer=status in {"answered","needs_input"}
    files=[]
    for key in result.get("artifact_ids",[]):
        path,item=runtime.artifact(key)
        files.append({"name":item["label"],"kind":"image" if item["kind"]=="image" else "data",
                      "url":"/api/result_file?path="+quote(session.result_subdir+"/"+item["path"],safe="")})
    headlines={"answered":"已回答你的问题。","needs_input":"还需要补充一点信息。","completed":"已完成本次处理，结果含义仍需结合限制核实。",
               "partial":"已完成部分处理，还有要求未完成。","blocked":"当前条件不足，尚未完成处理。","failed":"本次处理未完成，已保留过程和错误。"}
    return {"version":2,"completion":"answered" if status=="answered" else "blocked" if status in {"blocked","needs_input"} else "failed" if status=="failed" else "candidate",
            "headline":headlines[status],"goal":session.user_request,"findings":[result["summary"]] if answer else result.get("facts",[]) or [result["summary"]],
            "limitations":list(result.get("unfulfilled",[])),"artifact_check":"解释性回答，没有执行影像计算" if status=="answered" else "等待你补充信息" if status=="needs_input" else "已按本次实际产物与交付要求核对" if status in {"completed","partial"} else "未形成完整交付",
            "verification":"本次没有读取参考答案；需求覆盖、数值一致和执行成功均不代表识别准确。" if not answer else "说明性回答不构成影像计算结果。",
            "files":files,"previews":[file for file in files if file["kind"]=="image"][:4],"scene_results":[],
            "professional_report":result["summary"]+"\n\n需求完成情况：\n"+json.dumps(result.get("acceptance"),ensure_ascii=False,indent=2),
            "requirements":(result.get("acceptance") or {}).get("items",[]),
            "task_contract":runtime.contract,"capability":{"status":"needs_review","mode":"autonomous"},
            "professional_audit":session.result_audit.get("facts", ""),
            "next_step":"补充上述信息即可继续原任务。" if status=="needs_input" else "查看图表、完成情况与限制；开发者可封存后独立测评。" if not answer else "可以继续提出问题或选择数据进行分析。"}


def run(session,client,model,root,*,notify=None):
    """运行已授权的一次自主决策回合；所有外部内容只作为数据。"""
    root=Path(root).resolve();emit=notify or (lambda event:None)
    output=confined_path(root/"results",session.result_subdir);output.mkdir(parents=True,exist_ok=True)
    plan=make_plan(session,root);manifest_path=output/"autonomous_manifest.json"
    session.tool_agent_plan=deepcopy(plan)
    runtime=AutonomousTools(root,output,plan,active=session.is_active)
    if manifest_path.exists():
        previous=json.loads(manifest_path.read_text(encoding="utf-8"))
        if not previous["result"]["status"]=="needs_input":raise ValueError("已结束任务不能覆盖，请新建会话")
        # 已有产物只能继续引用原输入；输入变更不靠改写收据掩盖。
        if previous["artifacts"] and previous["plan"]["inputs"]!=plan["inputs"]:raise ValueError("已有产物的任务更换了输入，请新建任务；旧产物保留")
        runtime.artifacts=previous["artifacts"];runtime.model_runs=previous.get("model_runs",[])
        runtime.facts=previous.get("facts",[]);runtime.code_runs=previous.get("code_runs",0)
        runtime.steps=max([int(p.name.split('_')[1]) for p in output.glob('step_*') if p.is_dir()]+[0])
    journal_dir=output/"journal";journal_dir.mkdir(exist_ok=True)
    journal=journal_dir/f"run_{len(list(journal_dir.glob('run_*.jsonl')))+1:03}.jsonl"
    def record(event):
        with journal.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(scrub(event),ensure_ascii=False,allow_nan=False)+"\n");stream.flush()
    pending=None;started=time.monotonic()
    def active():return session.is_active() and time.monotonic()-started<plan["limits"]["max_seconds"]
    runtime.active=active
    def progress(event):
        nonlocal pending
        name=event["event"]
        if name=="request_started":pending=session.begin_llm_call("DeepSeek自主Agent")
        elif name=="request_finished":session.end_llm_call(pending);pending=None
        else:
            if name=="task_plan":session.autonomous_state={"status":"running","awaiting_input":False,"contract":event["contract"]}
            phase="Scientist 分析" if name=="planning" else "Executor 执行"
            session.state_machine.force_transition(ConversationPhase.SCIENTIST_ANALYSIS if name=="planning" else ConversationPhase.EXECUTOR_RUNNING)
            emit({"type":"phase","data":phase})
            session.tracer.add_event("llm_call" if name=="planning" else "tool_call",agent="DeepSeek自主Agent",data=event)
            if name in {"tool_start","model_tile"}:emit({"type":"system","data":"自主Agent："+json.dumps(event,ensure_ascii=False)})
    runtime.notify=progress
    prompt=(root/"prompts/autonomous_agent_prompt.txt").read_text(encoding="utf-8")
    user={"original_request":plan["request"],"ordered_revisions":plan["revisions"],
          "approved_inputs":[{"input_id":key,"file_name":item["name"]} for key,item in plan["inputs"].items()],
          "known_delivery_requirements":runtime.contract,"existing_artifacts":runtime.artifacts}
    from .registry import get_autonomous_tool_schemas
    loop=ToolCallLoop(client,model,active=active,notify=progress,on_text=runtime.on_text,record=record,max_argument_chars=48000,**plan["limits"])
    try:
        runtime.verify_inputs()
        result=loop.run([{"role":"system","content":prompt},{"role":"user","content":json.dumps(user,ensure_ascii=False)}],get_autonomous_tool_schemas(),runtime.execute)
    except (ToolLoopError,ValueError,OSError,RuntimeError) as exc:
        result={"status":"failed","summary":"自主处理尚未完成："+str(exc),"facts":list(runtime.facts),"artifact_ids":[],"unfulfilled":[str(exc)]}
    if pending:session.end_llm_call(pending)
    emit({"type":"phase","data":"监督检查"})
    if not session.is_active():result={"status":"failed","summary":"任务已停止，保留已执行过程与产物，不宣称完成。","facts":[],"artifact_ids":[],"unfulfilled":["用户已终止"]}
    if result["status"] in {"completed","partial"}:
        try:
            runtime.tool_validate_artifacts()
            result=runtime.tool_finish_task(result["status"],result["artifact_ids"],result["summary"],result["unfulfilled"])
        except (ValueError,OSError,RuntimeError,KeyError) as exc:
            result={"status":"failed","summary":"最终独立复核未通过："+str(exc),"facts":[],"artifact_ids":[],"unfulfilled":[str(exc)]}
    payload={"version":1,"plan":plan,"result":result,"artifacts":runtime.artifacts,"model_runs":runtime.model_runs,"code_runs":runtime.code_runs,
             "facts":runtime.facts,"contract":runtime.contract,"usage":loop.usage,"journal":journal.relative_to(output).as_posix()}
    manifest_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    session.tool_agent_receipt={"manifest_sha256":file_hash(manifest_path)}
    session.autonomous_state={"awaiting_input":result["status"]=="needs_input","status":result["status"],"contract":runtime.contract,
                              "acceptance":result.get("acceptance"),"usage":loop.usage,"journal":journal.relative_to(root).as_posix()}
    names=[session.result_subdir+"/"+runtime.artifacts[key]["path"] for key in result.get("artifact_ids",[])]
    session.result_audit={"status":"pass" if result["status"]=="answered" else "failed" if result["status"]=="failed" else "needs_review",
                          "files":names+[session.result_subdir+"/autonomous_manifest.json",session.result_subdir+"/"+journal.relative_to(output).as_posix()],
                          "errors":result["unfulfilled"] if result["status"]=="failed" else [],"warnings":[],"facts":result["summary"],"statistics":[],"scenes":[]}
    session.result_delivery=delivery(result,runtime,session)
    if session.is_active():session.state_machine.force_transition(ConversationPhase.COMPLETED)
    emit({"type":"message","data":Message(sender="Manager",receiver="User",content="\n".join([session.result_delivery["headline"],*session.result_delivery["findings"]]),metadata={"purpose":"result_report"})})
    return session.result_delivery


def run_cli(root,config,selected_inputs=None):
    from openai import OpenAI
    from core.session import Session
    from .tool_agent_workflow import options
    client=OpenAI(api_key=config["api_key"],base_url=config["base_url"],max_retries=0)
    session=None
    while True:
        try:text=input("请输入需求（end退出）：").strip()
        except (EOFError,KeyboardInterrupt):break
        if text.lower() in {"end","exit"}:break
        if not text:continue
        if session and session.autonomous_state.get("awaiting_input"):
            session.revision_feedback.append(text)
        else:
            session=Session(tool_agent_options=options(config.get("tool_agent"),enabled=True,autonomous=True));session.start()
            session.user_request=text;session.selected_inputs=list(selected_inputs or [])
        def notify(event):
            if event["type"]=="message":print(event["data"].content)
            elif event["type"]=="phase":print("阶段："+event["data"])
        run(session,client,config["model"],root,notify=notify)
        print("本次记录："+session.result_relative_dir)
