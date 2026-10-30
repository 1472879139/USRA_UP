"""真实请求生命周期的界面状态；全部使用模拟API，不计费。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock,patch

from core.session import Session
from agents.manager_agent import ManagerAgent


def reply():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="离线结果"))],usage=None)


class LLMActivityTests(unittest.TestCase):
    def session(self):
        session=Session();session.start();return session

    def agent(self,session):
        agent=ManagerAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
        session.register_agent(agent);return agent

    def test_overlapping_requests_do_not_clear_each_other(self):
        session=self.session();first=session.begin_llm_call("Manager");second=session.begin_llm_call("Scientist")
        state=session.get_llm_activity();self.assertTrue(state["active"]);self.assertEqual(state["request_count"],2)
        session.end_llm_call(first);self.assertTrue(session.get_llm_activity()["active"])
        session.end_llm_call(second);self.assertFalse(session.get_llm_activity()["active"])

    def test_termination_and_new_session_do_not_keep_busy_marker(self):
        session=self.session();token=session.begin_llm_call("Engineer")
        session.terminate("用户停止")
        self.assertFalse(session.get_llm_activity()["active"])
        self.assertFalse(self.session().get_llm_activity()["active"])
        session.end_llm_call(token)

    def test_nonstream_request_is_busy_only_during_call(self):
        session=self.session();agent=self.agent(session)
        def create(**kwargs):
            self.assertTrue(session.get_llm_activity()["active"])
            self.assertEqual(session.get_llm_activity()["agent"],"Manager")
            return reply()
        agent._client.chat.completions.create=Mock(side_effect=create)
        self.assertEqual(agent._call_llm("离线需求"),"离线结果")
        self.assertFalse(session.get_llm_activity()["active"])
        self.assertIn("llm_activity",session.to_dict())

    def test_stream_remains_busy_until_last_chunk_is_consumed(self):
        session=self.session();agent=self.agent(session)
        def chunks():
            for text,finish in [("离线",None),("结果","stop")]:
                self.assertTrue(session.get_llm_activity()["active"])
                yield SimpleNamespace(usage=None,choices=[SimpleNamespace(delta=SimpleNamespace(content=text),finish_reason=finish)])
        agent._client.chat.completions.create=Mock(return_value=chunks())
        self.assertEqual(agent._call_llm("离线需求",stream=True),"离线结果")
        self.assertFalse(session.get_llm_activity()["active"])

    def test_exception_clears_activity(self):
        session=self.session();agent=self.agent(session)
        def fail(**kwargs):
            self.assertTrue(session.get_llm_activity()["active"]);raise RuntimeError("模拟断开")
        agent._client.chat.completions.create=Mock(side_effect=fail)
        with redirect_stdout(io.StringIO()):self.assertTrue(agent._call_llm("离线需求").startswith("ERROR:"))
        self.assertFalse(session.get_llm_activity()["active"])
        self.assertEqual(session.get_llm_activity()["finished_requests"],1)

    def test_poll_snapshot_tracks_a_blocked_request_then_clears(self):
        from tests.test_ui_updates import make_view
        session=self.session();agent=self.agent(session);view=make_view();view.session=session
        from gui.app import create_app
        client=create_app(controller=view).test_client()
        entered,release=threading.Event(),threading.Event()
        def create(**kwargs):entered.set();release.wait(3);return reply()
        agent._client.chat.completions.create=Mock(side_effect=create)
        worker=threading.Thread(target=lambda:agent._call_llm("离线需求"));worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(client.get("/api/updates?since=0").get_json()["llm_activity"]["active"])
        finally:release.set();worker.join(3)
        state=client.get("/api/updates?since=0").get_json()["llm_activity"]
        self.assertFalse(state["active"])
        self.assertEqual(state["finished_requests"],1)
        self.assertTrue(view.get_updates()["running"])

    def test_elapsed_and_serialization_have_no_request_content(self):
        session=self.session()
        with patch("core.session.time.monotonic",return_value=100):token=session.begin_llm_call("Scientist")
        with patch("core.session.time.monotonic",return_value=107):state=session.get_llm_activity()
        self.assertEqual(state["elapsed_seconds"],7)
        self.assertEqual(set(state),{"active","agent","request_count","elapsed_seconds","total_requests","finished_requests"})
        session.end_llm_call(token)

    def test_native_tool_request_clears_before_executing_tools(self):
        from tests.test_tool_agent import task_fixture,response
        from agents.executor_agent import ExecutorAgent
        from tools import tool_agent_workflow as workflow
        from tools.agent_tools import AgentTools
        from core.message import Message,MessageType
        with task_fixture() as (root,session,_,runtime):
            (root/"prompts").mkdir();(root/"prompts/tool_agent_prompt.txt").write_text("离线工具测试",encoding="utf-8")
            agent=ExecutorAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            session.register_agent(agent);agent._project_root=str(root)
            replies=[response("inspect_data"),response("finish_task",{"status":"blocked","artifact_ids":[],"summary":"缺少去云工具","unfulfilled":["去云"]},identity="call_2")]
            def create(**kwargs):
                self.assertTrue(session.get_llm_activity()["active"]);return replies.pop(0)
            agent._client.chat.completions.create=Mock(side_effect=create)
            execute=AgentTools.execute
            def checked(runtime,name,args):
                self.assertFalse(session.get_llm_activity()["active"]);return execute(runtime,name,args)
            with patch.object(AgentTools,"execute",new=checked):
                workflow.execute_message(agent,Message(sender="Engineer",receiver="Executor",msg_type=MessageType.CODE,content="受控任务",metadata={"tool_agent_request":session.tool_agent_plan}))
            self.assertFalse(session.get_llm_activity()["active"])

    def test_short_request_is_still_visible_after_it_finishes_before_polling(self):
        from tests.test_ui_updates import make_view
        session=self.session();view=make_view();view.session=session
        token=session.begin_llm_call("Manager");session.end_llm_call(token)
        state=view.get_updates()["llm_activity"]
        self.assertFalse(state["active"])
        self.assertEqual(state["total_requests"],1)
        self.assertEqual(state["finished_requests"],1)

    def test_request_totals_do_not_leak_into_a_new_session(self):
        session=self.session();token=session.begin_llm_call("Manager");session.end_llm_call(token)
        self.assertEqual(session.get_llm_activity()["total_requests"],1)
        self.assertEqual(self.session().get_llm_activity()["total_requests"],0)

    def test_duplicate_completion_does_not_inflate_totals(self):
        session=self.session();token=session.begin_llm_call("Manager")
        session.end_llm_call(token);session.end_llm_call(token);session.end_llm_call("unknown")
        state=session.get_llm_activity()
        self.assertEqual(state["total_requests"],1)
        self.assertEqual(state["finished_requests"],1)

    def test_empty_gui_session_returns_zero_totals(self):
        from tests.test_ui_updates import make_view
        view=make_view();view.session=None
        state=view.get_updates()["llm_activity"]
        self.assertEqual(state["total_requests"],0)
        self.assertEqual(state["finished_requests"],0)

    def test_native_tool_provider_error_also_clears_marker(self):
        from tests.test_tool_agent import task_fixture
        from agents.executor_agent import ExecutorAgent
        from tools import tool_agent_workflow as workflow
        from core.message import Message,MessageType
        with task_fixture() as (root,session,_,runtime):
            (root/"prompts").mkdir();(root/"prompts/tool_agent_prompt.txt").write_text("离线工具测试",encoding="utf-8")
            agent=ExecutorAgent(model="offline",api_key="offline",base_url="http://127.0.0.1:1",system_prompt="")
            session.register_agent(agent);agent._project_root=str(root)
            agent._client.chat.completions.create=Mock(side_effect=RuntimeError("模拟错误"))
            result=workflow.execute_message(agent,Message(sender="Engineer",receiver="Executor",msg_type=MessageType.CODE,content="受控任务",metadata={"tool_agent_request":session.tool_agent_plan}))
            self.assertEqual(result.msg_type,MessageType.ERROR)
            self.assertFalse(session.get_llm_activity()["active"])


if __name__=="__main__":unittest.main()
