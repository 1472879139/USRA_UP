"""状态机 — 管理对话流程的状态转换和死锁检测"""

from enum import Enum, auto
from datetime import datetime
from typing import Dict, List, Optional


class AgentState(Enum):
    """Agent 状态"""
    IDLE = auto()
    THINKING = auto()
    WAITING = auto()
    EXECUTING = auto()
    BLOCKED = auto()
    ERROR = auto()
    TERMINATED = auto()


class ConversationPhase(Enum):
    """对话阶段"""
    INIT = "初始化"
    USER_INPUT = "用户输入"
    MANAGER_CONFIRM = "Manager 确认"
    SCIENTIST_ANALYSIS = "Scientist 分析"
    ENGINEER_CODING = "Engineer 编码"
    EXECUTOR_RUNNING = "Executor 执行"
    REVIEW_CHECKING = "监督检查"
    REPORTING = "生成报告"
    COMPLETED = "完成"
    TERMINATED = "已终止"


class StateMachine:
    """对话状态机，追踪流程进度和检测异常"""

    VALID_TRANSITIONS = {
        ConversationPhase.INIT: [ConversationPhase.USER_INPUT],
        ConversationPhase.USER_INPUT: [ConversationPhase.MANAGER_CONFIRM],
        ConversationPhase.MANAGER_CONFIRM: [ConversationPhase.SCIENTIST_ANALYSIS, ConversationPhase.USER_INPUT],
        ConversationPhase.SCIENTIST_ANALYSIS: [ConversationPhase.ENGINEER_CODING, ConversationPhase.REVIEW_CHECKING],
        ConversationPhase.ENGINEER_CODING: [ConversationPhase.EXECUTOR_RUNNING, ConversationPhase.REVIEW_CHECKING],
        ConversationPhase.EXECUTOR_RUNNING: [ConversationPhase.REVIEW_CHECKING, ConversationPhase.ENGINEER_CODING, ConversationPhase.REPORTING],
        ConversationPhase.REVIEW_CHECKING: [ConversationPhase.ENGINEER_CODING, ConversationPhase.EXECUTOR_RUNNING, ConversationPhase.REPORTING, ConversationPhase.TERMINATED],
        ConversationPhase.REPORTING: [ConversationPhase.COMPLETED, ConversationPhase.USER_INPUT],
        ConversationPhase.COMPLETED: [],
        ConversationPhase.TERMINATED: [],
    }

    def __init__(self):
        self.current_phase: ConversationPhase = ConversationPhase.INIT
        self.agent_states: Dict[str, AgentState] = {}
        self.phase_history: List[tuple] = []  # (phase, timestamp)
        self._stuck_counter: Dict[str, int] = {}  # agent_name → 连续相同回复计数
        self._last_agent_message: Dict[str, str] = {}  # agent_name → 最后一条消息内容
        self._transition_count: int = 0
        self._max_transitions: int = 50  # 最大状态转换次数，超限触发死锁检测

    def set_agent_state(self, agent_name: str, state: AgentState) -> None:
        self.agent_states[agent_name] = state

    def get_agent_state(self, agent_name: str) -> AgentState:
        return self.agent_states.get(agent_name, AgentState.IDLE)

    def transition_to(self, new_phase: ConversationPhase) -> bool:
        """尝试状态转换，返回是否成功"""
        if new_phase not in self.VALID_TRANSITIONS.get(self.current_phase, []):
            return False

        self.phase_history.append((self.current_phase, datetime.now().isoformat()))
        self.current_phase = new_phase
        self._transition_count += 1
        return True

    def force_transition(self, new_phase: ConversationPhase) -> None:
        """强制状态转换（审查 Agent 使用）"""
        self.phase_history.append((self.current_phase, datetime.now().isoformat()))
        self.current_phase = new_phase
        self._transition_count += 1

    def get_current_phase(self) -> ConversationPhase:
        return self.current_phase

    def detect_deadlock(self) -> Optional[str]:
        """检测死锁，返回死锁原因或 None"""
        # 检查1：转换次数超限
        if self._transition_count > self._max_transitions:
            return f"状态转换次数超限 ({self._max_transitions})，可能存在死循环"

        # 检查2：是否有 Agent 长时间处于 BLOCKED 状态
        blocked_agents = [name for name, state in self.agent_states.items() if state == AgentState.BLOCKED]
        if len(blocked_agents) >= 2:
            return f"多个 Agent 处于阻塞状态: {blocked_agents}"

        # 检查3：所有 Agent 都在 WAITING
        waiting_agents = [name for name, state in self.agent_states.items() if state == AgentState.WAITING]
        if len(waiting_agents) >= 3:
            return f"3个以上 Agent 处于等待状态，可能死锁: {waiting_agents}"

        return None

    def check_repetition(self, agent_name: str, message_content: str, threshold: int = 3) -> bool:
        """检测 Agent 是否在重复相同内容"""
        last = self._last_agent_message.get(agent_name, "")
        # 简单相似度检测：内容相同或高度相似
        if message_content.strip() == last.strip():
            self._stuck_counter[agent_name] = self._stuck_counter.get(agent_name, 0) + 1
        else:
            self._stuck_counter[agent_name] = 0

        self._last_agent_message[agent_name] = message_content
        return self._stuck_counter.get(agent_name, 0) >= threshold

    def get_progress_summary(self) -> dict:
        """获取进度摘要"""
        return {
            "current_phase": self.current_phase.value,
            "agent_states": {k: v.name for k, v in self.agent_states.items()},
            "transition_count": self._transition_count,
            "phase_sequence": [p.value for p, _ in self.phase_history],
        }

    def reset(self) -> None:
        self.current_phase = ConversationPhase.INIT
        self.agent_states.clear()
        self.phase_history.clear()
        self._stuck_counter.clear()
        self._last_agent_message.clear()
        self._transition_count = 0
