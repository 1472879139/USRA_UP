"""DeepSeek工具调用离线回归，不使用真实模型或业务影像。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.tool_call_loop import ToolCallLoop, ToolLoopError


def response(name=None,arguments=None,identity="call_1",text="",reasoning=None,finish=None):
    calls=[] if name is None else [SimpleNamespace(id=identity,type="function",function=SimpleNamespace(name=name,arguments=json.dumps(arguments or {})))]
    message=SimpleNamespace(content=text,tool_calls=calls,reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message,finish_reason=finish or ("tool_calls" if calls else "stop"))],usage=SimpleNamespace(prompt_tokens=10,completion_tokens=5))


class ToolLoopTests(unittest.TestCase):
    def client(self,*replies):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))

    def test_results_and_reasoning_are_returned_before_next_decision(self):
        client=self.client(response("inspect_data",reasoning="选择检查"),response("finish_task",identity="call_2"))
        execute=Mock(side_effect=[{"ok":True,"facts":["十二波段"]},{"ok":True,"_finish":True,"status":"completed"}])
        loop=ToolCallLoop(client,"offline",max_rounds=3,max_calls=3)
        result=loop.run([],[],execute)
        self.assertEqual(result["status"],"completed")
        sent=client.chat.completions.create.call_args_list[1].kwargs["messages"]
        self.assertTrue(any(x.get("role")=="tool" and "十二波段" in x["content"] for x in sent))
        self.assertEqual(sent[0]["reasoning_content"],"选择检查")
        self.assertEqual(execute.call_count,2)

    def test_plain_text_is_not_a_success_receipt(self):
        client=self.client(response(text="已经成功"),response(text="已经成功"))
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline",max_rounds=2).run([],[],Mock())

    def test_truncated_tool_arguments_never_execute(self):
        client=self.client(response("inspect_data",finish="length"));execute=Mock()
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline").run([],[],execute)
        execute.assert_not_called()

    def test_call_limit_stops_before_unbounded_replanning(self):
        client=self.client(response("inspect_data"),response("inspect_data",identity="call_2"))
        execute=Mock(return_value={"ok":False,"error":{"code":"unavailable"}})
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline",max_calls=1).run([],[],execute)
        self.assertEqual(execute.call_count,1)

    def test_provider_exception_is_not_retried_or_replaced(self):
        client=self.client(RuntimeError("offline provider failed"))
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline").run([],[],Mock())
        self.assertEqual(client.chat.completions.create.call_count,1)



from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import numpy as np
import rasterio
from unittest.mock import patch
from tools.agent_tools import AgentTools, TOOL_SPECS
from tools import tool_agent_workflow as workflow
from tools.task_contract import ensure_task_contract

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("tool_agent_gpu_fixture",PROJECT/"tests/test_terramind_agent.py")
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


@contextmanager
def task_fixture(network=False):
    with f.fixture(network=network) as (root,session,server):
        session.tool_agent_options=workflow.options(enabled=True)
        session.user_request="找出农田，给我候选分布图和平方米面积。"
        ensure_task_contract(session)
        session.tool_agent_plan=workflow.make_plan(session)
        output=root/"results"/session.result_subdir
        output.mkdir(parents=True)
        runtime=AgentTools(root,output,session.tool_agent_plan)
        yield root,session,server,runtime


def call(name,args=None,n=1):return response(name,args,identity="call_"+str(n))


class AgentToolTests(unittest.TestCase):
    def test_unknown_tool_and_extra_arguments_are_not_executed(self):
        with task_fixture() as (_,_,_,runtime):
            self.assertFalse(runtime.execute("run_python",{"code":"print(1)"})["ok"])
            self.assertFalse(runtime.execute("inspect_data",{"path":"config.json"})["ok"])
            self.assertFalse(runtime.execute("inspect_data",{"__class__":"x"})["ok"])

    def test_input_ids_cannot_escape_approved_data(self):
        with task_fixture() as (_,_,_,runtime):
            for key in ["../reference.tif","/etc/passwd","config.json"]:
                self.assertFalse(runtime.execute("compute_index",{"input_id":key,"index":"NDVI"})["ok"])

    def test_input_change_stops_before_model_or_tool(self):
        with task_fixture() as (root,_,_,runtime):
            path=root/"data/sample/metadata.json";path.write_text(path.read_text()+" ")
            with self.assertRaises(ToolLoopError):runtime.execute("inspect_data",{})

    def test_gpu_setting_change_requires_confirmation(self):
        with task_fixture() as (root,_,_,runtime):
            p=root/"gpu.local.json";p.write_text(p.read_text()+" ")
            with self.assertRaises(ToolLoopError):runtime.execute("list_capabilities",{})

    def test_indices_follow_actual_band_names_and_make_a_real_map(self):
        with task_fixture() as (_,_,_,runtime):
            result=runtime.execute("compute_index",{"input_id":"input_1","index":"NDVI"})
            self.assertTrue(result["ok"],result)
            path,_=runtime.artifact(result["raster_id"],True)
            with rasterio.open(path) as src:self.assertTrue(np.all(src.read(1,masked=True).compressed()==0))
            picture=runtime.execute("render_map",{"artifact_id":result["raster_id"]})
            self.assertTrue(picture["ok"],picture)
            self.assertTrue(runtime.execute("validate_artifacts",{})["ok"])

    def test_bad_units_are_not_reported_as_square_metres(self):
        with task_fixture() as (_,_,_,runtime):
            image=runtime.output/"geographic.tif"
            with rasterio.open(image,"w",driver="GTiff",height=2,width=2,count=1,dtype="uint8",crs="EPSG:4326",transform=rasterio.transform.from_origin(10,50,.01,.01),nodata=255) as dst:dst.write(np.ones((2,2),dtype="uint8"),1)
            key=runtime.register(image,"raster",classes={"0":"其他","1":"目标"})
            result=runtime.execute("calculate_area",{"artifact_id":key,"class_codes":[1]})
            self.assertFalse(result["ok"]);self.assertIn("米制",result["error"]["message"])

    def test_finish_requires_real_ids_validation_and_picture(self):
        with task_fixture() as (_,_,_,runtime):
            args={"status":"completed","artifact_ids":[],"summary":"已完成","unfulfilled":[]}
            self.assertFalse(runtime.execute("finish_task",args)["ok"])
            args["artifact_ids"]=["old_task_image"]
            self.assertFalse(runtime.execute("finish_task",args)["ok"])
            args.update(status="blocked",artifact_ids=[],unfulfilled=["没有去云模型"])
            self.assertTrue(runtime.execute("finish_task",args)["ok"])

    def test_real_terramind_then_class_selection_area_and_map(self):
        with task_fixture(network=True) as (_,_,server,runtime):
            first=runtime.execute("classify_landcover",{"input_id":"input_1"});self.assertTrue(first["ok"],first)
            selected=runtime.execute("select_classes",{"artifact_id":first["raster_id"],"class_codes":[4]});self.assertTrue(selected["ok"],selected)
            area=runtime.execute("calculate_area",{"artifact_id":selected["raster_id"],"class_codes":[1]});self.assertTrue(area["ok"],area)
            preview=runtime.execute("render_map",{"artifact_id":selected["raster_id"]});self.assertTrue(preview["ok"],preview)
            self.assertTrue(runtime.execute("validate_artifacts",{})["ok"])
            ids=[selected["raster_id"],*area["artifacts"],*preview["artifacts"]]
            result=runtime.execute("finish_task",{"status":"completed","artifact_ids":ids,"summary":"已生成农田候选与面积","unfulfilled":[]})
            self.assertTrue(result["ok"],result)
            stats=runtime.execute("summarize_raster",{"artifact_id":selected["raster_id"]})["statistics"]
            self.assertEqual(stats["classes"][2]["pixels"],1)

    def test_repeating_successful_model_call_reuses_current_result(self):
        with task_fixture(network=True) as (_,_,server,runtime):
            server.model.predict=Mock(wraps=server.model.predict)
            a=runtime.execute("classify_landcover",{"input_id":"input_1"})
            b=runtime.execute("classify_landcover",{"input_id":"input_1"})
            self.assertEqual(a,b);self.assertEqual(server.model.predict.call_count,1)

    def test_artifact_changed_after_validation_is_not_accepted(self):
        with task_fixture() as (_,_,_,runtime):
            r=runtime.execute("compute_index",{"input_id":"input_1","index":"NDVI"})
            p,_=runtime.artifact(r["raster_id"]);p.write_bytes(p.read_bytes()+b"changed")
            with self.assertRaises(ToolLoopError):runtime.execute("validate_artifacts",{})

    def test_model_can_replan_after_real_tool_error(self):
        with task_fixture() as (_,_,_,runtime):
            client=ToolLoopTests().client(call("retrieve_temperature",{"input_id":"input_1"}),
                call("compute_index",{"input_id":"input_1","index":"NDVI"},2),
                call("render_map",{"artifact_id":"artifact_1"},3),call("validate_artifacts",n=4),
                call("finish_task",{"status":"partial","artifact_ids":["artifact_1","artifact_2"],"summary":"完成植被指数，无法测温","unfulfilled":["输入不符合温度工具"]},5))
            result=ToolCallLoop(client,"offline").run([],TOOL_SPECS,runtime.execute)
            self.assertEqual(result["status"],"partial")
            second=client.chat.completions.create.call_args_list[1].kwargs["messages"]
            self.assertIn('"ok": false',second[-1]["content"])

    def test_complete_role_chain_uses_tool_calls_without_legacy_llm(self):
        from agents.manager_agent import ManagerAgent
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from agents.base_agent import BaseAgent
        from core.message import Message,MessageType
        from core.execution_cycle import run_execution_cycle
        from core.state_machine import ConversationPhase
        with task_fixture(network=True) as (root,session,_,runtime):
            runtime.output.rmdir()
            (root/"prompts").mkdir();(root/"prompts/tool_agent_prompt.txt").write_text((PROJECT/"prompts/tool_agent_prompt.txt").read_text(encoding="utf-8"),encoding="utf-8")
            kw=dict(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            manager,scientist,engineer,executor,supervisor=[cls(**kw) for cls in [ManagerAgent,ScientistAgent,EngineerAgent,ExecutorAgent,SupervisorAgent]]
            for agent in [manager,scientist,engineer,executor,supervisor]:session.register_agent(agent)
            executor._project_root=supervisor._project_root=str(root)
            executor._client=ToolLoopTests().client(call("inspect_data"),call("classify_landcover",{"input_id":"input_1"},2),
                call("select_classes",{"artifact_id":"artifact_1","class_codes":[4]},3),
                call("calculate_area",{"artifact_id":"artifact_4","class_codes":[1]},4),
                call("render_map",{"artifact_id":"artifact_4"},5),call("validate_artifacts",n=6),
                call("finish_task",{"status":"completed","artifact_ids":["artifact_4","artifact_5","artifact_6"],"summary":"农田候选图和候选面积已生成","unfulfilled":[]},7))
            session.user_request="";session.state_machine.force_transition(ConversationPhase.USER_INPUT)
            with patch.object(BaseAgent,"_call_llm",side_effect=AssertionError("旧文字规划路径不应调用")), patch("tools.io_kit._get_project_root",return_value=str(root)):
                confirm=manager.handle_message(Message(sender="User",receiver="Manager",content="找出农田并统计面积"))
                self.assertIn("自主选择",confirm.content)
                approved=manager.handle_message(Message(sender="User",receiver="Manager",content="approve",msg_type=MessageType.CONTROL))
                plan=scientist.handle_message(approved);code=engineer.handle_message(plan)
                result=run_execution_cycle(session,engineer,executor,supervisor,code,max_attempts=2)
                self.assertIsNotNone(result,session.get_terminate_reason())
                final=manager.handle_message(result)
                self.assertIn("农作物",final.content)
                self.assertEqual(session.result_delivery["completion"],"candidate")
                self.assertEqual(executor._client.chat.completions.create.call_count,7)
                self.assertIn("tool_agent_plan",session.to_dict())




class ToolProtocolBoundaryTests(unittest.TestCase):
    def test_duplicate_json_fields_are_returned_as_error(self):
        item=response("inspect_data");item.choices[0].message.tool_calls[0].function.arguments='{"input_id":"a","input_id":"b"}'
        client=ToolLoopTests().client(item,response("finish_task",identity="call_2"))
        execute=Mock(return_value={"ok":True,"_finish":True})
        ToolCallLoop(client,"offline").run([],[],execute)
        self.assertEqual(execute.call_count,1)
        message=client.chat.completions.create.call_args_list[1].kwargs["messages"][-1]
        self.assertIn("重复字段",message["content"])

    def test_duplicate_call_id_cannot_execute_twice(self):
        client=ToolLoopTests().client(response("inspect_data"),response("inspect_data"))
        execute=Mock(return_value={"ok":True})
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline").run([],[],execute)
        self.assertEqual(execute.call_count,1)

    def test_finish_and_work_cannot_be_in_one_batch(self):
        item=response("finish_task")
        second=response("inspect_data",identity="call_2").choices[0].message.tool_calls[0]
        item.choices[0].message.tool_calls.append(second)
        client=ToolLoopTests().client(item)
        execute=Mock()
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline",max_rounds=1).run([],[],execute)
        execute.assert_not_called()

    def test_cancelled_session_never_calls_provider(self):
        client=ToolLoopTests().client(response("inspect_data"))
        with self.assertRaises(ToolLoopError):ToolCallLoop(client,"offline",active=lambda:False).run([],[],Mock())
        client.chat.completions.create.assert_not_called()

    def test_schema_registry_does_not_expose_python_or_paths(self):
        from tools.registry import get_agent_tool_schemas
        schemas=get_agent_tool_schemas();names={x["function"]["name"] for x in schemas}
        self.assertEqual(len(names),12)
        self.assertNotIn("run_python",names)
        schemas[0]["function"]["name"]="changed"
        self.assertNotEqual(get_agent_tool_schemas()[0]["function"]["name"],"changed")

    def test_budget_and_config_types_are_checked(self):
        for config in [{"enabled":"true"},{"max_calls":True},{"max_rounds":100},{"unknown":1}]:
            with self.assertRaises(ValueError):workflow.options(config)

    def test_blocked_missing_capability_has_no_fake_result(self):
        with task_fixture() as (_,_,_,runtime):
            client=ToolLoopTests().client(call("list_capabilities"),call("finish_task",{"status":"blocked","artifact_ids":[],"summary":"未安装去云模型","unfulfilled":["去云"]},2))
            result=ToolCallLoop(client,"offline").run([],TOOL_SPECS,runtime.execute)
            self.assertEqual(result["status"],"blocked");self.assertFalse(runtime.artifacts)

    def test_gpu_failure_is_returned_and_not_retried(self):
        with task_fixture() as (_,_,_,runtime), patch("tools.terramind_remote.post",side_effect=RuntimeError("地物模型推理失败（HTTP 503）")) as post:
            first=runtime.execute("classify_landcover",{"input_id":"input_1"})
            second=runtime.execute("classify_landcover",{"input_id":"input_1"})
            self.assertFalse(first["ok"]);self.assertEqual(first,second);self.assertEqual(post.call_count,1)
            self.assertIn("503",first["error"]["message"])

    def test_local_water_and_temperature_use_existing_fixed_tools(self):
        from tests.test_water_agent import sample as water_sample
        from tests.test_lst_agent import sample as lst_sample
        from tools.data_context import load_data_context
        from core.session import Session
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);water_sample(root);lst_sample(root)
            session=Session(tool_agent_options=workflow.options(enabled=True));session.start()
            session.user_request="提取水体并计算地表温度"
            session.selected_inputs=["water_01/image.tif","lst_01/image.tif"]
            session.data_context=load_data_context(root/"data");ensure_task_contract(session)
            runtime=AgentTools(root,root/"results"/session.result_subdir,workflow.make_plan(session))
            water=runtime.execute("extract_water",{"input_id":"input_1","method":"mndwi"})
            self.assertTrue(water["ok"],water)
            temperature=runtime.execute("retrieve_temperature",{"input_id":"input_2"})
            self.assertTrue(temperature["ok"],temperature)
            self.assertTrue(runtime.execute("validate_artifacts",{})["ok"])

    def test_prithvi_wrapper_has_raw_packet_verification(self):
        spec=importlib.util.spec_from_file_location("tool_agent_prithvi_fixture",PROJECT/"tests/test_prithvi_remote.py")
        pr=importlib.util.module_from_spec(spec);spec.loader.exec_module(pr)
        with pr.fixture(network=True) as (root,session,server):
            session.tool_agent_options=workflow.options(enabled=True)
            runtime=AgentTools(root,root/"results"/session.result_subdir,workflow.make_plan(session))
            first=runtime.execute("extract_water",{"input_id":"input_1","method":"prithvi"})
            self.assertTrue(first["ok"],first)
            self.assertTrue(runtime.execute("validate_artifacts",{})["ok"])




class ToolAgentEntryTests(unittest.TestCase):
    def script(self):
        return ToolLoopTests().client(call("compute_index",{"input_id":"input_1","index":"NDVI"}),
            call("render_map",{"artifact_id":"artifact_1"},2),call("validate_artifacts",n=3),
            call("finish_task",{"status":"completed","artifact_ids":["artifact_1","artifact_2"],"summary":"已计算NDVI与图示","unfulfilled":[]},4))

    def test_gui_pipeline_has_plain_result_and_replayable_tool_trace(self):
        from tests.test_ui_updates import make_view
        from agents.manager_agent import ManagerAgent
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from agents.base_agent import BaseAgent
        from core.state_machine import ConversationPhase
        with task_fixture() as (root,session,_,runtime):
            runtime.output.rmdir();(root/"prompts").mkdir()
            (root/"prompts/tool_agent_prompt.txt").write_text((PROJECT/"prompts/tool_agent_prompt.txt").read_text(encoding="utf-8"),encoding="utf-8")
            kw=dict(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            agents=[cls(**kw) for cls in [ManagerAgent,ScientistAgent,EngineerAgent,ExecutorAgent,SupervisorAgent]]
            for agent in agents:session.register_agent(agent)
            manager,scientist,engineer,executor,supervisor=agents
            executor._project_root=supervisor._project_root=str(root);executor._client=self.script()
            session.user_request=session.confirmed_task="计算这张图的NDVI并给我图示";ensure_task_contract(session)
            view=make_view();view.session=session;view.config={"executor_max_retries":2}
            with patch("tools.io_kit._get_project_root",return_value=str(root)),patch.object(BaseAgent,"_call_llm",side_effect=AssertionError("不应进入旧文本路径")):
                view._run_analysis_pipeline(*agents,session.state_machine)
            self.assertEqual(session.state_machine.get_current_phase(),ConversationPhase.COMPLETED,session.get_terminate_reason())
            view._running=False;updates=view.get_updates()
            self.assertEqual(updates["result_delivery"]["completion"],"candidate")
            self.assertTrue(updates["result_delivery"]["previews"])
            self.assertIn("NDVI",str(updates))

    def test_gui_startup_suggestions_do_not_request_paid_model(self):
        from tests.test_ui_updates import make_view
        from agents.supervisor_agent import SupervisorAgent
        view=make_view();view._tool_agent_options=workflow.options(enabled=True)
        with patch.object(SupervisorAgent,"analyze_data_potential",side_effect=AssertionError("启动不应付费")):
            self.assertIn("启动扫描不调用付费模型",view._build_data_analysis_html())

    def test_real_cli_entry_uses_the_same_tool_cycle(self):
        import os,io,shutil
        from contextlib import redirect_stdout
        from core.session import Session
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from agents.base_agent import BaseAgent
        import main
        with task_fixture() as (root,session,_,runtime):
            runtime.output.rmdir()
            shutil.copytree(PROJECT/"prompts",root/"prompts")
            (root/"config.json").write_text(json.dumps({"model":"offline","api_key":"offline","base_url":"http://127.0.0.1:1"}),encoding="utf-8")
            kw=dict(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            executor=ExecutorAgent(**kw);executor._project_root=str(root);executor._client=self.script()
            supervisor=SupervisorAgent(**kw);supervisor._project_root=str(root)
            session.user_request="";session.selected_inputs=[]
            old=os.getcwd();os.chdir(root)
            try:
                with patch("core.session.Session",return_value=session),patch("agents.executor_agent.ExecutorAgent",return_value=executor),patch("agents.supervisor_agent.SupervisorAgent",return_value=supervisor),patch("tools.io_kit._get_project_root",return_value=str(root)),patch("builtins.input",side_effect=["计算sample/input.tif的NDVI并给我图示","approve"]),patch.object(BaseAgent,"_call_llm",side_effect=AssertionError("不应进入旧文本路径")),redirect_stdout(io.StringIO()):
                    main.run_cli(tool_agent=True)
            finally:os.chdir(old)
            self.assertEqual(session.result_delivery["completion"],"candidate",session.get_terminate_reason())
            self.assertEqual(executor._client.chat.completions.create.call_count,4)

    def test_ndvi_maps_semantic_bands_after_order_changes(self):
        from tools.agent_tools import input_identity
        with task_fixture() as (root,session,_,runtime):
            source=root/"data/sample/input.tif"
            with rasterio.open(source,"r+") as dst:
                values=dst.read();names=list(dst.descriptions)
                values[names.index("B8")]=3000;values[names.index("B4")]=1000
                values[:,0,0]=0;order=list(reversed(range(12)))
                dst.write(values[order])
                for i,n in enumerate(order,1):dst.set_band_description(i,names[n])
            runtime.plan["inputs"]["input_1"]["identity"]=input_identity(source)
            result=runtime.execute("compute_index",{"input_id":"input_1","index":"NDVI"})
            self.assertTrue(result["ok"],result)
            path,_=runtime.artifact(result["raster_id"])
            with rasterio.open(path) as src:self.assertTrue(np.allclose(src.read(1,masked=True).compressed(),.5))


if __name__=="__main__":unittest.main()
