"""温度工具与跨任务交卷回归：临时合成数据，不调用模型。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np
import rasterio

from tests.test_benchmarks import raster
from tests.test_water_agent import session_for as water_session_for, sample as water_sample
from tools.artifact_paths import file_hash


def session_for(root, text, revisions=None):
    return water_session_for(root, text, revisions=[''])


def sample(root):
    from tools.lst_tool import BANDS
    temperature = np.array([[20, 25], [30, 35]], dtype='float64')
    blackbody = 774.8853 / np.expm1(1321.0789 / (temperature + 273.15))
    arrays = np.stack([.8 * (.97 * blackbody + .03 * 1.0) + 2,
                       np.full((2, 2), 2), np.ones((2, 2)),
                       np.full((2, 2), .8), np.full((2, 2), .97), np.full((2, 2), 64)])
    image = raster(root/'data/lst_01/image.tif', arrays.astype('float32'), BANDS, nodata=np.nan)
    metadata = {'platform': 'landsat-8', 'bands_in_order': list(BANDS),
                'band_units': ['watt/steradian/square_meter/micrometer'] * 3 + ['flag', 'emissivity coefficient', 'bit index'],
                'encoding': '浮点栅格已应用每个资产的scale/offset，为物理量，禁止重复缩放；QA_PIXEL保持整数位标志；缺测NaN',
                'thermal_constants_from_scene_MTL': {'K1_BAND_10': 774.8853, 'K2_BAND_10': 1321.0789}}
    (image.parent/'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    return image, temperature


def completed(root):
    from agents.manager_agent import ManagerAgent
    from agents.scientist_agent import ScientistAgent
    from agents.engineer_agent import EngineerAgent
    from agents.executor_agent import ExecutorAgent
    from agents.supervisor_agent import SupervisorAgent
    from tests.test_ui_updates import make_view
    view = make_view(); view.session = session_for(root, '看看lst_01哪里比较热')
    view.config = {'executor_max_retries': 2}
    agents = [kind(model='test', api_key='test', base_url='https://example.invalid', system_prompt='测试')
              for kind in (ManagerAgent, ScientistAgent, EngineerAgent, ExecutorAgent, SupervisorAgent)]
    for agent in agents:
        agent.session = view.session
    agents[0]._call_llm = Mock(return_value='地表温度结果已生成，误差尚未独立验证。')
    agents[1]._call_llm = Mock(side_effect=AssertionError('固定温度不应调用规划模型'))
    agents[2]._call_llm_with_memory = Mock(side_effect=AssertionError('固定温度不应重写方法'))
    agents[3]._project_root = agents[4]._project_root = str(root)
    agents[4].select_relevant_files = Mock(side_effect=AssertionError('输入必须锁定'))
    with patch('tools.io_kit._get_project_root', return_value=str(root)):
        view._run_analysis_pipeline(*agents, view.session.state_machine)
    return view.session


class LSTTests(unittest.TestCase):
    def test_real_role_chain_then_freeze_and_grade_never_changes_answer(self):
        from tools.submission import freeze_submission, verify_submission
        from benchmarks.assessment import grade_submission
        from core.state_machine import ConversationPhase
        with TemporaryDirectory() as tmp:
            root = Path(tmp); image, expected = sample(root)
            (root/'benchmark.local.json').write_text('故意不可解析，生产端不应读取', encoding='utf-8')
            with patch('benchmarks.catalog.load_catalog', side_effect=AssertionError('生产端不得看答案')):
                session = completed(root)
            self.assertEqual(session.state_machine.get_current_phase(), ConversationPhase.COMPLETED, session.get_terminate_reason())
            self.assertIn('地表温度', session.result_delivery['headline'])
            self.assertNotIn('水体', session.result_delivery['headline'])
            self.assertNotIn('scores', session.result_audit['scenes'][0]['evaluation'])
            before = json.dumps(session.result_delivery, ensure_ascii=False)
            receipt = freeze_submission(session, root); folder = root/receipt['directory']
            sealed = verify_submission(folder, receipt['sha256'])
            self.assertEqual(sealed['scenes'][0]['task_type'], 'lst')
            ref = raster(root/'answers/temp.tif', expected.astype('float32'), nodata=np.nan)
            mask = root/'answers/clear.npy'; np.save(mask, np.ones((2, 2), dtype=bool))
            (root/'benchmark.local.json').write_text(json.dumps({'reference_root': 'answers'}))
            case = {'id': 'synthetic_lst', 'task': 'regression', 'assessment_enabled': True,
                    'inputs': [{'sha256': file_hash(image)}], 'reference': {'path': 'temp.tif', 'sha256': file_hash(ref)},
                    'evaluation_mask': {'path': 'clear.npy', 'sha256': file_hash(mask)}, 'reference_unit': 'degC',
                    'source': 'https://example.invalid/synthetic', 'limitation': '合成验证，不代表地面精度'}
            with patch('benchmarks.catalog.load_catalog', return_value=[case]):
                report = grade_submission(folder, receipt['sha256'], root)
                self.assertLess(report['scenes'][0]['scores']['rmse_celsius'], .0001)
                mask.write_bytes(b'changed')
                rejected = grade_submission(folder, receipt['sha256'], root)
                self.assertEqual(rejected['status'], 'unscored')
                self.assertIn('哈希', rejected['scenes'][0]['reason'])
            self.assertEqual(json.dumps(session.result_delivery, ensure_ascii=False), before)

    def test_temperature_audit_rejects_tampered_csv_and_pixels(self):
        from tools.lst_workflow import attach_lst_audit
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root); session = completed(root)
            folder = root/session.result_relative_dir/'input_01'
            csv = folder/'temperature_statistics.csv'; original = csv.read_bytes(); csv.write_bytes(original.replace(b'27.', b'99.'))
            with self.assertRaisesRegex(ValueError, 'CSV'):
                attach_lst_audit(session, root)
            csv.write_bytes(original)
            with rasterio.open(folder/'final_lst_celsius.tif', 'r+') as src:
                src.write(np.zeros((2, 2), dtype='float32'), 1)
            with self.assertRaisesRegex(ValueError, '哈希'):
                attach_lst_audit(session, root)

    def test_temperature_input_change_and_forged_plan_are_rejected(self):
        from tools.lst_workflow import select_lst_tool, run_lst_workflow
        from tools.installed_workflow import tool_code_message
        from core.message import MessageType
        with TemporaryDirectory() as tmp:
            root = Path(tmp); image, _ = sample(root); session = session_for(root, '计算lst_01地表温度')
            session.tool_plan = select_lst_tool(session)
            self.assertEqual(tool_code_message(session, {**session.tool_plan, 'version': 'other'}).msg_type, MessageType.ERROR)
            sidecar = image.parent/'metadata.json'; sidecar.write_text(sidecar.read_text()+' ')
            with patch('tools.lst_workflow.Path.cwd', return_value=root):
                with self.assertRaisesRegex(ValueError, '已变化'):
                    run_lst_workflow(session.tool_plan, root/session.result_relative_dir)

    def test_radiative_transfer_recovers_independent_known_temperature(self):
        from tools.lst_tool import calculate
        with TemporaryDirectory() as tmp:
            image, expected = sample(Path(tmp))
            actual, method, _ = calculate(image)
            np.testing.assert_allclose(actual, expected, atol=.0001)
            self.assertEqual(method['unit'], 'degC')
            self.assertEqual(method['valid_pixels'], 4)
            self.assertAlmostEqual(method['mean_celsius'], 27.5, places=4)

    def test_quality_bits_and_physical_invalids_remain_missing(self):
        from tools.lst_tool import calculate
        with TemporaryDirectory() as tmp:
            image, _ = sample(Path(tmp))
            with rasterio.open(image, 'r+') as src:
                qa = src.read(6); qa[0, 0] = 8; qa[0, 1] = 192; src.write(qa, 6)
                tau = src.read(4); tau[1, 0] = 0; src.write(tau, 4)
                radiance = src.read(1); radiance[1, 1] = np.nan; src.write(radiance, 1)
            actual, method, _ = calculate(image)
            self.assertEqual(np.isfinite(actual).sum(), 1)
            self.assertTrue(np.isfinite(actual[0, 1]), '水面质量位应保留')
            self.assertEqual(method['excluded'], {'unavailable': 1, 'qa': 1, 'physics': 1})

    def test_each_quality_bit_and_empty_scene_stop(self):
        from tools.lst_tool import calculate
        with TemporaryDirectory() as tmp:
            image, _ = sample(Path(tmp))
            for bit in range(6):
                with rasterio.open(image, 'r+') as src:
                    src.write(np.full((2, 2), 1 << bit, dtype='float32'), 6)
                with self.assertRaisesRegex(ValueError, '没有可用'):
                    calculate(image)

    def test_ambiguous_units_scales_and_constants_rejected(self):
        from tools.lst_tool import inspect_input
        with TemporaryDirectory() as tmp:
            image, _ = sample(Path(tmp)); sidecar = image.parent/'metadata.json'
            original = json.loads(sidecar.read_text())
            for key, value in [('encoding', '原始DN'), ('platform', 'landsat-7'),
                               ('thermal_constants_from_scene_MTL', {}), ('band_units', ['K'] * 6)]:
                modified = {**original, key: value}; sidecar.write_text(json.dumps(modified))
                with self.assertRaises(ValueError):
                    inspect_input(image)
            sidecar.write_text(json.dumps(original))
            with rasterio.open(image, 'r+') as src:
                src.scales = (.001,) * 6
            with self.assertRaisesRegex(ValueError, '缩放'):
                inspect_input(image)

    def test_temperature_unsupported_inputs_never_fall_back_to_generated_code(self):
        from agents.scientist_agent import ScientistAgent
        with TemporaryDirectory() as tmp:
            root = Path(tmp); water_sample(root)
            agent = object.__new__(ScientistAgent); agent.name = 'Scientist'
            agent.session = session_for(root, '帮我看看哪里比较热', revisions=[])
            agent._call_llm = Mock(side_effect=AssertionError('不能生成假温度'))
            from core.message import Message, MessageType
            result = agent.handle_message(Message(sender='Manager', receiver='Scientist', content='确认'))
            self.assertEqual(result.msg_type, MessageType.ERROR)
            self.assertIn('温度', result.content)

    def test_dispatch_temperature_and_water_without_changing_water_default(self):
        from tools.installed_workflow import select_installed_tool
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root); water_sample(root)
            temperature = session_for(root, '看看lst_01哪里比较热', revisions=[])
            water = session_for(root, '找出water_01中的水体', revisions=[])
            self.assertEqual(select_installed_tool(temperature)['name'], 'lst_rte')
            self.assertEqual(select_installed_tool(water)['name'], 'water_mndwi_zero')

    def test_explicit_other_demands_are_not_silently_replaced(self):
        from tools.installed_workflow import select_installed_tool
        with TemporaryDirectory() as tmp:
            root = Path(tmp); sample(root)
            for request in ['lst_01计算气温', 'lst_01计算温度变化', 'lst_01采用深度学习反演温度', 'lst_01只要CSV温度表']:
                self.assertIsNone(select_installed_tool(session_for(root, request, revisions=[])))

    def test_continuous_score_keeps_missing_coverage_and_checks_grid_units(self):
        from benchmarks.lst_metrics import score_temperature
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = raster(root/'ref.tif', np.array([[20, 25], [30, 35]], dtype='float32'), nodata=np.nan)
            pred = raster(root/'pred.tif', np.array([[21, 24], [np.nan, 37]], dtype='float32'), nodata=np.nan)
            with rasterio.open(pred, 'r+') as src:
                src.set_band_unit(1, 'degC')
            with rasterio.open(ref, 'r+') as src:
                src.set_band_unit(1, 'degC')
            mask = root/'clear.npy'; np.save(mask, np.ones((2, 2), dtype=bool))
            score = score_temperature(pred, ref, mask)
            self.assertEqual(score['missing_prediction_pixels'], 1)
            self.assertEqual(score['coverage'], .75)
            self.assertAlmostEqual(score['rmse_celsius'], 2 ** .5)
            self.assertEqual(score['status'], 'incomplete')
            with rasterio.open(pred, 'r+') as src:
                src.set_band_unit(1, 'K')
            with self.assertRaisesRegex(ValueError, '摄氏'):
                score_temperature(pred, ref, mask)


if __name__ == '__main__':
    unittest.main()
