"""会话管理 — 整合 MessageBus、StateMachine，管理整个对话生命周期"""

import threading
from datetime import datetime
from typing import List, Optional, Callable
from .message import Message, MessageType
from .message_bus import MessageBus
from .state_machine import StateMachine, AgentState, ConversationPhase
from .trace_collector import TraceCollector


class Session:
    """会话管理器，协调所有 Agent 的交互"""

    def __init__(self, session_id: str = None):
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.bus = MessageBus()
        self.state_machine = StateMachine()
        self.tracer = TraceCollector()
        self.created_at = datetime.now().isoformat()
        self._active = False
        self._terminated = False
        self._terminate_reason: str = ""
        self._lock = threading.RLock()
        self._event_callbacks: dict = {}  # 事件回调
        # 当前任务的关键交接文本。编排器应从这里读取已确认任务，
        # 而不能把 approve 等控制指令当作分析需求传给下游 Agent。
        self.user_request: str = ""
        self.revision_feedback: list[str] = []
        self.manager_confirmation: str = ""
        self.confirmed_task: str = ""
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
            "active": self._active,
            "terminated": self._terminated,
            "terminate_reason": self._terminate_reason,
            "progress": self.get_progress(),
            "message_count": self.bus.get_round_count(),
        }
        if hasattr(self, 'tracer') and self.tracer:
            result["trace_summary"] = self.tracer.get_summary()
        return result
