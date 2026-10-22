"""面向普通用户的任务约定与结果交付，全部使用临时数据和模拟模型。"""
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from agents.manager_agent import ManagerAgent
from agents.scientist_agent import ScientistAgent
from tools.task_contract import build_task_contract, assess_capability
from tools.result_delivery import build_result_delivery
from tests.test_ui_updates import make_view


def context():
    return {"files": [
        {"file_name": "pair/before.tif", "readable": True, "band_count": 3,
         "band_names": ["Red", "Green", "Blue"], "width": 256, "height": 256},
        {"file_name": "pair/after.tif", "readable": True, "band_count": 3,
         "band_names": ["Red", "Green", "Blue"], "width": 256, "height": 256},
        {"file_name": "other/thermal.tif", "readable": True, "band_count": 1,
         "band_names": ["Thermal radiance"]},
    ]}


class TaskContractTests(unittest.TestCase):
    def test_selected_inputs_exclude_unrelated_thermal_data(self):
        task = build_task_contract("看看哪里热", [], context(), ["pair/before.tif"])
        check = assess_capability(task, context())
        self.assertEqual(task["task_type"], "lst")
        self.assertEqual(task["inputs"], ["pair/before.tif"])
        self.assertEqual(check["status"], "blocked")
        self.assertIn("测温", "".join(check["reasons"]))

    def test_plain_temperature_question_uses_same_gate(self):
        task = build_task_contract("帮我看看这张图哪里最热", [], context(), ["pair/before.tif"])
        self.assertEqual(task["task_type"], "lst")
        self.assertEqual(assess_capability(task, context())["status"], "blocked")

    def test_explicit_folder_resolves_only_its_pair(self):
        task = build_task_contract("找出 data/pair 中 before.tif 到 after.tif 的建筑变化", [], context())
        self.assertEqual(set(task["inputs"]), {"pair/before.tif", "pair/after.tif"})
        self.assertEqual(task["task_type"], "change")
        self.assertIn("建筑", task["targets"])

    def test_revisions_preserve_raw_goal_and_latest_task(self):
        task = build_task_contract("识别建筑和道路", ["改成比较建筑变化，输出PNG"], context())
        self.assertEqual(task["original_request"], "识别建筑和道路")
        self.assertEqual(task["task_type"], "change")
        self.assertEqual(task["revisions"], ["改成比较建筑变化，输出PNG"])

    def test_single_selected_image_cannot_become_change_pair(self):
        task = build_task_contract("找出建筑变化", [], context(), ["pair/before.tif"])
        self.assertEqual(assess_capability(task, context())["status"], "blocked")

    def test_cancelled_temperature_request_does_not_block_classification(self):
        task = build_task_contract("不要地表温度，只识别建筑", [], context(), ["pair/before.tif"])
        self.assertEqual(task["task_type"], "segmentation")

    def test_plain_revision_can_replace_temperature_goal(self):
        task = build_task_contract("看看哪里热", ["不要温度了，我想看看水体在哪里"], context(), ["pair/before.tif"])
        self.assertEqual(task["task_type"], "segmentation")
        self.assertNotEqual(assess_capability(task, context())["status"], "blocked")

    def test_data_only_request_cannot_start_arbitrary_analysis(self):
        request = "请查看我选择的数据，推荐两三个可以进行的分析，先不要执行。"
        task = build_task_contract(request, [], context(), ["pair/before.tif"])
        self.assertTrue(task["needs_goal"])
        self.assertEqual(assess_capability(task, context())["status"], "blocked")
        task = build_task_contract(request, ["我想识别建筑"], context(), ["pair/before.tif"])
        self.assertFalse(task["needs_goal"])

    def test_unknown_temperature_encoding_requires_review(self):
        task = build_task_contract("地表温度", [], context(), ["other/thermal.tif"])
        check = assess_capability(task, context())
        self.assertNotEqual(check["status"], "blocked")
        self.assertIn("单位", "".join(check["reasons"]))

    def test_rgb_can_support_building_models_but_not_prove_semantics(self):
        task = build_task_contract("识别建筑", [], context(), ["pair/before.tif"])
        check = assess_capability(task, context())
        self.assertNotEqual(check["status"], "blocked")
        self.assertIn("建筑", "".join(check["reasons"]))
        self.assertFalse(check["semantic_verified"])

    def test_manager_confirms_contract_without_rewriting_handoff(self):
        session = Session()
        session.start()
        session.user_request = "找出建筑变化"
        session.data_context = context()
        manager = object.__new__(ManagerAgent)
        manager.session, manager.name = session, "Manager"
        manager._call_llm = lambda *a: self.fail("确认不应调用模型")
        with patch("agents.manager_agent.load_data_context", return_value=context()):
            response = manager.handle_message(Message(sender="User", receiver="Manager", content="approve", msg_type=MessageType.CONTROL))
        self.assertEqual(response.content, session.user_request)
        self.assertEqual(session.task_contract["original_request"], session.user_request)
        self.assertTrue(any(e.event_type == "task_contract" for e in session.tracer.get_events()))

    def test_scientist_blocks_rgb_temperature_before_model_call(self):
        session = Session()
        session.start()
        session.user_request = "看看哪里热"
        session.selected_inputs = ["pair/before.tif"]
        session.data_context = context()
        scientist = object.__new__(ScientistAgent)
        scientist.session, scientist.name = session, "Scientist"
        scientist._call_llm = lambda *a, **k: self.fail("条件不足不应发起模型请求")
        response = scientist.handle_message(Message(sender="Manager", receiver="Scientist", content=session.user_request))
        self.assertEqual(response.msg_type, MessageType.ERROR)
        self.assertEqual(session.task_capability["status"], "blocked")


class PlainDeliveryTests(unittest.TestCase):
    def fixture(self, root):
        session = Session()
        session.start()
        session.user_request = "找出建筑变化，生成变化图"
        session.task_contract = build_task_contract(session.user_request, [], context(), ["pair/before.tif", "pair/after.tif"])
        session.task_capability = assess_capability(session.task_contract, context())
        folder = Path(root, session.result_relative_dir)
        folder.mkdir(parents=True)
        Image.new("RGB", (16, 16), "white").save(folder / "变化图.png")
        session.result_audit = {"status": "needs_review", "errors": [], "warnings": ["类别语义待复核"],
                                "statistics": [], "files": [session.result_subdir + "/变化图.png"], "facts": "数值检查通过，语义待复核"}
        return session

    def test_readable_outputs_are_not_reported_as_verified_buildings(self):
        with TemporaryDirectory() as root:
            session = self.fixture(root)
            result = build_result_delivery(session, root, report="建筑变化已全部准确识别，可信度99%")
            self.assertEqual(result["completion"], "candidate")
            self.assertIn("可能", result["headline"])
            self.assertIn("独立", result["verification"])
            self.assertNotIn("99%", json.dumps({k: v for k, v in result.items() if k != "professional_report"}, ensure_ascii=False))
            self.assertEqual(len(result["previews"]), 1)

    def test_failed_task_has_no_success_preview_and_no_old_artifacts(self):
        with TemporaryDirectory() as root:
            session = self.fixture(root)
            session.result_audit["status"] = "failed"
            session.result_audit["errors"] = ["统计矛盾"]
            result = build_result_delivery(session, root)
            self.assertEqual(result["completion"], "failed")
            self.assertEqual(result["previews"], [])

    def test_outside_task_and_symlink_are_not_linked(self):
        with TemporaryDirectory() as root:
            session = self.fixture(root)
            session.result_audit["files"] += ["../config.json", "old_task/map.png"]
            result = build_result_delivery(session, root)
            self.assertEqual(len(result["files"]), 1)
            self.assertTrue(result["files"][0]["url"].startswith("/api/result_file?"))

    def test_snapshot_replays_card_and_new_chat_does_not_keep_it(self):
        with TemporaryDirectory() as root:
            view = make_view()
            view.session = self.fixture(root)
            view.session.result_delivery = build_result_delivery(view.session, root)
            view.session.state_machine.force_transition(ConversationPhase.COMPLETED)
            view._running = False
            first = view.get_updates()
            replay = view.get_updates(since=first["cursor"])
            self.assertEqual(first["result_delivery"], replay["result_delivery"])
            view.session = None
            self.assertIsNone(view.get_updates()["result_delivery"])

    def test_result_report_purpose_survives_event_replay(self):
        view = make_view()
        view.ui_queue.put({"type": "message", "data": Message(sender="Manager", receiver="User", content="专业报告", metadata={"purpose": "result_report"})})
        self.assertEqual(view.get_updates()["updates"][0]["purpose"], "result_report")

    def test_missing_preview_is_called_out(self):
        with TemporaryDirectory() as root:
            session = self.fixture(root)
            session.result_audit["files"] = []
            result = build_result_delivery(session, root)
            self.assertNotEqual(result["completion"], "completed")
            self.assertIn("图", "".join(result["limitations"]))

    def test_csv_only_request_does_not_require_unwanted_picture(self):
        with TemporaryDirectory() as root:
            session = self.fixture(root)
            session.task_contract["revisions"] = ["只要CSV"]
            folder = Path(root, session.result_relative_dir)
            (folder / "stats.csv").write_text("类别,占比\n候选,30\n", encoding="utf8")
            session.result_audit["files"] = [session.result_subdir + "/stats.csv"]
            result = build_result_delivery(session, root)
            self.assertFalse(any("没有可展示" in text for text in result["limitations"]))

    def test_plan_and_engineer_prompts_keep_locked_goal(self):
        from agents.engineer_agent import EngineerAgent
        from tools.task_contract import ensure_task_contract
        session = Session()
        session.user_request = "找出建筑变化"
        session.data_context = context()
        session.selected_inputs = ["pair/before.tif", "pair/after.tif"]
        ensure_task_contract(session)
        prompts = []
        scientist = object.__new__(ScientistAgent)
        scientist.session, scientist.name = session, "Scientist"
        scientist._call_llm = lambda prompt, **kw: prompts.append(prompt) or "候选方案"
        scientist.scan_data_sources = lambda text: []
        scientist.handle_message(Message(sender="Manager", receiver="Scientist", content=session.user_request))
        engineer = object.__new__(EngineerAgent)
        engineer.session = session
        engineer._code_message = lambda prompt: prompts.append(prompt)
        engineer.handle_message(Message(sender="Scientist", receiver="Engineer", content="改为亮度差分"))
        for prompt in prompts:
            self.assertIn("找出建筑变化", prompt)
            self.assertIn("pair/before.tif", prompt)
            self.assertIn("不得把建筑变化换成亮度变化", prompt)

    def test_real_execution_audit_manager_and_ui_deliver_candidate_together(self):
        from types import SimpleNamespace
        from agents.executor_agent import ExecutorAgent
        from agents.supervisor_agent import SupervisorAgent
        from tools.task_contract import ensure_task_contract
        with TemporaryDirectory() as root:
            view = make_view()
            view.config = {"executor_max_retries": 2}
            view.session.user_request = "找出建筑变化并生成PNG"
            view.session.confirmed_task = view.session.user_request
            view.session.data_context = context()
            view.session.selected_inputs = ["pair/before.tif", "pair/after.tif"]
            ensure_task_contract(view.session)
            manager = object.__new__(ManagerAgent)
            manager.name, manager.session = "Manager", view.session
            manager._call_llm = lambda prompt: "所有建筑均已准确识别，可信度99%。"
            supervisor = SupervisorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            supervisor.session, supervisor._project_root = view.session, root
            supervisor.select_relevant_files = lambda *args: self.fail("锁定输入后不应再次匹配其他文件")
            executor = ExecutorAgent(model="test", api_key="test", base_url="https://example.invalid", system_prompt="test")
            executor.session, executor._project_root = view.session, root
            code = ("import os\nfrom pathlib import Path\nfrom PIL import Image\n"
                    "p=Path(os.environ['URSA_RESULTS_DIR'])\n"
                    "Image.new('RGB',(8,8),'white').save(p/'map.png')\n"
                    "(p/'stats.csv').write_text('类别编号,类别,像元数,占比_%\\n0,未变化,3,75\\n1,变化候选,1,25\\n',encoding='utf-8-sig')\n")
            with patch("tools.io_kit.scan_data_directory", return_value={"data": {"files": []}}), patch("tools.io_kit._get_project_root", return_value=root):
                view._run_analysis_pipeline(
                    manager=manager, scientist=SimpleNamespace(handle_message=lambda m: Message(sender="Scientist", receiver="Engineer", content="亮度变化候选方案")),
                    engineer=SimpleNamespace(handle_message=lambda m: Message(sender="Engineer", receiver="Executor", content=code, msg_type=MessageType.CODE)),
                    executor=executor, supervisor=supervisor, sm=view.session.state_machine)
            view._running = False
            snapshot = view.get_updates()
            self.assertEqual(view.session.state_machine.get_current_phase(), ConversationPhase.COMPLETED)
            self.assertEqual(snapshot["result_delivery"]["completion"], "candidate")
            self.assertEqual(len(snapshot["result_delivery"]["previews"]), 1)
            self.assertNotIn("99%", snapshot["result_delivery"]["headline"])
            self.assertTrue(any(e.get("purpose") == "result_report" for e in snapshot["updates"]))


class InputAndResultApiTests(unittest.TestCase):
    def make_app(self, root):
        from gui.app import create_app
        view = make_view()
        view._running = False
        view.model = "offline-test"
        view._data_summary_html = view._data_analysis_html = ""
        app = create_app(controller=view)
        app.config.update(TESTING=True, PROJECT_ROOT=root)
        return app, view

    def tiff_bytes(self):
        import numpy as np
        from rasterio.io import MemoryFile
        with MemoryFile() as memory:
            with memory.open(driver="GTiff", width=8, height=8, count=3, dtype="uint8") as src:
                src.write(np.full((3, 8, 8), 128, dtype="uint8"))
                src.descriptions = ("Red", "Green", "Blue")
            return memory.read()

    def test_upload_catalog_and_bounded_preview_without_model(self):
        with TemporaryDirectory() as root:
            app, view = self.make_app(root)
            client = app.test_client()
            raw = self.tiff_bytes()
            response = client.post("/api/upload_inputs", data={"files": (BytesIO(raw), "before.tif")})
            self.assertEqual(response.status_code, 200)
            name = response.json["files"][0]
            self.assertEqual((Path(root) / "data" / name).read_bytes(), raw)
            self.assertIn("before", name)
            files = client.get("/api/input_files").json["files"]
            self.assertEqual(files[0]["file_name"], name)
            preview = client.get("/api/input_preview", query_string={"path": name})
            self.assertEqual(preview.mimetype, "image/png")
            with Image.open(BytesIO(preview.data)) as image:
                self.assertLessEqual(max(image.size), 512)
            self.assertEqual(view.session.tracer.get_events(), [])

    def test_invalid_upload_rolls_back_only_new_batch(self):
        with TemporaryDirectory() as root:
            app, view = self.make_app(root)
            keep = Path(root, "data/keep.tif")
            keep.parent.mkdir()
            keep.write_bytes(self.tiff_bytes())
            original = keep.read_bytes()
            result = app.test_client().post("/api/upload_inputs", data={"files": [(BytesIO(original), "ok.tif"), (BytesIO(b"not a tiff"), "bad.tif")]})
            self.assertEqual(result.status_code, 400)
            self.assertEqual(keep.read_bytes(), original)
            self.assertEqual(list(Path(root, "data/uploads").rglob("*.tif")), [])

    def test_busy_upload_does_not_touch_data(self):
        with TemporaryDirectory() as root:
            app, view = self.make_app(root)
            view._running = True
            response = app.test_client().post("/api/upload_inputs", data={"files": (BytesIO(b"x"), "bad.tif")})
            self.assertEqual(response.status_code, 409)
            self.assertFalse(Path(root, "data").exists())

    def test_result_download_denies_config_old_task_and_active_content(self):
        with TemporaryDirectory() as root:
            app, view = self.make_app(root)
            view.session = PlainDeliveryTests().fixture(root)
            client = app.test_client()
            name = view.session.result_audit["files"][0]
            response = client.get("/api/result_file", query_string={"path": name})
            self.assertEqual(response.mimetype, "image/png")
            response.close()
            for invalid in ("../config.json", "old_task/变化图.png", "/etc/passwd"):
                self.assertEqual(client.get("/api/result_file", query_string={"path": invalid}).status_code, 404)
            bad = Path(root, view.session.result_relative_dir, "script.html")
            bad.write_text("<script>alert(1)</script>", encoding="utf8")
            view.session.result_audit["files"].append(view.session.result_subdir + "/script.html")
            self.assertEqual(client.get("/api/result_file", query_string={"path": view.session.result_subdir + "/script.html"}).status_code, 404)

    def test_selected_input_cannot_escape_data_directory(self):
        from gui.data_files import validate_selected_inputs
        with TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                validate_selected_inputs(root, ["../config.json"])


if __name__ == "__main__":
    unittest.main()
