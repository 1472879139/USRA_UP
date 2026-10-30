"""对话水体工具链的回归，全部使用临时合成数据与模拟模型。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np
import rasterio

from core.message import Message, MessageType
from core.session import Session
from tools.data_context import load_data_context
from tools.task_contract import build_task_contract, ensure_task_contract
from tools.result_audit import audit_execution_results
from tools.result_delivery import build_result_delivery
from tests.test_benchmarks import raster


def sample(root, folder="water_01"):
    path = raster(root/"data"/folder/"image.tif", np.array([
        [[100, 100], [100, 100]], [[300, 100], [300, 100]], [[100, 100], [100, 100]],
        [[400, 400], [400, 400]], [[100, 300], [100, 300]], [[200, 200], [200, 200]]
    ], dtype="uint16"), ("B2", "B3", "B4", "B8", "B11", "B12"))
    (path.parent/"metadata.json").write_text(json.dumps({"dataset": "合成Sentinel2", "bands": ["B2", "B3", "B4", "B8", "B11", "B12"], "encoding": "反射率DN/10000"}), encoding="utf-8")
    return path


def session_for(root, text="帮我找出水体，告诉我有多少", revisions=None):
    session = Session()
    session.start()
    session.user_request = text
    session.revision_feedback = revisions or ["water_01和water_02"]
    session.data_context = load_data_context(root/"data")
    ensure_task_contract(session)
    return session


class BindingTests(unittest.TestCase):
    def test_unknown_filename_is_not_replaced_by_a_known_file_in_same_folder(self):
        context = {"files": [{"file_name": "suite/water_01/image.tif"}, {"file_name": "other/image.tif"}]}
        contract = build_task_contract("提取data/suite/water_01/missing.tif中的水体", [], context)
        self.assertEqual(contract["inputs"], [])
        context["files"] = context["files"][:1]
        self.assertEqual(build_task_contract("提取data/missing.tif的水体", [], context)["inputs"], [])

    def test_bare_case_aliases_bind_both_inputs_and_revision_replaces(self):
        context = {"files": [{"file_name": "suite/water_01/image.tif"}, {"file_name": "suite/water_02/image.tif"}, {"file_name": "suite/water_010/image.tif"}]}
        contract = build_task_contract("提取水体", ["water_01和water_02"], context)
        self.assertEqual(contract["inputs"], ["suite/water_01/image.tif", "suite/water_02/image.tif"])
        contract = build_task_contract("提取水体", ["water_01和water_02", "改成water_02"], context)
        self.assertEqual(contract["inputs"], ["suite/water_02/image.tif"])

    def test_ambiguous_alias_does_not_silently_select_both(self):
        context = {"files": [{"file_name": "a/water_01/image.tif"}, {"file_name": "b/water_01/image.tif"}]}
        contract = build_task_contract("提取water_01的水体", [], context)
        self.assertEqual(contract["inputs"], [])
        self.assertIn("water_01", contract["input_ambiguities"])


class AuditTests(unittest.TestCase):
    def test_declared_raster_cannot_borrow_another_scene_with_matching_counts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raster(root/"one_water_mask.tif", np.array([[0, 1]], dtype="uint8"), nodata=255)
            raster(root/"two_water_mask.tif", np.array([[1, 1]], dtype="uint8"), nodata=255)
            (root/"stats.csv").write_text("类别编号,类别,像元数,占比_%,分类栅格\n0,非水体,1,50,two_water_mask.tif\n1,水体,1,50,two_water_mask.tif\n", encoding="utf-8")
            audit = audit_execution_results(root, ["one_water_mask.tif", "two_water_mask.tif", "stats.csv"])
            self.assertEqual(audit["status"], "failed")

    def test_water_mask_is_checked_and_declared_unknown_class_is_counted(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raster(root/"final_water_mask.tif", np.array([[0, 1], [1, 255]], dtype="uint8"), nodata=255)
            table = root/"stats.csv"
            table.write_text("类别编号,类别,像元数,占比_%,分类栅格\n0,非水体,1,25,final_water_mask.tif\n1,水体,2,50,final_water_mask.tif\n255,待核实,1,25,final_water_mask.tif\n", encoding="utf-8-sig")
            audit = audit_execution_results(root, ["stats.csv", "final_water_mask.tif"])
            self.assertEqual(audit["errors"], [])
            self.assertFalse(any("没有匹配" in warning for warning in audit["warnings"]))
            table.write_text(table.read_text(encoding="utf-8-sig").replace("1,水体,2,50", "1,水体,1,25"), encoding="utf-8-sig")
            self.assertEqual(audit_execution_results(root, ["stats.csv", "final_water_mask.tif"])["status"], "failed")


class ToolChainTests(unittest.TestCase):
    def test_gui_pipeline_uses_same_tool_and_manager_preserves_each_scene(self):
        from agents.manager_agent import ManagerAgent
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from core.state_machine import ConversationPhase
        from tests.test_ui_updates import make_view
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample(root); sample(root, "water_02")
            view = make_view(); view.session = session_for(root)
            view.config = {"executor_max_retries": 2}
            agents = [kind(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
                      for kind in (ManagerAgent, ScientistAgent, EngineerAgent, ExecutorAgent, SupervisorAgent)]
            manager, scientist, engineer, executor, supervisor = agents
            for agent in agents:
                agent.session = view.session
            executor._project_root = supervisor._project_root = str(root)
            manager._call_llm = Mock(return_value="已逐幅完成候选水体处理，精度见独立评价。")
            scientist._call_llm = Mock(side_effect=AssertionError("不应重新设计默认方法"))
            engineer._call_llm_with_memory = Mock(side_effect=AssertionError("不应重写固定工具"))
            supervisor.select_relevant_files = Mock(side_effect=AssertionError("输入已绑定，不应重新猜文件"))
            with patch("tools.io_kit._get_project_root", return_value=str(root)):
                view._run_analysis_pipeline(manager, scientist, engineer, executor, supervisor, view.session.state_machine)
            self.assertEqual(view.session.state_machine.get_current_phase(), ConversationPhase.COMPLETED, view.session.get_terminate_reason())
            self.assertEqual(len(view.session.result_delivery["scene_results"]), 2)
            self.assertEqual(manager._call_llm.call_count, 1)
            self.assertIn("独立评价", manager._call_llm.call_args.args[0])

    def test_metadata_change_invalidates_the_data_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = sample(root)
            first = load_data_context(root/"data")
            metadata = image.parent/"metadata.json"
            metadata.write_text(metadata.read_text(encoding="utf-8") + " ", encoding="utf-8")
            second = load_data_context(root/"data", previous=first)
            self.assertNotEqual(first["signature"], second["signature"])

    def test_changed_input_is_rejected_before_execution(self):
        from tools.water_workflow import select_water_tool, run_water_workflow
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = sample(root); sample(root, "water_02")
            plan = select_water_tool(session_for(root))
            with rasterio.open(image, "r+") as src:
                src.write(np.zeros((2, 2), dtype="uint16"), 1)
            with patch("tools.water_workflow.Path.cwd", return_value=root):
                with self.assertRaisesRegex(ValueError, "已变化"):
                    run_water_workflow(plan, root/"results/new_task")

    def test_engineer_rejects_forged_tool_plan_without_model_call(self):
        from agents.engineer_agent import EngineerAgent
        engineer = object.__new__(EngineerAgent)
        engineer.name, engineer.session = "Engineer", Session()
        engineer.session.tool_plan = {"name": "water_mndwi_zero", "version": "mndwi-zero-v1", "inputs": []}
        engineer._call_llm_with_memory = Mock(side_effect=AssertionError("不得回退到模型改写"))
        result = engineer.handle_message(Message(sender="Scientist", receiver="Engineer", content="伪造计划", metadata={"tool_request": {"name": "other"}}))
        self.assertEqual(result.msg_type, MessageType.ERROR)
        engineer._call_llm_with_memory.assert_not_called()

    def test_fixed_tool_failure_does_not_start_model_rewrite(self):
        from core.execution_cycle import run_execution_cycle
        session = Session(); session.start()
        session.tool_plan = {"name": "water_mndwi_zero"}
        executor = Mock()
        executor.handle_message.return_value = Message(sender="Executor", receiver="Engineer", msg_type=MessageType.ERROR, content="输入版本改变")
        engineer = Mock()
        result = run_execution_cycle(session, engineer, executor, Mock(), Message(sender="Engineer", receiver="Executor", msg_type=MessageType.CODE, content="print('fixed')"))
        self.assertIsNone(result)
        self.assertEqual(executor.handle_message.call_count, 1)
        engineer.handle_message.assert_not_called()

    def test_two_scene_plan_engineer_executor_and_delivery(self):
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from core.execution_cycle import run_execution_cycle
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample(root); sample(root, "water_02")
            session = session_for(root)
            scientist = object.__new__(ScientistAgent)
            scientist.name, scientist.session = "Scientist", session
            scientist._call_llm = Mock(side_effect=AssertionError("固定规划不应调用模型"))
            plan = scientist.handle_message(Message(sender="Manager", receiver="Scientist", content="提取水体"))
            self.assertEqual(plan.metadata["tool_request"]["name"], "water_mndwi_zero")
            engineer = object.__new__(EngineerAgent)
            engineer.name, engineer.session = "Engineer", session
            engineer._call_llm_with_memory = Mock(side_effect=AssertionError("固定工具包装不应调用模型"))
            code = engineer.handle_message(plan)
            self.assertIn("URSA_RESULTS_DIR", code.content)
            self.assertNotIn("answers", code.content)
            executor = object.__new__(ExecutorAgent)
            executor.name, executor.session, executor._project_root, executor._code_timeout = "Executor", session, str(root), 60
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = session, str(root)
            # 执行器必须自行提供模块路径，测试不额外注入PYTHONPATH。
            response = run_execution_cycle(session, engineer, executor, supervisor, code, max_attempts=1)
            self.assertIsNotNone(response, session.get_terminate_reason())
            delivery = build_result_delivery(session, root)
            self.assertEqual(len(delivery["scene_results"]), 2)
            self.assertTrue(all(row["water_pixels"] == 2 for row in delivery["scene_results"]))
            self.assertIn("water_01", "".join(delivery["findings"]))
            self.assertIn("water_02", "".join(delivery["findings"]))
            self.assertFalse(any("没有匹配" in warning for warning in session.result_audit["warnings"]))
            scientist._call_llm.assert_not_called()
            engineer._call_llm_with_memory.assert_not_called()

    def test_explicit_method_is_not_replaced(self):
        from tools.water_workflow import select_water_tool
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample(root); sample(root, "water_02")
            session = session_for(root, "使用双指数NDWI和MNDWI交集提取水体")
            self.assertIsNone(select_water_tool(session))

    def test_unknown_sensor_metadata_does_not_guess_b11_semantics(self):
        from tools.water_workflow import select_water_tool
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample(root); sample(root, "water_02")
            (root/"data/water_01/metadata.json").write_text("{}")
            self.assertIsNone(select_water_tool(session_for(root)))


if __name__ == "__main__":
    unittest.main()
