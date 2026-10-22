"""Offline fixtures for pre-Manager data inspection and shared context."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.transform import from_origin

from agents.manager_agent import ManagerAgent
from agents.scientist_agent import ScientistAgent
from agents.engineer_agent import EngineerAgent
from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from tests.test_ui_updates import make_view


def write_fixture(path, value=100):
    with rasterio.open(path, "w", driver="GTiff", width=6, height=4, count=10,
                       dtype="uint16", crs="EPSG:32650",
                       transform=from_origin(447020, 4425040, 10, 10)) as dst:
        arr = np.full((10, 4, 6), value, dtype="uint16")
        arr[:, :, :2] = 0
        dst.write(arr)
        dst.descriptions = ("B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12")


class DataContextTests(unittest.TestCase):
    def test_manager_has_actual_data_before_first_model_call(self):
        manager = object.__new__(ManagerAgent)
        manager.name = "Manager"
        manager.session = Session()
        manager.session.start()
        prompts = []
        manager._call_llm = lambda prompt: prompts.append(prompt) or "请确认"
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            write_fixture(data / "Sentinel2_test.tif")
            with patch("tools.io_kit._get_project_root", return_value=tmp):
                manager.handle_message(Message(sender="User", receiver="Manager", content="分析东城区"))

        self.assertIn("B8A", prompts[0])
        self.assertIn("EPSG:32650", prompts[0])
        self.assertTrue(getattr(manager.session, "data_context", {}).get("files"))

    def test_snapshot_distinguishes_zero_values_from_declared_nodata(self):
        from tools.data_context import load_data_context, render_data_context
        with TemporaryDirectory() as tmp:
            write_fixture(Path(tmp) / "Sentinel2_test.tif")
            context = load_data_context(tmp)
            item = context["files"][0]
            self.assertEqual(item["band_count"], 10)
            self.assertIn("B8A", item["band_names"])
            self.assertIsNone(item["nodata"])
            self.assertEqual(item["sample"]["mask_valid_pixels"], 24)
            self.assertEqual(item["sample"]["all_band_zero_pixels"], 8)
            self.assertEqual(item["sample"]["band_stats"][0]["max"], 100)
            self.assertIn("不能", render_data_context(context))

    def test_changed_file_refreshes_cached_sample(self):
        from tools.data_context import load_data_context
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.tif"
            write_fixture(path, 100)
            first = load_data_context(tmp)
            with rasterio.open(path, "r+") as dst:
                dst.write(np.full((4, 6), 250, dtype="uint16"), 1)
            second = load_data_context(tmp, previous=first)
            self.assertEqual(second["files"][0]["sample"]["band_stats"][0]["max"], 250)
            self.assertNotEqual(first["signature"], second["signature"])

    def test_file_replaced_while_waiting_for_approval_requires_new_confirmation(self):
        manager = object.__new__(ManagerAgent)
        manager.name, manager.session = "Manager", Session()
        manager.session.start()
        prompts = []
        manager._call_llm = lambda prompt: prompts.append(prompt) or "新数据确认计划"
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            path = data / "input.tif"
            write_fixture(path, 100)
            with patch("tools.io_kit._get_project_root", return_value=tmp):
                manager.handle_message(Message(sender="User", receiver="Manager", content="全部波段分析"))
                with rasterio.open(path, "r+") as dst:
                    dst.write(np.full((4, 6), 250, dtype="uint16"), 1)
                response = manager.handle_message(Message(sender="User", receiver="Manager", content="approve", msg_type=MessageType.CONTROL))
                self.assertEqual(response.receiver, "User")
                self.assertEqual(manager.session.state_machine.get_current_phase(), ConversationPhase.MANAGER_CONFIRM)
                self.assertEqual(manager.session.confirmed_task, "")
                self.assertIn("250", prompts[-1])
                self.assertEqual(manager.session.user_request, "全部波段分析")
                response = manager.handle_message(Message(sender="User", receiver="Manager", content="approve", msg_type=MessageType.CONTROL))
                self.assertEqual(response.receiver, "Scientist")
                self.assertEqual(len(prompts), 2)

    def test_gui_does_not_launch_pipeline_when_manager_requests_reconfirmation(self):
        view = make_view()
        view.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
        view.agents = {name: object() for name in ("user", "scientist", "engineer", "executor", "supervisor")}
        view.agents["manager"] = SimpleNamespace(handle_message=lambda message: Message(sender="Manager", receiver="User", content="输入已变更，请重新确认"))
        calls = []
        view._run_analysis_pipeline = lambda *args: calls.append(args)
        view._process_flow("approve")
        self.assertEqual(calls, [])
        self.assertTrue(any(event["type"] == "message" and event["data"].content == "输入已变更，请重新确认" for event in view.ui_queue.queue))

    def test_unreadable_file_is_reported_without_inventing_metadata(self):
        from tools.data_context import load_data_context, render_data_context
        with TemporaryDirectory() as tmp:
            # A zero-byte TIFF simulates an interrupted local file copy.
            (Path(tmp) / "broken.tif").touch()
            context = load_data_context(tmp)
            self.assertFalse(context["files"][0]["readable"])
            self.assertIn("读取失败", render_data_context(context))

    def test_nan_nodata_does_not_break_context_rendering(self):
        from tools.data_context import load_data_context, render_data_context
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "float.tif"
            with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1,
                               dtype="float32", nodata=float("nan"),
                               transform=from_origin(0, 2, 1, 1)) as dst:
                dst.write(np.array([[np.nan, 1], [2, 3]], dtype="float32"), 1)
            self.assertIn("NaN", render_data_context(load_data_context(tmp)))

    def test_each_band_statistics_respects_its_own_nodata_mask(self):
        from tools.data_context import load_data_context
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "masked.tif"
            with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=2,
                               dtype="int16", nodata=-9999,
                               transform=from_origin(0, 2, 1, 1)) as dst:
                dst.write(np.array([[-9999, 1], [2, 3]], dtype="int16"), 1)
                dst.write(np.full((2, 2), 10, dtype="int16"), 2)
            item = load_data_context(tmp)["files"][0]
            self.assertEqual(item["sample"]["mask_valid_pixels"], 4)
            self.assertEqual(item["sample"]["band_stats"][0]["min"], 1)

    def test_gui_two_revisions_preserve_user_messages_and_reach_real_scientist(self):
        view = make_view()
        view._running = False
        view.agents = {}
        manager = object.__new__(ManagerAgent)
        manager.name = "Manager"
        manager._call_llm = lambda prompt: "计划确认摘要"
        scientist = object.__new__(ScientistAgent)
        scientist.name = "Scientist"
        captured = []

        def scientist_call(prompt, **kwargs):
            captured.append(prompt)
            view.session.terminate("offline test stops before code generation")
            return "分析计划"

        scientist._call_llm = scientist_call
        agents = {
            "manager": manager, "scientist": scientist,
            "user": SimpleNamespace(submit_request=lambda text: Message(sender="User", receiver="Manager", content=text)),
            "supervisor": SimpleNamespace(select_relevant_files=lambda *args: {"relevant_files": []}),
            "engineer": object(), "executor": object(),
        }
        view._create_agents = lambda: agents

        def setup_session():
            view.session = Session()
            view.session.start()
            view.session.register_agent(manager)
            view.session.register_agent(scientist)

        view._setup_session = setup_session
        messages = ["东城区四类地物，全部波段原值，输出 JPG", "禁止指数、纹理和形状特征", "增加面积占比统计", "approve"]
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            write_fixture(data / "Sentinel2_test.tif")
            with patch("tools.io_kit._get_project_root", return_value=tmp):
                for text in messages:
                    self.assertTrue(view.start_analysis(text)["success"])
                    view._thread.join(timeout=10)
                    self.assertFalse(view._thread.is_alive())
        self.assertEqual(len(captured), 1)
        for text in messages[:-1]:
            self.assertIn(text, captured[0])
        self.assertIn("B8A", captured[0])
        events = view.get_updates()["updates"]
        self.assertEqual([event["content"] for event in events if event.get("sender") == "User"], messages)

    def test_manager_does_not_resend_previous_data_snapshots_to_model(self):
        manager = ManagerAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
        manager._conversation_history = [{"role": "user", "content": "obsolete data snapshot"}]
        requests = []
        manager._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None),
        )))
        manager._call_llm("current full request and data snapshot")
        self.assertNotIn("obsolete data snapshot", str(requests[0]["messages"]))
        self.assertEqual(manager._conversation_history[0]["content"], "obsolete data snapshot")

    def test_scientist_and_engineer_receive_same_snapshot_and_original_constraints(self):
        session = Session()
        session.start()
        session.user_request = "全部波段原值分类，输出 JPG"
        session.revision_feedback = ["禁止指数、纹理和形状特征"]
        session.confirmed_task = session.user_request + "；" + session.revision_feedback[0]
        session.data_context = {"files": [{"file_name": "actual.tif", "band_count": 10,
                                         "band_names": ["B8A"], "crs": "EPSG:32650",
                                         "readable": True}], "warnings": []}
        captured = {}
        scientist = object.__new__(ScientistAgent)
        scientist.name, scientist.session = "Scientist", session
        scientist._call_llm = lambda prompt, **kw: captured.setdefault("scientist", prompt) and "分析计划"
        engineer = object.__new__(EngineerAgent)
        engineer.name, engineer.session = "Engineer", session
        engineer._call_llm_with_memory = lambda prompt: captured.setdefault("engineer", prompt) and "print('ok')"
        engineer._sanitize_response = lambda text: text

        plan = scientist.handle_message(Message(sender="Manager", receiver="Scientist", content=session.confirmed_task))
        engineer.handle_message(plan)
        for prompt in captured.values():
            self.assertIn("B8A", prompt)
            self.assertIn("EPSG:32650", prompt)
            self.assertIn("禁止指数、纹理和形状特征", prompt)


if __name__ == "__main__":
    unittest.main()
