"""离线多方法比较：干净处理端先全部交卷，评分端之后才读取答案。"""
import argparse
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from .catalog import load_catalog
from .prepare import prepare_runtime
from .assessment import grade_submission

# 此工作脚本只放进新运行包，不携带答案清单或评分逻辑。
WORKER = r'''
import functools,hashlib,json,sys,time
from pathlib import Path
from queue import Queue
from unittest.mock import Mock,patch
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from agents.manager_agent import ManagerAgent
from agents.scientist_agent import ScientistAgent
from agents.engineer_agent import EngineerAgent
from agents.executor_agent import ExecutorAgent
from agents.supervisor_agent import SupervisorAgent
from core.session import Session
from core.message import Message,MessageType
from core.state_machine import ConversationPhase
from gui.app import URSAGUI
from tools.submission import freeze_submission
from tools import water_workflow
import rasterio
assert not (ROOT/'benchmarks').exists()
assert not (ROOT/'benchmark.local.json').exists()
tasks=json.loads((ROOT/'comparison_tasks.json').read_text(encoding='utf-8'))
records=[]
original_selector=water_workflow.select_water_tool
for task in tasks:
 started=time.perf_counter();session=Session();session.start()
 view=object.__new__(URSAGUI);view.session=session;view.ui_queue=Queue();view.config={'executor_max_retries':5}
 agents=[]
 for name,kind in [('manager',ManagerAgent),('scientist',ScientistAgent),('engineer',EngineerAgent),('executor',ExecutorAgent),('supervisor',SupervisorAgent)]:
  agent=kind(model='offline',api_key='offline-placeholder',base_url='https://example.invalid',system_prompt=(ROOT/'prompts'/f'{name}_prompt.txt').read_text(encoding='utf-8'));agent.session=session;agents.append(agent)
 manager,scientist,engineer,executor,supervisor=agents
 manager._call_llm=Mock(return_value='按指定输入与锁定候选方法执行，结果准确性尚未独立验证。')
 scientist._call_llm=Mock(side_effect=AssertionError('本离线比较仅支持已安装工具，不调用收费规划模型'))
 engineer._call_llm_with_memory=Mock(side_effect=AssertionError('不重写已安装方法'))
 supervisor.select_relevant_files=Mock(side_effect=AssertionError('本次输入应已锁定'))
 with patch('tools.water_workflow.select_water_tool',new=functools.partial(original_selector,policy=task['policy'])):
  try:
   manager.handle_message(Message(sender='User',receiver='Manager',content=task['request']))
   approval=manager.handle_message(Message(sender='User',receiver='Manager',content='approve',msg_type=MessageType.CONTROL))
   if approval.receiver!='Scientist':raise ValueError('需求未确认')
   view._run_analysis_pipeline(*agents,session.state_machine)
   if session.state_machine.get_current_phase() not in (ConversationPhase.COMPLETED,ConversationPhase.TERMINATED):session.terminate('比较任务未到达终态')
  except Exception as exc:
   session.terminate('离线任务失败：'+str(exc))
  assert not any(key=='benchmarks' or key.startswith('benchmarks.') for key in sys.modules)
  assert all('scores' not in scene['evaluation'] for scene in session.result_audit.get('scenes',[]))
  record={**task,'analysis_seconds':round(time.perf_counter()-started,3),'manager_mock_calls':manager._call_llm.call_count,
          'phase':session.state_machine.get_current_phase().value,'stop_reason':session.get_terminate_reason(),
          'methods':[{'method':s.get('tool_version'),'threshold':s.get('threshold'),'selection':s.get('selection')} for s in session.result_audit.get('scenes',[])]}
  record['prediction_pixels_sha256']=[]
  for scene in session.result_audit.get('scenes',[]):
   with rasterio.open(ROOT/'results'/scene['prediction_path']) as dataset:
    record['prediction_pixels_sha256'].append(hashlib.sha256(dataset.read(1).tobytes()).hexdigest())
  try:record['receipt']=freeze_submission(session,ROOT)
  except Exception as exc:record['seal_error']=str(exc)
  records.append(record)
  (ROOT/'comparison_receipts.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
  print(task['case'],task['policy'],task['phrase_index'],record['phase'],flush=True)
'''


def run_comparison(project_root, case_ids, output, phrases):
    """真实工具和GUI编排，Manager模拟；不允许覆盖既有比较目录。"""
    root, output = Path(project_root).resolve(), Path(output).resolve()
    catalog = {c["id"]: c for c in load_catalog() if c.get("enabled")}
    if not case_ids or any(cid not in catalog for cid in case_ids):
        raise ValueError("请指定已准备好的水体案例")
    if output.exists() or output==root or root.is_relative_to(output):
        raise ValueError("比较输出必须是新的独立目录")
    output.mkdir(parents=True)
    runtime = output/"runtime"
    prepare_runtime(root, [catalog[cid]["inputs"][0]["path"].removeprefix("data/") for cid in case_ids], runtime)
    tasks = []
    for cid in case_ids:
        for policy in ("mndwi_zero", "mndwi_otsu", "auto"):
            templates = phrases if policy=="auto" else phrases[:1]
            tasks.extend({"case": cid, "policy": policy, "phrase_index": i,
                          "request": template.format(case=cid)} for i,template in enumerate(templates,1))
    (runtime/"comparison_tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (runtime/"offline_comparison.py").write_text(WORKER, encoding="utf-8")
    env = dict(os.environ); env.pop("PYTHONPATH", None); env["PYTHONNOUSERSITE"] = "1"
    # 工作端日志直接显示进度，不把含答案的配置或清单放进子进程。
    with (output/"runtime.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable,"-X","utf8",str(runtime/"offline_comparison.py")], cwd=runtime,
                                   env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            code = process.wait(timeout=max(180, len(tasks)*30))
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
            raise ValueError("离线比较超时；已保留产生的记录，不能宣称全部完成")
    records_path = runtime/"comparison_receipts.json"
    if code or not records_path.is_file():
        raise ValueError("处理端异常，详情见本次runtime.log；未将部分成功当作全组完成")
    records = json.loads(records_path.read_text(encoding="utf-8"))
    if len(records)!=len(tasks):
        raise ValueError("任务记录不完整，拒绝只统计成功子集")
    # 全部任务结束并封存以后才进入评分阶段，任何分数都不返回工作端。
    graded = []
    for record in records:
        if "receipt" in record:
            r = record["receipt"]
            report = grade_submission(runtime/r["directory"], r["sha256"], root)
        else:
            report = {"status":"not_completed", "scenes":[], "stop_reason":record.get("seal_error", "未交卷")}
        graded.append({**record, "evaluation":report})
    report = {"mode":"offline_role_replay", "real_model_calls":0, "expected_trials":len(tasks),
              "recorded_trials":len(graded), "records":graded,
              "scope":"两种方法与输入选路的离线比较，Manager模拟；不是DeepSeek真实语言理解成绩。",
              "isolation":"application_separation_only"}
    (output/"comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    write_comparison_html(report, output)
    return report


def write_comparison_html(report, output):
    """只呈现已完成的外部评分，不改答卷；不可评分记录原样列出。"""
    def percent(value):
        return "无法计算" if value is None else f"{value:.2%}"
    rows = []
    for record in report["records"]:
        if record["phrase_index"]!=1:
            continue
        evaluation = record["evaluation"]
        scenes = evaluation.get("scenes", [])
        score = scenes[0].get("scores", {}) if len(scenes)==1 else {}
        method = record.get("methods", [{}])[0] if record.get("methods") else {}
        label = {"mndwi_zero":"原零阈值方法", "mndwi_otsu":"输入分布阈值方法", "auto":"候选输入选路"}[record["policy"]]
        rows.append("<tr>"+"".join("<td>"+html.escape(str(value))+"</td>" for value in (
            record["case"],label,evaluation["status"],percent(score.get("precision")),percent(score.get("recall")),
            percent(score.get("f1")),record["analysis_seconds"]))+"</tr>")
    body = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>水体方法离线比较</title><style>
body{margin:0;background:#f3f7fa;color:#183247;font:16px/1.65 "Microsoft YaHei",sans-serif}main{max-width:1150px;margin:32px auto;padding:0 20px 40px}h1{font-size:30px}.note{padding:16px;background:#fff3d6;border-left:4px solid #b27b0e}.table{overflow-x:auto;background:#fff;margin:24px 0}table{width:100%;border-collapse:collapse;white-space:nowrap}td,th{padding:12px;border-bottom:1px solid #d7e3eb;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;padding:16px}a{color:#0665b6}</style><main><h1>水体候选方法：逐场景外部比较</h1>
<p class="note">两种方法与候选选路先在干净运行包中完成并封存，之后才读取答案。Manager响应模拟，真实模型请求0；不是DeepSeek语言理解成绩。此表的候选选路可能尚未获准作为默认方法，发布状态以版本记录为准。</p>'''
    body += f"<p>计划{report['expected_trials']}次，记录{report['recorded_trials']}次；失败与不可评分均保留。表格每种策略列首种说法，其余话术在完整JSON中。</p>"
    body += '<div class="table"><table><thead><tr><th>案例</th><th>方法/策略</th><th>评价状态</th><th>标出的有多少正确</th><th>真实水体找回多少</th><th>F1</th><th>处理秒数</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
    body += '<p>输入分布可分开不等于地物识别准确。历史或本批查看过的样本之后只能作为回归；没有统一80分合格线。</p><a href="comparison.json">下载完整比较JSON</a><details><summary>展开方法选择、阈值与执行记录</summary><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></details></main></html>\n'
    with (Path(output)/"comparison.html").open("x", encoding="utf-8") as stream:
        stream.write(body)


def promotion_result(report, protocol):
    """预先冻结的升级门槛；失败/缺分不删除，不以最高分逐图挑选方法。"""
    scores = {}
    for row in report["records"]:
        scenes = row["evaluation"].get("scenes", [])
        scores[(row["case"],row["policy"],row["phrase_index"])] = scenes[0].get("scores") if len(scenes)==1 else None
    old = protocol["development_cases"]
    new = [c["id"] for c in protocol["samples"]]
    pairs = [(cid, scores.get((cid,"mndwi_zero",1)), scores.get((cid,"auto",1))) for cid in old+new]
    complete = all(a and b and a.get("f1") is not None and b.get("f1") is not None for _,a,b in pairs)
    complete = bool(complete and report["recorded_trials"]==report["expected_trials"] and
                    all(row["evaluation"]["status"]=="scored" for row in report["records"]))
    drops = {cid:b["f1"]-a["f1"] for cid,a,b in pairs if a and b and a.get("f1") is not None and b.get("f1") is not None}
    delta = sum(drops[cid] for cid in new)/len(new) if complete else None
    gate = protocol["promotion_gate"]
    passed = bool(complete and delta>=gate["new_macro_f1_min_improvement"] and
                  all(value>=-gate["max_individual_f1_drop_all_cases"] for value in drops.values()))
    return {"passed":passed, "all_cases_comparable":complete, "new_macro_f1_delta":delta,
            "individual_f1_deltas":drops, "gate":gate,
            "decision":"可按预先冻结规则升级默认选路" if passed else "未达到冻结升级门槛，保留原默认方法；新方法仅作实验比较"}


def main():
    parser = argparse.ArgumentParser(description="先交卷后评分的离线多方法比较，不调用收费模型")
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    report = run_comparison(args.project_root,args.case,args.output,["帮我圈出{case}中的水面，看看占多大面积。"])
    print("比较完成",report["recorded_trials"],"个任务；真实模型请求0")


if __name__ == "__main__":
    main()
