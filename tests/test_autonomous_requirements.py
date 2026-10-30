"""逐项验收与澄清继续的回归；不调用真实API。"""
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

from tests.test_tool_agent import task_fixture,response
from tests.test_autonomous_agent import prepare_prompt,raster


class RequirementCompletionTests(unittest.TestCase):
    def test_band_means_require_every_band_and_actual_values(self):
        from tools.task_acceptance import infer_contract,check_requirements
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'input.tif';values=raster(source)
            contract=infer_contract(['统计每个波段均值，只要CSV，不要图'],['input_1'])
            artifacts={'a':{'path':'means.csv','kind':'table','input_ids':['input_1']}}
            for rows,expected in [([(1,1000)],False),([(i+1,float(band.mean())+100) for i,band in enumerate(values)],False),([(i+1,float(band.mean())) for i,band in enumerate(values)],True)]:
                with (root/'means.csv').open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow(['波段','均值']);writer.writerows(rows)
                result=check_requirements(contract,artifacts,root,inputs={'input_1':source})
                self.assertEqual(result['complete'],expected)

    def test_csv_area_delivery_keeps_intermediate_raster_as_evidence_only(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            plan=dict(session.tool_agent_plan);plan['request']='计算平方米面积，只要CSV，不要图片'
            runtime=AutonomousTools(root,unused.output,plan)
            folder=runtime.folder();path=folder/'mask.tif'
            with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=1,dtype='uint8',crs='EPSG:32632',transform=from_origin(500000,6000000,10,10),nodata=255) as dst:dst.write(np.array([[1,0],[0,0]],dtype='uint8'),1)
            key=runtime.register(path,'raster',classes={'0':'其他','1':'目标'},role='classification');runtime.artifacts[key]['input_ids']=['input_1']
            area=runtime.execute('calculate_area',{'artifact_id':key,'class_codes':[1]})
            self.assertTrue(area['ok'])
            self.assertTrue(runtime.execute('validate_artifacts',{})['ok'])
            result=runtime.execute('finish_task',{'status':'completed','artifact_ids':list(area['artifacts']),'summary':'平方米面积表已生成。','unfulfilled':[]})
            self.assertTrue(result['ok'],result)

    def test_later_explicit_explanation_revision_can_stop_processing(self):
        from tools.task_acceptance import infer_contract
        contract=infer_contract(['帮我计算影像面积','改为只解释面积的计算方法，不需要处理'],['input_1'])
        self.assertEqual(contract['mode'],'answer')
        self.assertEqual(contract['requirements'],[])

    def test_code_attempt_budget_is_enforced(self):
        from tools.autonomous_tools import AutonomousTools
        with task_fixture() as (root,session,_,unused):
            runtime=AutonomousTools(root,unused.output,session.tool_agent_plan);runtime.code_runs=4
            with patch('tools.python_runtime.run') as execute:
                result=runtime.execute('run_python',{'code':'print(1)','input_ids':['input_1'],'artifact_ids':[],'outputs':[{'filename':'a.csv','kind':'table','role':'data','label':'结果'}]})
            self.assertFalse(result['ok']);execute.assert_not_called()

    def test_clarification_preserves_original_goal_and_allows_selected_input(self):
        from core.session import Session
        from tools.autonomous_workflow import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepare_prompt(root);session=Session();session.start()
            session.user_request='统计每个波段均值，只要CSV，不要图片'
            first=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response('request_clarification',{'question':'请先选择影像。','missing':['输入影像']})))))
            run(session,first,'offline',root)
            data=root/'data/sample';data.mkdir(parents=True);raster(data/'input.tif')
            session.selected_inputs=['sample/input.tif'];session.revision_feedback.append('使用刚选择的这张影像。')
            args={'code':'offline','input_ids':['input_1'],'artifact_ids':[],'outputs':[{'filename':'means.csv','kind':'table','role':'data','label':'逐波段均值'}]}
            replies=[response('run_python',args,identity='b1'),response('validate_artifacts',identity='b2'),response('finish_task',{'status':'completed','artifact_ids':['artifact_1'],'summary':'均值表已生成。','unfulfilled':[]},identity='b3')]
            second=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))
            def execute(code,inputs,job,**kwargs):
                output=Path(job)/'output';output.mkdir(parents=True)
                with rasterio.open(inputs['input_1']) as src:values=src.read(masked=True).mean(axis=(1,2))
                with (output/'means.csv').open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow(['波段','均值']);writer.writerows(enumerate(values,1))
                return {'ok':True,'stdout':'','stderr':'','output_dir':str(output)}
            with patch('tools.python_runtime.run',side_effect=execute):result=run(session,second,'offline',root)
            self.assertEqual(session.user_request,'统计每个波段均值，只要CSV，不要图片')
            self.assertEqual(session.autonomous_state['status'],'completed')
            self.assertFalse(session.autonomous_state['awaiting_input'])
            self.assertEqual(len(list((root/session.result_relative_dir/'journal').glob('run_*.jsonl'))),2)


    def test_plain_hotspot_question_requests_temperature_evidence(self):
        from tools.task_acceptance import infer_contract
        contract=infer_contract(['帮我看看哪里比较热'],['input_1'])
        self.assertEqual(contract['mode'],'analysis')
        self.assertEqual(next(r['target'] for r in contract['requirements'] if r['kind']=='map'),'temperature')

    def test_landcover_image_cannot_satisfy_a_temperature_map(self):
        from tools.task_acceptance import infer_contract,check_requirements
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'map.png').write_bytes(b'placeholder')
            contract=infer_contract(['帮我看看哪里比较热'],['input_1'])
            artifacts={'a':{'path':'map.png','kind':'image','role':'preview','producer':'classify_landcover','input_ids':['input_1']}}
            self.assertFalse(check_requirements(contract,artifacts,root)['complete'])

    def test_cancellation_does_not_become_completed_at_the_end(self):
        from core.session import Session
        from core.state_machine import ConversationPhase
        from tools.autonomous_workflow import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepare_prompt(root);session=Session();session.start();session.user_request='NDVI是什么'
            def create(**kwargs):
                session.terminate('用户停止');return response(text='迟到回复')
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=create))))
            result=run(session,client,'offline',root)
            self.assertEqual(result['completion'],'failed')
            self.assertEqual(session.state_machine.get_current_phase(),ConversationPhase.TERMINATED)
            self.assertFalse(session.get_llm_activity()['active'])


    def test_runtime_lookup_uses_fully_qualified_image(self):
        from tools import python_runtime
        payload={'Os':'linux','Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.ursa.runtime':'python-v1'}}}
        with patch.object(python_runtime.shutil,'which',return_value='docker'),patch.object(python_runtime,'docker',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(payload))) as command:
            result=python_runtime.status()
        self.assertTrue(result['available'])
        self.assertIn('docker.io/library/ursa-python-runtime:1',command.call_args.args[0])


if __name__=='__main__':unittest.main()
