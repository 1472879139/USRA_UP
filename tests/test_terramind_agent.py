"""统一Agent与TerraMind远程链路回归；不连接真实GPU或收费模型。"""
from contextlib import contextmanager,redirect_stdout
from copy import deepcopy
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
import zipfile
import numpy as np
import rasterio
from rasterio.transform import from_origin
from tools import terramind_protocol as protocol
from tools import terramind_remote as remote
from tools import terramind_workflow as workflow
from tools import remote_models
from tools.data_context import load_data_context
from tools.task_contract import ensure_task_contract
from tools.result_audit import audit_execution_results
from tools.result_delivery import build_result_delivery
from core.session import Session
from core.message import Message,MessageType

PROJECT=Path(__file__).resolve().parents[1]
sys.modules["terramind_protocol"]=protocol
spec=importlib.util.spec_from_file_location("terramind_http_service_test",PROJECT/"deploy/terramind_gpu/service.py")
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)


def image(path,empty=False):
    data=np.full((12,224,224),1000,dtype="int16")
    data[:,0,0]=0
    if empty:data[:]=0
    with rasterio.open(path,"w",driver="GTiff",height=224,width=224,count=12,dtype="int16",nodata=0,
                       crs="EPSG:32632",transform=from_origin(500000,6000000,10,10)) as dst:
        dst.write(data)
        for n,name in enumerate(protocol.BAND_NAMES,1):dst.set_band_description(n,name)


class FakeModel:
    info={"offline_test_double":True}
    def predict(self,raw,valid):
        mask=np.full((224,224),4,dtype="uint8");mask[:112]=2;mask[1,1]=0
        mask[~valid]=255
        return mask,{"forward_seconds":0.0}


@contextmanager
def fixture(network=False,include_empty=False):
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);data=root/"data/sample";data.mkdir(parents=True)
        image(data/"input.tif")
        (data/"metadata.json").write_text(json.dumps({"sensor":"Sentinel-2","product_level":"L2A",
             "encoding":"DN/10000","bands":protocol.BAND_NAMES}),encoding="utf-8")
        server=None
        if network:
            server=service.LandcoverServer(("127.0.0.1",0),FakeModel(),root/"scratch")
            threading.Thread(target=server.serve_forever,daemon=True).start()
        config={"enabled":False,"terramind":{"enabled":True,"base_url":f"http://127.0.0.1:{server.server_port if server else 18766}","timeout_seconds":2}}
        (root/"gpu.local.json").write_text(json.dumps(config),encoding="utf-8")
        session=Session();session.start();session.user_request="给这张影像做一张地物分类图，说明主要地物和各类占比。"
        session.selected_inputs=["sample/input.tif"]
        if include_empty:
            image(data/"empty.tif",True);session.selected_inputs.append("sample/empty.tif")
        session.data_context=load_data_context(root/"data")
        ensure_task_contract(session)
        try:
            yield root,session,server
        finally:
            if server:server.shutdown();server.server_close()


def run(root,session,network=False):
    plan=workflow.select_remote_tool(session,root)
    session.remote_tool_plan=plan
    output=root/"results"/session.result_subdir
    workflow.run_remote_workflow(plan,output,root)
    names=[p.relative_to(root/"results").as_posix() for p in output.rglob("*") if p.is_file()]
    session.result_audit=audit_execution_results(root/"results",names,session.user_request,task_subdir=session.result_subdir)
    workflow.attach_remote_audit(session,root)
    return output


class TerraMindAgentTests(unittest.TestCase):
    def test_l2a_input_and_partial_nodata_contract(self):
        with fixture() as (root,session,_):
            value=protocol.inspect_input(root/"data/sample/input.tif")
            self.assertEqual(value["valid_pixels"],224*224-1)

    def test_l1c_metadata_is_not_silently_reinterpreted(self):
        with fixture() as (root,session,_):
            path=root/"data/sample/metadata.json";data=json.loads(path.read_text());data["product_level"]="L1C";path.write_text(json.dumps(data))
            plan=workflow.select_remote_tool(session,root)
            self.assertFalse(plan["inputs"])
            self.assertIn("ERROR:",workflow.confirmation_text(plan))

    def test_empty_input_is_rejected_before_http(self):
        with fixture(include_empty=True) as (root,session,_):
            plan=workflow.select_remote_tool(session,root)
            self.assertEqual(len(plan["inputs"]),1);self.assertEqual(len(plan["rejected_inputs"]),1)

    def test_missing_metadata_is_not_adapted_by_filename(self):
        with fixture() as (root,session,_):
            (root/"data/sample/metadata.json").unlink()
            self.assertFalse(workflow.select_remote_tool(session,root)["inputs"])

    def test_raw_reflectance_not_accepted_as_dn(self):
        with fixture() as (root,session,_):
            path=root/"data/sample/input.tif"
            with rasterio.open(path,"r+") as dst:dst.write(np.full((12,224,224),1,dtype="int16"))
            with self.assertRaisesRegex(ValueError,"DN"):protocol.inspect_input(path)

    def test_protocol_preserves_unknown_zero_and_invalid_255(self):
        with fixture() as (root,session,_):
            raw,valid,profile=protocol.read_input(root/"data/sample/input.tif")
            mask,_=FakeModel().predict(raw,valid)
            response=protocol.package_response(mask,profile,"a"*32,"b"*64,{})
            actual,manifest=protocol.unpack_response(response,"a"*32,"b"*64,profile,valid)
            self.assertEqual(actual[1,1],0);self.assertEqual(actual[0,0],255)
            self.assertFalse(manifest["accuracy_evaluated"]);self.assertEqual(manifest["seed"],20261028)

    def test_wrong_request_or_input_refused(self):
        with fixture() as (root,session,_):
            raw,valid,profile=protocol.read_input(root/"data/sample/input.tif");mask,_=FakeModel().predict(raw,valid)
            response=protocol.package_response(mask,profile,"a"*32,"b"*64,{})
            with self.assertRaises(ValueError):protocol.unpack_response(response,"c"*32,"b"*64,profile,valid)
            with self.assertRaises(ValueError):protocol.unpack_response(response,"a"*32,"d"*64,profile,valid)

    def test_illegal_class_and_changed_valid_area_refused(self):
        with fixture() as (root,session,_):
            raw,valid,profile=protocol.read_input(root/"data/sample/input.tif");mask,_=FakeModel().predict(raw,valid)
            mask[5,5]=100
            with self.assertRaises(ValueError):protocol.encode_mask(mask,profile)
            mask[5,5]=255
            with self.assertRaises(ValueError):protocol.decode_mask(protocol.encode_mask(mask,profile),profile,valid)

    def test_endpoint_must_be_private_loopback(self):
        with fixture() as (root,session,_):
            path=root/"gpu.local.json";data=json.loads(path.read_text());data["terramind"]["base_url"]="http://example.com:18766";path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):remote.settings(root)

    def test_unsupported_area_or_target_request_not_replaced(self):
        with fixture() as (root,session,_):
            for text in ["只输出地物分类GeoJSON","地物分类并统计平方公里面积","提取水体","地物前后变化"]:
                session.user_request=text;ensure_task_contract(session)
                self.assertIsNone(workflow.select_remote_tool(session,root),text)

    def test_unified_selector_retains_local_tools_and_prithvi(self):
        from tools.installed_workflow import select_installed_tool
        with fixture() as (root,session,_):
            selected=remote_models.select_remote_tool(session,root)
            self.assertEqual(selected["name"],protocol.VERSION)
            self.assertIsNone(select_installed_tool(session))
            session.user_request="提取水体";ensure_task_contract(session)
            sentinel={"name":"prithvi-remote-v1"}
            with patch("tools.prithvi_workflow.select_remote_tool",return_value=sentinel):
                self.assertIs(remote_models.select_remote_tool(session,root),sentinel)

    def test_manager_and_scientist_lock_terramind_without_llm(self):
        from agents.manager_agent import ManagerAgent
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        with fixture() as (root,session,_),patch("tools.io_kit._get_project_root",return_value=str(root)):
            manager=ManagerAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            scientist=ScientistAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            engineer=EngineerAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            for agent in [manager,scientist,engineer]:session.register_agent(agent)
            with patch.object(manager,"_call_llm",side_effect=AssertionError("不应请求模型")):
                reply=manager._confirmation_response("")
                self.assertIn("TerraMind",reply)
            plan=scientist.handle_message(Message(sender="Manager",receiver="Scientist",content=session.user_request))
            code=engineer.handle_message(plan)
            self.assertEqual(code.msg_type,MessageType.CODE)
            self.assertIn("tools.terramind_workflow",code.content)
            self.assertEqual(session.to_dict()["remote_tool_plan"]["name"],protocol.VERSION)

    def test_remote_roundtrip_audit_and_plain_delivery(self):
        with fixture(network=True,include_empty=True) as (root,session,server):
            output=run(root,session)
            self.assertFalse(session.result_audit["errors"],session.result_audit["errors"])
            delivery=build_result_delivery(session,root)
            self.assertIn("地物",delivery["headline"])
            self.assertIn("1幅输入",delivery["headline"])
            self.assertEqual(delivery["completion"],"candidate")
            self.assertTrue(delivery["scene_results"][0]["preview"])
            self.assertIn("准确性尚未",delivery["verification"])
            self.assertFalse(any("83.43" in line for line in delivery["findings"]))

    def test_changed_input_or_configuration_stops_before_request(self):
        with fixture() as (root,session,_),patch.object(remote,"post") as post:
            plan=workflow.select_remote_tool(session,root)
            path=root/"gpu.local.json";path.write_text(path.read_text()+" ")
            with self.assertRaises(ValueError):workflow.run_remote_workflow(plan,root/"results"/session.result_subdir,root)
            post.assert_not_called()

    def test_wrong_artifact_stats_and_returned_mask_are_refused(self):
        with fixture(network=True) as (root,session,server):
            output=run(root,session)
            table=output/"landcover_01/terramind_class_statistics.csv"
            table.write_text(table.read_text(encoding="utf-8-sig").replace("树木","假树木"),encoding="utf-8-sig")
            with self.assertRaisesRegex(ValueError,"哈希"):workflow.attach_remote_audit(session,root)

    def test_remote_failure_does_not_fallback_or_use_old_result(self):
        with fixture(network=True) as (root,session,server):
            server.model.predict=Mock(side_effect=RuntimeError("simulated"))
            with self.assertRaisesRegex(RuntimeError,"503"):
                run(root,session)
            self.assertFalse((root/"results"/session.result_subdir/"terramind_workflow.json").exists())

    def test_real_executor_child_to_loopback_gpu_and_supervisor(self):
        from agents.executor_agent import ExecutorAgent
        with fixture(network=True) as (root,session,server):
            for name in ["tools","core"]:
                shutil.copytree(PROJECT/name,root/name,ignore=shutil.ignore_patterns("__pycache__"))
            session.remote_tool_plan=workflow.select_remote_tool(session,root)
            code=workflow.tool_message(session,deepcopy(session.remote_tool_plan))
            executor=ExecutorAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            executor._project_root=str(root);session.register_agent(executor)
            server.model.predict=Mock(wraps=server.model.predict)
            with patch.dict(os.environ,{"PYTHONPATH":""}):
                result=executor.handle_message(code)
            self.assertEqual(result.msg_type,MessageType.RESULT,result.content)
            server.model.predict.assert_called_once()
            session.result_audit=audit_execution_results(root/"results",result.metadata["files_generated"],session.user_request,task_subdir=session.result_subdir)
            self.assertFalse(session.result_audit["errors"],session.result_audit["errors"])
            remote_models.attach_remote_audit(session,root)
            self.assertIn("remote_landcover",session.result_audit)

    def test_packaged_service_imports_its_own_runtime_and_has_no_labels(self):
        spec=importlib.util.spec_from_file_location("terramind_packager",PROJECT/"deploy/terramind_gpu/build_service.py")
        builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive=root/"landcover.pyz";builder.build(archive)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()),{"__main__.py","service.py","runtime.py","terramind_protocol.py","deploy.py","landcover.py"})
            original_import=builtins.__import__;factory=Mock(return_value=FakeModel())
            def fake_import(name,*args,**kwargs):
                module=original_import(name,*args,**kwargs)
                if name=="runtime":
                    self.assertTrue(Path(module.__file__).is_relative_to(root/"ursa-gpu-runtime/services"))
                    module.GPUModel=factory
                return module
            saved_path,saved_bytecode=list(sys.path),sys.dont_write_bytecode
            try:
                with patch.dict(sys.modules):
                    for name in ["runtime","deploy","landcover"]:sys.modules.pop(name,None)
                    with patch.object(Path,"home",return_value=root),patch.object(sys,"argv",[str(archive)]),patch.object(builtins,"__import__",side_effect=fake_import),patch("http.server.ThreadingHTTPServer.__init__",return_value=None) as bind,patch("http.server.ThreadingHTTPServer.serve_forever",side_effect=KeyboardInterrupt),patch("http.server.ThreadingHTTPServer.server_close"),redirect_stdout(io.StringIO()):
                        runpy.run_path(str(archive),run_name="__main__")
                    factory.assert_called_once()
                    self.assertEqual(bind.call_args.args[0],("127.0.0.1",18766))
            finally:
                sys.path[:]=saved_path;sys.dont_write_bytecode=saved_bytecode

    def test_full_role_chain_keeps_plain_result_and_never_calls_paid_model(self):
        from agents.base_agent import BaseAgent
        from agents.manager_agent import ManagerAgent
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from core.execution_cycle import run_execution_cycle
        with fixture(network=True) as (root,session,server),patch("tools.io_kit._get_project_root",return_value=str(root)):
            for name in ["tools","core"]:
                shutil.copytree(PROJECT/name,root/name,ignore=shutil.ignore_patterns("__pycache__"))
            kw=dict(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            manager,scientist,engineer,executor,supervisor=[cls(**kw) for cls in
                [ManagerAgent,ScientistAgent,EngineerAgent,ExecutorAgent,SupervisorAgent]]
            executor._project_root=supervisor._project_root=str(root)
            for agent in [manager,scientist,engineer,executor,supervisor]:session.register_agent(agent)
            text=session.user_request
            session.user_request=""
            with patch.object(BaseAgent,"_call_llm",side_effect=AssertionError("固定能力不应调用付费模型")):
                confirmation=manager.handle_message(Message(sender="User",receiver="Manager",content=text))
                self.assertIn("TerraMind",confirmation.content)
                approved=manager.handle_message(Message(sender="User",receiver="Manager",content="approve",msg_type=MessageType.CONTROL))
                planned=scientist.handle_message(approved)
                code=engineer.handle_message(planned)
                result=run_execution_cycle(session,engineer,executor,supervisor,code,max_attempts=2)
                self.assertIsNotNone(result,session.get_terminate_reason())
                message=manager.handle_message(result)
                self.assertIn("地物候选",message.content)
                self.assertEqual(session.result_delivery["completion"],"candidate")
                self.assertTrue(session.result_delivery["scene_results"][0]["preview"])

    def test_common_landcover_phrases_all_select_the_same_tool(self):
        with fixture() as (root,session,_):
            for text in ["给我一张地物分类图","识别这张影像的主要地物并给出占比","帮我做土地覆盖分类"]:
                session.user_request=text;ensure_task_contract(session)
                self.assertEqual(remote_models.select_remote_tool(session,root)["name"],protocol.VERSION)

    def test_busy_service_returns_429_without_running_model(self):
        with fixture(network=True) as (root,session,server):
            server.model.predict=Mock(wraps=server.model.predict)
            server.gpu_slot.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError,"429"):run(root,session)
                server.model.predict.assert_not_called()
            finally:
                server.gpu_slot.release()

    def test_all_unknown_output_is_not_described_as_successful_recognition(self):
        mask=np.zeros((224,224),dtype="uint8")
        rows=protocol.statistics(mask,np.ones_like(mask,dtype=bool))
        text=" ".join(remote.findings(rows))
        self.assertIn("无法给出可靠的主要地物结论",text)
        self.assertIn("50176个有效像元",text)

    def test_response_zip_tampering_refused(self):
        with fixture() as (root,session,_):
            raw,valid,profile=protocol.read_input(root/"data/sample/input.tif")
            mask,_=FakeModel().predict(raw,valid)
            content=protocol.package_response(mask,profile,"a"*32,"b"*64,{})
            with zipfile.ZipFile(io.BytesIO(content)) as original:
                files={name:original.read(name) for name in original.namelist()}
            files["classification.tif"]+=b"changed"
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,"w") as archive:
                for name,data in files.items():archive.writestr(name,data)
            with self.assertRaisesRegex(ValueError,"哈希"):
                protocol.unpack_response(buffer.getvalue(),"a"*32,"b"*64,profile,valid)


if __name__=="__main__":
    unittest.main()
