"""远程工具离线验收：合成影像、回环HTTP和模拟模型，不调用真实API或GPU。"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import builtins
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import zipfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

from tools import prithvi_protocol as protocol
from tools import prithvi_remote as remote
from tools import prithvi_workflow as workflow
from tools.result_audit import audit_execution_results
from tools.result_delivery import build_result_delivery
from core.session import Session
from core.message import Message, MessageType

PROJECT = Path(__file__).resolve().parents[1]
sys.modules["prithvi_protocol"] = protocol
spec = importlib.util.spec_from_file_location("test_gpu_service", PROJECT/"deploy/prithvi_gpu/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def image(path, empty=False):
    data = np.full((13, 512, 512), 1000, dtype="int16")
    data[2, :256] = 1500
    data[2, 256:] = 500
    data[:, 0, 0] = 0
    if empty:
        data[:] = 0
    with rasterio.open(path, "w", driver="GTiff", count=13, height=512, width=512, dtype="int16", nodata=0,
                       crs="EPSG:4326", transform=from_origin(70, 20, .0001, .0001)) as dst:
        dst.write(data)
        for number, name in enumerate(protocol.BAND_NAMES, 1):
            dst.set_band_description(number, name)


class FakeModel:
    info = {"offline_test_double": True}
    def predict(self, raw, valid):
        result = protocol.baseline(raw, valid)
        result[1, 1] = 0
        return result, {"forward_seconds": 0.0}


@contextmanager
def fixture(network=False):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        folder = root/"data/sample"
        folder.mkdir(parents=True)
        image(folder/"input.tif")
        image(folder/"empty.tif", empty=True)
        (folder/"metadata.json").write_text(json.dumps({"sensor": "Sentinel-2", "product_level": "L1C",
            "encoding": "DN/10000", "bands": protocol.BAND_NAMES}), encoding="utf-8")
        server = None
        if network:
            server = service.WaterServer(("127.0.0.1", 0), FakeModel(), root/"scratch")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        port = server.server_port if server else 18765
        (root/"gpu.local.json").write_text(json.dumps({"enabled": True, "base_url": f"http://127.0.0.1:{port}", "timeout_seconds": 2}), encoding="utf-8")
        session = Session()
        session.user_request = "帮我圈出这张影像里的水体，告诉我大概占多少。"
        session.selected_inputs = ["sample/input.tif", "sample/empty.tif"]
        session.data_context = {"data_root": str(root/"data"), "files": [
            {"file_name": name, "readable": True, "band_count": 13, "band_names": protocol.BAND_NAMES, "crs": "EPSG:4326"}
            for name in session.selected_inputs]}
        from tools.task_contract import ensure_task_contract
        ensure_task_contract(session)
        try:
            yield root, session, server
        finally:
            if server:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


def small_preview(raw, model, index, valid, path):
    from PIL import Image
    Image.new("RGB", (4, 4), "blue").save(path)


def completed(root, session):
    session.remote_tool_plan = workflow.select_remote_tool(session, root)
    output = root/"results"/session.result_subdir
    with patch.object(remote, "save_preview", side_effect=small_preview), redirect_stdout(io.StringIO()):
        workflow.run_remote_workflow(session.remote_tool_plan, output, root)
    files = [path.relative_to(root/"results").as_posix() for path in output.rglob("*") if path.is_file()]
    session.result_audit = audit_execution_results(root/"results", files, session.user_request, task_subdir=session.result_subdir)
    if session.result_audit["errors"]:
        raise AssertionError(session.result_audit["errors"])
    workflow.attach_remote_audit(session, root)
    return output


class RemoteWaterTests(unittest.TestCase):
    def test_fixed_tool_confirmation_matches_actual_method_and_outputs(self):
        from agents.manager_agent import ManagerAgent
        with fixture() as (root, session, _), patch("tools.io_kit._get_project_root", return_value=str(root)):
            manager = ManagerAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            session.register_agent(manager)
            with patch.object(manager, "_call_llm", return_value="取交集作为主体，并输出约合面积。") as llm:
                result = manager.handle_message(Message(sender="User", receiver="Manager", content=session.user_request))
            self.assertEqual(result.msg_type, MessageType.TEXT)
            self.assertIn("保留模型", result.content)
            self.assertIn("比例", result.content)
            self.assertNotIn("交集", result.content)
            self.assertNotIn("面积", result.content)
            llm.assert_not_called()

    def test_extra_area_requirement_keeps_original_llm_planning_path(self):
        from agents.manager_agent import ManagerAgent
        with fixture() as (root, session, _), patch("tools.io_kit._get_project_root", return_value=str(root)):
            manager = ManagerAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            session.register_agent(manager)
            with patch.object(manager, "_call_llm", return_value="面积需求需要另行核对计算方案。") as llm:
                result = manager.handle_message(Message(sender="User", receiver="Manager", content="提取水体并统计真实面积"))
            self.assertIn("面积需求", result.content)
            llm.assert_called_once()

    def test_project_import_failure_card_explains_client_problem(self):
        with fixture() as (root, session, _):
            session.remote_tool_plan = workflow.select_remote_tool(session, root)
            session.terminate("远程固定工具未通过代码执行：ModuleNotFoundError: No module named 'tools'")
            delivery = build_result_delivery(session, root)
            self.assertEqual(delivery["completion"], "failed")
            self.assertIn("本机", delivery["headline"])
            self.assertIn("更新", delivery["next_step"])
            self.assertNotIn("补充缺少的数据", delivery["next_step"])
            self.assertFalse(delivery["files"])

    def test_real_executor_child_runs_fixed_workflow_and_produces_auditable_results(self):
        from agents.executor_agent import ExecutorAgent
        with fixture(network=True) as (root, session, server):
            # 仅复制当前源码到测试临时项目，不使用真实配置、业务数据或参考答案。
            for name in ["tools", "core"]:
                shutil.copytree(PROJECT/name, root/name, ignore=shutil.ignore_patterns("__pycache__"))
            session.start()
            session.remote_tool_plan = workflow.select_remote_tool(session, root)
            code = workflow.tool_message(session, deepcopy(session.remote_tool_plan))
            executor = ExecutorAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            executor._project_root = str(root)
            session.register_agent(executor)
            server.model.predict = Mock(wraps=server.model.predict)
            with patch.dict(os.environ, {"PYTHONPATH": ""}):
                result = executor.handle_message(code)
            self.assertEqual(result.msg_type, MessageType.RESULT, result.content)
            server.model.predict.assert_called_once()
            session.result_audit = audit_execution_results(root/"results", result.metadata["files_generated"],
                                                          session.user_request, task_subdir=session.result_subdir)
            self.assertFalse(session.result_audit["errors"], session.result_audit["errors"])
            workflow.attach_remote_audit(session, root)
            delivery = build_result_delivery(session, root)
            self.assertEqual(delivery["completion"], "candidate")
            self.assertTrue(delivery["previews"])
            self.assertIn("准确性尚未验证", delivery["verification"])
            self.assertTrue((root/"results"/session.result_subdir/"remote_water_workflow.json").is_file())

    def test_packaged_service_reaches_startup_through_its_bundled_runtime(self):
        spec = importlib.util.spec_from_file_location("build_startup_archive", PROJECT/"deploy/prithvi_gpu/build_service.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/"service.pyz"
            builder.build(archive)
            original_import = builtins.__import__
            fake_model_factory = Mock(return_value=FakeModel())
            imported_runtime = []
            def import_with_fake_gpu(name, *args, **kwargs):
                module = original_import(name, *args, **kwargs)
                if name == "runtime":
                    self.assertTrue(Path(module.__file__).is_relative_to(root/"ursa-gpu-runtime/services"))
                    module.GPUModel = fake_model_factory
                    imported_runtime.append(module)
                return module
            saved_path, saved_bytecode = list(sys.path), sys.dont_write_bytecode
            output = io.StringIO()
            try:
                with patch.dict(sys.modules):
                    sys.modules.pop("runtime", None)
                    sys.modules["run_batch"] = None
                    with patch.object(Path, "home", return_value=root), patch.object(sys, "argv", [str(archive), "--port", "18765"]), \
                         patch.object(builtins, "__import__", side_effect=import_with_fake_gpu), \
                         patch("http.server.ThreadingHTTPServer.__init__", return_value=None) as bind, \
                         patch("http.server.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt) as serve, \
                         patch("http.server.ThreadingHTTPServer.server_close") as close, redirect_stdout(output):
                        runpy.run_path(str(archive), run_name="__main__")
                    fake_model_factory.assert_called_once_with()
                    self.assertEqual(len(imported_runtime), 1)
                    self.assertEqual(bind.call_args.args[0], ("127.0.0.1", 18765))
                    serve.assert_called_once_with()
                    close.assert_called_once_with()
                    self.assertIn("模型已就绪", output.getvalue())
            finally:
                sys.path[:] = saved_path
                sys.dont_write_bytecode = saved_bytecode

    def test_metadata_change_invalidates_preconfirmed_data_snapshot(self):
        from tools.data_context import load_data_context
        from agents.manager_agent import ManagerAgent
        from core.state_machine import ConversationPhase
        with fixture() as (root, session, _), patch("tools.io_kit._get_project_root", return_value=str(root)):
            session.data_context = load_data_context(root/"data")
            sidecar = root/"data/sample/metadata.json"
            before = sidecar.stat()
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            value["product_level"] = "L2A"
            sidecar.write_text(json.dumps(value), encoding="utf-8")
            os.utime(sidecar, ns=(before.st_atime_ns, before.st_mtime_ns+1_000_000))
            manager = ManagerAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            session.register_agent(manager)
            with patch.object(manager, "_call_llm", return_value="输入说明已变化，需要重新确认。"):
                response = manager.handle_message(Message(sender="User", receiver="Manager", msg_type=MessageType.CONTROL, content="approve"))
            self.assertEqual(response.receiver, "User")
            self.assertTrue(response.metadata["requires_confirmation"])
            self.assertEqual(session.get_current_phase(), ConversationPhase.MANAGER_CONFIRM.value)

    def test_encoding_cannot_silently_resize_a_prediction(self):
        with fixture() as (root, _, _):
            _, _, profile = protocol.read_input(root/"data/sample/input.tif")
            with self.assertRaisesRegex(ValueError, "隐式重采样"):
                protocol.encode_mask(np.zeros((256, 256), dtype="uint8"), profile)

    def test_service_archive_contains_only_sources_and_bootstrap_is_repeatable(self):
        spec = importlib.util.spec_from_file_location("build_gpu_archive", PROJECT/"deploy/prithvi_gpu/build_service.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root/"service.pyz"
            digest = builder.build(destination)
            self.assertEqual(digest, protocol.file_sha(destination))
            with zipfile.ZipFile(destination) as bundle:
                self.assertEqual(set(bundle.namelist()), {"__main__.py", "service.py", "runtime.py", "prithvi_protocol.py"})
            saved_path = list(sys.path)
            try:
                for _ in range(2):
                    with patch.object(Path, "home", return_value=root), patch.object(sys, "argv", [str(destination), "--help"]), redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit) as status:
                            runpy.run_path(str(destination), run_name="__main__")
                        self.assertEqual(status.exception.code, 0)
                expanded = next((root/"ursa-gpu-runtime/services").glob("*/service.py"))
                expanded.write_text("changed", encoding="utf-8")
                with patch.object(Path, "home", return_value=root), patch.object(sys, "argv", [str(destination), "--help"]):
                    with self.assertRaisesRegex(ValueError, "被改变"):
                        runpy.run_path(str(destination), run_name="__main__")
            finally:
                sys.path[:] = saved_path

    def test_corrupt_zip_is_a_validation_error(self):
        with fixture() as (root, _, _):
            source = root/"data/sample/input.tif"
            raw, valid, profile = protocol.read_input(source)
            with self.assertRaisesRegex(ValueError, "损坏"):
                protocol.unpack_response(b"incomplete zip", "a"*32, protocol.file_sha(source), raw, valid, profile)

    def test_returned_mask_cannot_hide_disputed_pixels_as_nodata(self):
        with fixture() as (root, _, _):
            source = root/"data/sample/input.tif"
            raw, valid, profile = protocol.read_input(source)
            mask = protocol.baseline(raw, valid)
            bad = mask.copy()
            bad[1, 1] = 255
            content = protocol.package_response(bad, mask, profile, "a"*32, protocol.file_sha(source), {})
            with self.assertRaisesRegex(ValueError, "有效范围"):
                protocol.unpack_response(content, "a"*32, protocol.file_sha(source), raw, valid, profile)

    def test_remote_execution_failure_stops_without_llm_rewriting(self):
        from core.execution_cycle import run_execution_cycle
        session = Session()
        session.start()
        session.remote_tool_plan = {"name": remote.VERSION}
        executor, engineer, supervisor = Mock(), Mock(), Mock()
        executor.handle_message.return_value = Message(sender="Executor", receiver="Engineer", msg_type=MessageType.ERROR, content="模拟连接失败")
        result = run_execution_cycle(session, engineer, executor, supervisor,
                                     Message(sender="Engineer", receiver="Executor", msg_type=MessageType.CODE, content="pass"))
        self.assertIsNone(result)
        self.assertTrue(session.is_terminated())
        self.assertEqual(executor.handle_message.call_count, 1)
        engineer.handle_message.assert_not_called()

    def test_model_exception_is_a_failure_and_does_not_lock_gpu_forever(self):
        with fixture(network=True) as (root, _, server):
            server.model = Mock()
            server.model.predict.side_effect = RuntimeError("模拟GPU失败")
            raw = (root/"data/sample/input.tif").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "GPU推理"):
                remote.post(remote.settings(root), raw, "a"*32, protocol.sha(raw))
            server.model = FakeModel()
            self.assertTrue(remote.post(remote.settings(root), raw, "b"*32, protocol.sha(raw)))

    def test_default_disabled_makes_no_network_calls(self):
        with fixture() as (root, session, _):
            (root/"gpu.local.json").unlink()
            with patch.object(remote, "post") as post:
                self.assertIsNone(workflow.select_remote_tool(session, root))
                post.assert_not_called()

    def test_only_loopback_endpoints_are_allowed(self):
        with fixture() as (root, _, _):
            for url in ["https://example.com:18765", "http://0.0.0.0:18765", "http://127.0.0.1:18765/a", "http://name:secret@127.0.0.1:18765"]:
                (root/"gpu.local.json").write_text(json.dumps({"enabled": True, "base_url": url}), encoding="utf-8")
                with self.subTest(url=url), self.assertRaises(ValueError):
                    remote.settings(root)

    def test_input_contract_prevents_b8_and_l2a_confusion(self):
        with fixture() as (root, _, _):
            path = root/"data/sample/input.tif"
            self.assertEqual(protocol.inspect_input(path)["valid_pixels"], 512*512-1)
            with rasterio.open(path, "r+") as dst:
                dst.set_band_description(9, "B8")
            with self.assertRaises(ValueError):
                protocol.inspect_input(path)
        with fixture() as (root, _, _):
            path = root/"data/sample/metadata.json"
            content = json.loads(path.read_text())
            content["product_level"] = "L2A"
            path.write_text(json.dumps(content))
            with self.assertRaises(ValueError):
                protocol.inspect_input(path.parent/"input.tif")

    def test_invalid_input_rejected_before_network(self):
        with fixture() as (root, _, _), patch.object(remote, "post") as post:
            with self.assertRaises(protocol.EmptyInputError):
                remote.run_remote_water(root/"data/sample/empty.tif", root/"results/test", root)
            post.assert_not_called()
            self.assertFalse((root/"results/test").exists())

    def test_plan_keeps_rejected_input_without_discarding_good_input(self):
        with fixture() as (root, session, _):
            plan = workflow.select_remote_tool(session, root)
            self.assertEqual([item["file_name"] for item in plan["inputs"]], ["sample/input.tif"])
            self.assertEqual([item["file_name"] for item in plan["rejected_inputs"]], ["sample/empty.tif"])

    def test_extra_requirements_do_not_get_silently_overridden(self):
        with fixture() as (root, session, _):
            for request in ["只输出CSV", "比较前后变化", "统计水体面积", "生成岸线矢量", "不要水体"]:
                session.revision_feedback = [request]
                self.assertIsNone(workflow.select_remote_tool(session, root))

    def test_roundtrip_raw_geotiffs_and_independent_audit(self):
        with fixture(network=True) as (root, session, _):
            output = completed(root, session)
            facts = session.result_audit["remote_water"]["scenes"][0]["diagnostics"]
            self.assertEqual(facts["disagreement_pixels"], 1)
            self.assertEqual(facts["accuracy_status"], "unavailable")
            self.assertFalse(facts["reference_used"])
            delivery = build_result_delivery(session, root)
            self.assertIn("1幅", delivery["headline"])
            self.assertIn("未处理", delivery["headline"])
            self.assertIn("准确性尚未验证", delivery["verification"])
            self.assertTrue(any("没有有效像元" in line for line in delivery["findings"]))
            self.assertTrue((output/"remote_01/final_model_classification.tif").exists())

    def test_final_plain_report_uses_verified_facts_without_llm_self_grading(self):
        from agents.manager_agent import ManagerAgent
        with fixture(network=True) as (root, session, _), patch("tools.io_kit._get_project_root", return_value=str(root)):
            completed(root, session)
            manager = ManagerAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            session.register_agent(manager)
            with patch.object(manager, "_call_llm") as llm:
                report = manager.handle_message(Message(sender="Executor", receiver="Manager", msg_type=MessageType.RESULT,
                    content="ignored", metadata={"result_audit": session.result_audit}))
                self.assertIn("准确性尚未验证", report.content)
                self.assertIn("判断不同", report.content)
                llm.assert_not_called()

    def test_request_identity_mismatch_rejected(self):
        with fixture() as (root, _, _):
            source = root/"data/sample/input.tif"
            raw, valid, profile = protocol.read_input(source)
            mask = protocol.baseline(raw, valid)
            content = protocol.package_response(mask, mask, profile, "a"*32, protocol.file_sha(source), {})
            with self.assertRaisesRegex(ValueError, "本次请求"):
                protocol.unpack_response(content, "b"*32, protocol.file_sha(source), raw, valid, profile)

    def test_audit_rejects_result_from_another_locked_request(self):
        with fixture(network=True) as (root, session, _):
            completed(root, session)
            session.remote_tool_plan["inputs"][0]["request_id"] = "b"*32
            with self.assertRaisesRegex(ValueError, "身份不一致"):
                workflow.attach_remote_audit(session, root)

    def test_changed_returned_pixels_cannot_bypass_digest(self):
        with fixture() as (root, _, _):
            source = root/"data/sample/input.tif"
            raw, valid, profile = protocol.read_input(source)
            mask = protocol.baseline(raw, valid)
            content = protocol.package_response(mask, mask, profile, "a"*32, protocol.file_sha(source), {})
            buffer = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(buffer, "w") as dst:
                for name in src.namelist():
                    data = src.read(name)
                    dst.writestr(name, data+b"changed" if name == "model_mask.tif" else data)
            with self.assertRaisesRegex(ValueError, "哈希"):
                protocol.unpack_response(buffer.getvalue(), "a"*32, protocol.file_sha(source), raw, valid, profile)

    def test_wrong_grid_rejected_even_with_correct_hashes(self):
        with fixture() as (root, _, _):
            source = root/"data/sample/input.tif"
            raw, valid, profile = protocol.read_input(source)
            mask = protocol.baseline(raw, valid)
            shifted = dict(profile, transform=from_origin(80, 20, .0001, .0001))
            content = protocol.package_response(mask, mask, shifted, "a"*32, protocol.file_sha(source), {})
            with self.assertRaisesRegex(ValueError, "网格"):
                protocol.unpack_response(content, "a"*32, protocol.file_sha(source), raw, valid, profile)

    def test_disagreement_is_not_reference_accuracy(self):
        valid = np.ones((2, 2), bool)
        a = np.array([[1, 0], [0, 0]], dtype="uint8")
        b = np.array([[0, 1], [0, 0]], dtype="uint8")
        facts = protocol.diagnostics(a, b, valid)
        self.assertEqual(facts["disagreement_fraction_of_union"], 1)
        self.assertNotIn("f1", facts)
        self.assertEqual(protocol.diagnostics(a*0, b*0, valid)["disagreement_fraction_of_union"], None)

    def test_config_and_input_changes_require_replanning(self):
        with fixture() as (root, session, _):
            plan = workflow.select_remote_tool(session, root)
            (root/"gpu.local.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "配置已经改变"):
                workflow.run_remote_workflow(plan, root/"results/test", root)

    def test_statistics_tamper_rejected_even_if_hash_record_is_updated(self):
        with fixture(network=True) as (root, session, _):
            output = completed(root, session)
            table = output/"remote_01/model_class_statistics.csv"
            table.write_text("类别编号,类别,像元数,占比_%\n0,非水体候选,1,100\n", encoding="utf-8-sig")
            record_path = output/"remote_01/remote_result.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["artifacts_sha256"][table.name] = protocol.file_sha(table)
            record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "分类统计"):
                workflow.attach_remote_audit(session, root)

    def test_busy_gpu_returns_explicit_error(self):
        with fixture(network=True) as (root, _, server):
            raw = (root/"data/sample/input.tif").read_bytes()
            server.gpu_slot.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "GPU正在"):
                    remote.post(remote.settings(root), raw, "a"*32, protocol.sha(raw))
            finally:
                server.gpu_slot.release()

    def test_disconnected_service_does_not_publish_success_or_fallback(self):
        with fixture() as (root, _, _):
            with patch.object(remote, "post", side_effect=RuntimeError("模拟断线")):
                with self.assertRaisesRegex(RuntimeError, "断线"):
                    remote.run_remote_water(root/"data/sample/input.tif", root/"results/test", root)
            self.assertFalse((root/"results/test").exists())

    def test_scientist_and_engineer_use_locked_tool_without_llm(self):
        from agents.scientist_agent import ScientistAgent
        from agents.engineer_agent import EngineerAgent
        with fixture() as (root, session, _), patch("tools.io_kit._get_project_root", return_value=str(root)):
            scientist = ScientistAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            engineer = EngineerAgent(model="offline", api_key="offline", base_url="http://127.0.0.1:1", system_prompt="")
            session.register_agent(scientist)
            session.register_agent(engineer)
            with patch.object(scientist, "_call_llm") as a, patch.object(engineer, "_call_llm") as b:
                plan_message = scientist.handle_message(Message(sender="Manager", receiver="Scientist", content=session.user_request))
                self.assertIn("remote_tool_request", plan_message.metadata)
                code = engineer.handle_message(plan_message)
                self.assertEqual(code.msg_type, MessageType.CODE)
                self.assertIn("URSA_RESULTS_DIR", code.content)
                forged = deepcopy(plan_message)
                forged.metadata["remote_tool_request"]["version"] = 2
                self.assertEqual(engineer.handle_message(forged).msg_type, MessageType.ERROR)
                a.assert_not_called()
                b.assert_not_called()


if __name__ == "__main__":
    unittest.main()
