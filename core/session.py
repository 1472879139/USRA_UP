"""会话管理 — 整合 MessageBus、StateMachine，管理整个对话生命周期"""

import threading
import time
import uuid
from datetime import datetime
from typing import List, Optional, Callable
from .message import Message, MessageType
from .message_bus import MessageBus
from .state_machine import StateMachine, AgentState, ConversationPhase
from .trace_collector import TraceCollector


class Session:
    """会话管理器，协调所有 Agent 的交互"""

    def __init__(self, session_id: str = None, tool_agent_options=None):
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.bus = MessageBus()
        self.state_machine = StateMachine()
        self.tracer = TraceCollector()
        self.created_at = datetime.now().isoformat()
        self.result_subdir = f"task_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        self.result_relative_dir = "results/" + self.result_subdir
        self._active = False
        self._terminated = False
        self._terminate_reason: str = ""
        self._lock = threading.RLock()
        self._event_callbacks: dict = {}  # 事件回调
        self._llm_requests: dict = {}  # 仅记录本会话在途请求，不保存内容或认证信息
        self._llm_total_requests = 0
        self._llm_finished_requests = 0
        # 当前任务的关键交接文本。编排器应从这里读取已确认任务，
        # 而不能把 approve 等控制指令当作分析需求传给下游 Agent。
        self.user_request: str = ""
        self.revision_feedback: list[str] = []
        self.manager_confirmation: str = ""
        self.confirmed_task: str = ""
        self.data_context: dict = {}
        self.result_audit: dict = {}
        self.selected_inputs: list[str] = []
        self.task_contract: dict = {}
        self.task_capability: dict = {}
        self.result_delivery: dict = {}
        self.tool_plan: dict = {}
        self.submission: dict = {}  # 交卷收据；外部评分不得写回会话
        # 仅存储本轮固定远程工具计划；由Scientist锁定，Engineer/Executor/Supervisor消费。
        self.remote_tool_plan: dict = {}
        # 仅存储工具调度选项、锁定计划和执行收据；不包含API认证信息。
        self.tool_agent_options: dict = dict(tool_agent_options or {})
        self.tool_agent_plan: dict = {}
        self.tool_agent_receipt: dict = {}
        self.autonomous_state: dict = {}  # 本次目标、完成项与待补充信息；不含模型认证
        # 注册工具名供追踪器检测 stdout 中的工具调用
        try:
            from tools.registry import get_all_tool_names
            self.tracer.set_tool_names(get_all_tool_names())
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            self._active = True
            self.state_machine.force_transition(ConversationPhase.USER_INPUT)

    def terminate(self, reason: str = "") -> None:
        """终止会话"""
        with self._lock:
            self._active = False
            self._terminated = True
            self._terminate_reason = reason
            self.state_machine.force_transition(ConversationPhase.TERMINATED)
            # 广播终止消息
            self.bus.publish(Message(
                sender="Session",
                receiver="broadcast",
                content=f"会话终止: {reason}" if reason else "会话已终止",
                msg_type=MessageType.SYSTEM,
                metadata={"action": "terminate", "reason": reason},
            ))

    def is_active(self) -> bool:
        return self._active and not self._terminated

    def is_terminated(self) -> bool:
        return self._terminated

    def get_terminate_reason(self) -> str:
        return self._terminate_reason

    def begin_llm_call(self, agent: str) -> str:
        """记录一次实际API请求；返回仅用于finally清理的内部标识。"""
        token = uuid.uuid4().hex
        with self._lock:
            self._llm_requests[token] = {"agent": agent, "started": time.monotonic()}
            self._llm_total_requests += 1
        return token

    def end_llm_call(self, token: str) -> None:
        """结束对应请求；旧请求返回不会清除其他仍在进行的请求。"""
        with self._lock:
            if self._llm_requests.pop(token, None) is not None:
                self._llm_finished_requests += 1

    def get_llm_activity(self) -> dict:
        """供轮询/导出读取的状态快照；会话终止后不保留等待动画。"""
        with self._lock:
            pending = list(self._llm_requests.values()) if self._active and not self._terminated else []
            return {"active": bool(pending), "agent": pending[-1]["agent"] if pending else "",
                    "request_count": len(pending),
                    "total_requests": self._llm_total_requests,
                    "finished_requests": self._llm_finished_requests,
                    "elapsed_seconds": max(0, int(time.monotonic()-min(item["started"] for item in pending))) if pending else 0}

    def register_agent(self, agent) -> None:
        """向会话注册 Agent"""
        self.bus.register_agent(agent.name, agent)
        agent.session = self
        self.state_machine.set_agent_state(agent.name, AgentState.IDLE)

    def get_message_history(self, last_n: int = 50) -> List[Message]:
        return self.bus.get_history(last_n)

    def get_progress(self) -> dict:
        return self.state_machine.get_progress_summary()

    def get_current_phase(self) -> str:
        return self.state_machine.get_current_phase().value

    def on_event(self, event_name: str, callback: Callable) -> None:
        """注册事件回调"""
        self._event_callbacks[event_name] = callback

    def emit_event(self, event_name: str, data: dict = None) -> None:
        """触发事件"""
        cb = self._event_callbacks.get(event_name)
        if cb:
            try:
                cb(data or {})
            except Exception:
                pass

    def to_dict(self) -> dict:
        result = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "result_directory": self.result_relative_dir,
            "active": self._active,
            "terminated": self._terminated,
            "terminate_reason": self._terminate_reason,
            "progress": self.get_progress(),
            "message_count": self.bus.get_round_count(),
            "task_contract": self.task_contract,
            "task_capability": self.task_capability,
            "result_delivery": self.result_delivery,
            "tool_plan": self.tool_plan,
            "submission": self.submission,
            "llm_activity": self.get_llm_activity(),
            "remote_tool_plan": self.remote_tool_plan,
            "tool_agent_options": self.tool_agent_options,
            "tool_agent_plan": self.tool_agent_plan,
            "tool_agent_receipt": self.tool_agent_receipt,
            "autonomous_state": self.autonomous_state,
        }
        if hasattr(self, 'tracer') and self.tracer:
            result["trace_summary"] = self.tracer.get_summary()
        return result
