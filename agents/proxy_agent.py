"""代理 Agent — 消息总线拦截器（v1.14 修复：警告消息不再被丢弃）"""

from typing import Optional
from core.message import Message, MessageType


class ProxyAgent:
    """代理拦截器，嵌入 MessageBus 的拦截链中

    工作方式：
    1. 每条消息在路由前经过 ProxyAgent
    2. ProxyAgent 将消息先发给 SupervisorAgent 审查
    3. SupervisorAgent 返回 None → 审查通过，消息继续路由
    4. SupervisorAgent 返回 force_terminate → 消息被拦截
    5. SupervisorAgent 返回其他警告 → 原始消息继续路由，警告反馈通过总线发送
    """

    def __init__(self, supervisor_agent):
        self.supervisor_agent = supervisor_agent
        self._intercepted_count: int = 0
        self._passed_count: int = 0

    def __call__(self, message: Message) -> Optional[Message]:
        """拦截器调用入口"""
        # 系统消息直接放行（如终止通知）
        if message.msg_type == MessageType.SYSTEM:
            self._passed_count += 1
            return message

        # SupervisorAgent 自己的消息直接放行
        if message.sender == "SupervisorAgent":
            self._passed_count += 1
            return message

        # 控制消息直接放行
        if message.msg_type == MessageType.CONTROL:
            self._passed_count += 1
            return message

        # 发送给 SupervisorAgent 进行审查
        supervisor_result = self.supervisor_agent.handle_message(message)

        if supervisor_result is None:
            # 审查通过
            self._passed_count += 1
            return message

        # 审查不通过，有反馈
        if supervisor_result.metadata.get("action") == "force_terminate":
            self._intercepted_count += 1
            return None  # 终止时不再路由原始消息

        # 非终止警告：原始消息继续路由，同时将监督反馈发布到总线
        # （v1.14 修复：此前警告反馈被丢弃，目标 Agent 从不知晓）
        self._intercepted_count += 1
        self.supervisor_agent.send_message(
            receiver=supervisor_result.receiver,
            content=supervisor_result.content,
            msg_type=supervisor_result.msg_type,
            metadata=supervisor_result.metadata,
        )
        return message

    def get_stats(self) -> dict:
        return {
            "intercepted": self._intercepted_count,
            "passed": self._passed_count,
            "total": self._intercepted_count + self._passed_count,
        }
