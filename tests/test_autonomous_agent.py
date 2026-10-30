"""自主回答、代码执行、数据适配和需求验收；使用合成数据与模拟SDK。"""
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from tests.test_tool_agent import response,task_fixture
from core.tool_call_loop import ToolCallLoop


class AutonomousLoopTests(unittest.TestCase):
    def test_explanation_can_finish_without_a_tool_call(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response(text="NDVI是归一化植被指数。")))))
        result=ToolCallLoop(client,"offline",on_text=lambda text:{"status":"answered","summary":text}).run([],[],Mock())
        self.assertEqual(result["status"],"answered")
        self.assertEqual(client.chat.completions.create.call_count,1)

    def test_plain_claim_cannot_finish_an_image_computation(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=[response(text="已算出面积"),response("finish_task",identity="c2")]))))
        execute=Mock(return_value={"ok":True,"_finish":True,"status":"partial"})
        result=ToolCallLoop(client,"offline",on_text=lambda text:None).run([],[],execute)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(client.chat.completions.create.call_count,2)


class AcceptanceTests(unittest.TestCase):
    def test_csv_only_does_not_require_a_picture(self):
        from tools.task_acceptance import infer_contract
        contract=infer_contract(["统计每个波段均值，只要CSV，不要图片"],["input_1"])
        self.assertIn("table",[r["kind"] for r in contract["requirements"]])
        self.assertNotIn("map",[r["kind"] for r in contract["requirements"]])

    def test_later_revision_replaces_the_picture_preference(self):
        from tools.task_acceptance import infer_contract
        contract=infer_contract(["给我结果图和面积","改成只要CSV，不要图"],["input_1"])
        self.assertNotIn("map",[r["kind"] for r in contract["requirements"]])
        self.assertIn("area",[r["kind"] for r in contract["requirements"]])

    def test_missing_area_cannot_be_declared_complete(self):
        from tools.task_acceptance import infer_contract,check_requirements
        contract=infer_contract(["给我农田分布图和平方米面积"],["input_1"])
        with tempfile.TemporaryDirectory() as tmp:
            state=check_requirements(contract,{},Path(tmp))
        self.assertFalse(state["complete"])
        self.assertTrue(any(r["kind"]=="area" and not r["met"] for r in state["items"]))

    def test_explanation_is_not_classified_as_an_execution_request(self):
        from tools.task_acceptance import is_processing_request
        self.assertFalse(is_processing_request("NDVI如何计算？解释公式。"))
        self.assertTrue(is_processing_request("帮我计算这张图的NDVI，并解释公式。"))


def raster(path,*,width=32,height=24,order=None,encoding="DN/10000"):
    from tools.terramind_protocol import BAND_NAMES
    order=order or BAND_NAMES
    value=np.stack([np.full((height,width),1000+i*200,dtype="float32") for i in range(len(order))])
    if encoding=="reflectance":value/=10000
    with rasterio.open(path,"w",driver="GTiff",height=height,width=width,count=len(order),dtype="float32",crs="EPSG:32650",transform=from_origin(500000,4000000,10,10),nodata=-9999) as dst:
        dst.write(value);dst.descriptions=tuple(order)
    (path.parent/"metadata.json").write_text(json.dumps({"sensor":"Sentinel-2","product_level":"L2A","encoding":encoding,"bands":order}),encoding="utf-8")
    return value


class AdapterTests(unittest.TestCase):
    def test_shuffled_reflectance_is_reordered_and_scaled_without_changing_source(self):
        from tools.data_adapter import prepare_tiles,verify_prepared
        from tools.terramind_protocol import BAND_NAMES,read_input
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"input.tif";order=list(reversed(BAND_NAMES))
            raw=raster(source,order=order,encoding="reflectance");before=source.read_bytes()
            result=prepare_tiles(source,root/"prepared","terramind")
            self.assertEqual(len(result["tiles"]),1)
            actual,valid,profile=read_input(root/"prepared"/result["tiles"][0]["path"])
            np.testing.assert_allclose(actual[0,:24,:32],raw[-1]*10000,rtol=1e-6)
            self.assertEqual(int(valid.sum()),32*24)
            self.assertEqual(source.read_bytes(),before)
            verify_prepared(source,root/"prepared",result)

    def test_large_input_is_tiled_at_native_resolution_and_covers_edges(self):
        from tools.data_adapter import prepare_tiles
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"input.tif";raster(source,width=420,height=300)
            result=prepare_tiles(source,root/"prepared","terramind")
            cover=np.zeros((300,420),dtype=bool)
            for tile in result["tiles"]:
                y,x,h,w=tile["window"];cover[y:y+h,x:x+w]=True
                with rasterio.open(root/"prepared"/tile["path"]) as src:
                    self.assertEqual(src.res,(10,10));self.assertEqual(src.shape,(224,224))
            self.assertTrue(cover.all());self.assertGreater(len(result["tiles"]),1)

    def test_unknown_sensor_is_not_guessed_from_filename(self):
        from tools.data_adapter import prepare_tiles
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"sentinel_2_L2A.tif";raster(source)
            (root/"metadata.json").unlink()
            with self.assertRaises(ValueError):prepare_tiles(source,root/"prepared","terramind")

    def test_prepared_pixel_tampering_is_found_against_source(self):
        from tools.data_adapter import prepare_tiles,verify_prepared
        from tools.artifact_paths import file_hash
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"input.tif";raster(source)
            result=prepare_tiles(source,root/"prepared","terramind")
            tile=root/"prepared"/result["tiles"][0]["path"]
            with rasterio.open(tile,"r+") as dst:
                data=dst.read();data[0,0,0]+=3;dst.write(data)
            result["tiles"][0]["sha256"]=file_hash(tile)
            with self.assertRaises(ValueError):verify_prepared(source,root/"prepared",result)

    def test_reference_alignment_retains_category_codes(self):
        from tools.data_adapter import align_raster
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"source.tif";reference=root/"reference.tif"
            raster(source,width=32,height=24);raster(reference,width=16,height=12)
            align_raster(source,root/"aligned.tif",reference=reference,categorical=True)
            with rasterio.open(root/"aligned.tif") as aligned,rasterio.open(reference) as ref:
                self.assertEqual(aligned.transform,ref.transform);self.assertEqual(aligned.shape,ref.shape)

    def test_metadata_conflict_is_not_silently_overridden(self):
        from tools.data_adapter import prepare_tiles
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"input.tif";raster(source)
            with rasterio.open(source,"r+") as dst:dst.update_tags(sensor="Landsat-8")
            with self.assertRaises(ValueError):prepare_tiles(source,root/"prepared","terramind")


class RuntimeTests(unittest.TestCase):
    def test_container_command_has_no_network_and_no_project_mount(self):
        from tools.python_runtime import container_command
        command=container_command("sha256:"+"a"*64,"ursa-python-test",Path("C:/approved-job"))
        self.assertIn("--network=none",command)
        self.assertIn("--read-only",command)
        self.assertIn("--cap-drop=ALL",command)
        self.assertIn("--pull=never",command)
        self.assertNotIn("--privileged",command)
        self.assertFalse(any("docker.sock" in str(v) for v in command))

    def test_unavailable_container_never_executes_on_the_host(self):
        from tools import python_runtime
        with tempfile.TemporaryDirectory() as tmp,patch.object(python_runtime,"status",return_value={"available":False,"reason":"offline"}),patch.object(python_runtime.subprocess,"Popen") as process:
            result=python_runtime.run("print(1)",{},Path(tmp)/"job")
        self.assertFalse(result["ok"]);process.assert_not_called()


def prepare_prompt(root):
    import shutil
    target=root/"prompts";target.mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).resolve().parents[1]/"prompts/autonomous_agent_prompt.txt",target/"autonomous_agent_prompt.txt")


class AutonomousWorkflowTests(unittest.TestCase):
    def test_real_session_explanation_and_request_journal(self):
        from core.session import Session
        from tools.autonomous_workflow import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepare_prompt(root)
            session=Session();session.start();session.user_request="NDVI如何计算？解释公式。"
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response(text="NDVI=(近红外-红)/(近红外+红)。")))))
            result=run(session,client,"offline",root)
            self.assertEqual(result["completion"],"answered");self.assertFalse(result["files"])
            self.assertEqual(session.get_llm_activity()["total_requests"],1)
            events=[json.loads(line) for path in (root/session.result_relative_dir/"journal").glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["event"] for e in events],["request","response"])
            self.assertEqual(events[0]["request"]["messages"][1]["role"],"user")
            self.assertEqual(events[1]["assistant"]["content"],"NDVI=(近红外-红)/(近红外+红)。")

    def test_missing_image_can_be_asked_without_claiming_completion(self):
        from core.session import Session
        from tools.autonomous_workflow import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepare_prompt(root)
            session=Session();session.start();session.user_request="帮我计算这张影像的NDVI"
            replies=[response(text="已完成影像计算"),response("request_clarification",{"question":"请先选择需要分析的影像。","missing":["输入影像"]},identity="c2")]
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))
            result=run(session,client,"offline",root)
            self.assertEqual(result["completion"],"blocked")
            self.assertTrue(session.autonomous_state["awaiting_input"])
            self.assertEqual(session.user_request,"帮我计算这张影像的NDVI")
            self.assertEqual(client.chat.completions.create.call_count,2)

    def test_python_error_returns_to_same_model_then_csv_only_completes(self):
        from tools.autonomous_workflow import run
        from tools.tool_agent_workflow import options
        with task_fixture() as (root,session,_,unused):
            prepare_prompt(root);session.tool_agent_options=options(enabled=True,autonomous=True)
            session.user_request="统计每个波段均值，只要CSV，不要图片"
            outputs=[{"filename":"means.csv","kind":"table","label":"各波段均值","role":"data"}]
            args={"code":"bad_name","input_ids":["input_1"],"artifact_ids":[],"outputs":outputs}
            replies=[response("run_python",args,identity="c1"),response("run_python",{**args,"code":"print('fixed')"},identity="c2"),response("validate_artifacts",identity="c3"),response("finish_task",{"status":"completed","artifact_ids":["artifact_1"],"summary":"已生成各波段均值统计表。","unfulfilled":[]},identity="c4")]
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))
            count=0
            def execute(code,inputs,job,**kwargs):
                nonlocal count
                count+=1
                if count==1:return {"ok":False,"stderr":"NameError: bad_name","error":{"code":"python_execution_failed","message":"请修复"}}
                output=Path(job)/"output";output.mkdir(parents=True)
                with rasterio.open(inputs["input_1"]) as src:means=src.read(masked=True).mean(axis=(1,2))
                with (output/"means.csv").open("w",encoding="utf-8-sig",newline="") as stream:
                    writer=csv.writer(stream);writer.writerow(["波段","均值"]);writer.writerows(enumerate(means,1))
                return {"ok":True,"stdout":"fixed","stderr":"","output_dir":str(output)}
            with patch("tools.python_runtime.run",side_effect=execute):result=run(session,client,"offline",root)
            self.assertEqual(session.autonomous_state["status"],"completed")
            self.assertFalse(result["previews"])
            self.assertTrue(all(item["met"] for item in result["requirements"]))
            sent=client.chat.completions.create.call_args_list[1].kwargs["messages"]
            self.assertTrue(any(x.get("role")=="tool" and "NameError" in x["content"] for x in sent))

    def test_model_cannot_erase_the_users_area_requirement(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            runtime=AutonomousTools(root,unused.output,session.tool_agent_plan)
            result=runtime.execute("set_task_plan",{"mode":"analysis","objective":"仅做分类图","requirements":[]})
            self.assertTrue(result["ok"])
            self.assertTrue(any(r["kind"]=="area" for r in runtime.contract["requirements"]))
            self.assertFalse(runtime.execute("set_task_plan",{"mode":"answer","objective":"改为解释","requirements":[]})["ok"])

    def test_model_classification_selection_area_and_completion(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture(network=True) as (root,session,_,unused):
            runtime=AutonomousTools(root,unused.output,session.tool_agent_plan)
            classified=runtime.execute("classify_landcover",{"input_id":"input_1"})
            self.assertTrue(classified["ok"],classified)
            selected=runtime.execute("select_classes",{"artifact_id":classified["raster_id"],"class_codes":[4]})
            self.assertTrue(selected["ok"])
            runtime.execute("render_map",{"artifact_id":selected["raster_id"]})
            checked=runtime.execute("validate_artifacts",{})
            self.assertTrue(checked["ok"],checked)
            refused=runtime.execute("finish_task",{"status":"completed","artifact_ids":list(runtime.artifacts),"summary":"处理完成","unfulfilled":[]})
            self.assertFalse(refused["ok"]);self.assertIn("面积",refused["error"]["message"])
            area=runtime.execute("calculate_area",{"artifact_id":selected["raster_id"],"class_codes":[1]})
            self.assertTrue(area["ok"],area)
            self.assertTrue(runtime.execute("validate_artifacts",{})["ok"])
            accepted=runtime.execute("finish_task",{"status":"completed","artifact_ids":list(runtime.artifacts),"summary":"候选范围与平方米面积已生成。","unfulfilled":[]})
            self.assertTrue(accepted["ok"],accepted)

    def test_gui_post_goes_directly_to_autonomous_model_without_approve(self):
        from gui.app import create_app
        from tests.test_ui_updates import make_view
        from tools.tool_agent_workflow import options
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"gui").mkdir();prepare_prompt(root)
            view=make_view();view._running=False;view._tool_agent_options=options(enabled=True,autonomous=True)
            view.model="offline";view._data_summary_html="";view._data_analysis_html=""
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response(text="这是关于NDVI的解释。")))))
            view.agents={"executor":SimpleNamespace(_client=client,model="offline")}
            view._create_agents=Mock(return_value=view.agents);view._setup_session=Mock()
            with patch("gui.app.__file__",str(root/"gui/app.py")):
                app=create_app(controller=view);http=app.test_client()
                self.assertTrue(http.post('/api/chat',json={"message":"NDVI是什么"}).get_json()["success"])
                view._thread.join(5)
                snapshot=http.get('/api/updates?since=0').get_json()
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["result_delivery"]["completion"],"answered")
            self.assertEqual(client.chat.completions.create.call_count,1)


class AutonomousBoundaryTests(unittest.TestCase):
    def test_declared_plan_cannot_reduce_multi_input_coverage(self):
        from tools.task_acceptance import infer_contract,check_requirements
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"map.png").write_bytes(b"placeholder")
            contract=infer_contract(["给这两幅影像分别生成分布图"],["input_1","input_2"])
            result=check_requirements(contract,{"artifact_1":{"path":"map.png","kind":"image","role":"preview","input_ids":["input_1"]}},root)
            self.assertFalse(result["complete"])

    def test_fake_or_missing_area_values_are_rejected(self):
        from tools.task_acceptance import infer_contract,check_requirements
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with rasterio.open(root/"mask.tif","w",driver="GTiff",height=2,width=2,count=1,dtype="uint8",crs="EPSG:32650",transform=from_origin(500000,4000000,10,10),nodata=255) as dst:dst.write(np.array([[1,0],[0,0]],dtype="uint8"),1)
            contract=infer_contract(["统计平方米面积，只要CSV，不要图"],["input_1"])
            artifacts={"r":{"path":"mask.tif","kind":"raster","classes":{"0":"其他","1":"农田"},"input_ids":["input_1"]},
                       "t":{"path":"area.csv","kind":"table","input_ids":["input_1"],"measurement":{"kind":"area","raster_id":"r","class_codes":[1],"unit":"m2"}}}
            for value,expected in [("N/A",False),("999",False),("100",True)]:
                (root/"area.csv").write_text("类别,面积_m2\n农田,"+value,encoding="utf-8-sig")
                state=check_requirements(contract,artifacts,root)
                self.assertEqual(next(v["met"] for v in state["items"] if v["kind"]=="area"),expected)

    def test_python_cannot_invent_lst_from_declared_sentinel2(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            plan=dict(session.tool_agent_plan);plan["request"]="计算这张影像的地表温度"
            runtime=AutonomousTools(root,unused.output,plan)
            with patch('tools.python_runtime.run') as execute:
                result=runtime.execute('run_python',{"code":"print(25)","input_ids":["input_1"],"artifact_ids":[],"outputs":[{"filename":"temp.csv","kind":"table","label":"温度","role":"data"}]})
            self.assertFalse(result["ok"]);self.assertIn("热红外",result["error"]["message"]);execute.assert_not_called()

    def test_python_raster_cannot_invent_a_different_grid(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            runtime=AutonomousTools(root,unused.output,session.tool_agent_plan)
            altered=root/'altered.tif'
            with rasterio.open(root/'data/sample/input.tif') as src:
                profile=src.profile.copy();profile.update(count=1,transform=from_origin(1,2,1,1))
            with rasterio.open(altered,'w',**profile) as dst:dst.write(np.ones((224,224),dtype='int16'),1)
            with self.assertRaises(ValueError):runtime.check_code_grid(altered,[root/'data/sample/input.tif'])

    def test_geographic_classification_area_is_projected_before_counting(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            runtime=AutonomousTools(root,unused.output,session.tool_agent_plan)
            folder=runtime.folder();path=folder/'geographic.tif'
            with rasterio.open(path,'w',driver='GTiff',height=20,width=20,count=1,dtype='uint8',crs='EPSG:4326',transform=from_origin(116,40,.0001,.0001),nodata=255) as dst:dst.write(np.ones((20,20),dtype='uint8'),1)
            key=runtime.register(path,'raster',classes={'0':'其他','1':'农田'},role='classification');runtime.artifacts[key]['input_ids']=['input_1']
            result=runtime.execute('calculate_area',{'artifact_id':key,'class_codes':[1]})
            self.assertTrue(result['ok'],result)
            table=runtime.artifacts[next(iter(result['artifacts']))]
            with rasterio.open(runtime.output/runtime.artifacts[table['measurement']['raster_id']]['path']) as src:self.assertTrue(src.crs.is_projected)

    def test_tiled_model_keeps_native_grid_on_a_larger_image(self):
        from tools.model_adapter import run_tiled,verify_tiled
        with task_fixture(network=True) as (root,session,_,unused):
            source=root/'data/sample/input.tif';raster(source,width=420,height=300)
            target,manifest=run_tiled(source,unused.output/'tiled','terramind',root)
            self.assertGreater(len(manifest['responses']),1)
            with rasterio.open(source) as src,rasterio.open(target) as dst:
                self.assertEqual(dst.shape,src.shape);self.assertEqual(dst.transform,src.transform);self.assertEqual(dst.crs,src.crs)
            self.assertTrue(verify_tiled(source,target.parent))

    def test_new_cli_uses_the_same_autonomous_workflow(self):
        from tools.autonomous_workflow import run_cli
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepare_prompt(root)
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response(text="NDVI是植被指数。")))))
            with patch('openai.OpenAI',return_value=client),patch('builtins.input',side_effect=['NDVI是什么','end']),redirect_stdout(io.StringIO()) as printed:
                run_cli(root,{"api_key":"offline","base_url":"http://127.0.0.1:1","model":"offline"})
            self.assertIn('NDVI是植被指数',printed.getvalue())
            self.assertEqual(client.chat.completions.create.call_count,1)


if __name__=="__main__":unittest.main()
