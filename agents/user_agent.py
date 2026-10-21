"""用户代理 — 代表用户与系统交互"""

from .base_agent import BaseAgent
from core.message import Message, MessageType


class UserAgent(BaseAgent):
    """用户代理，负责：
    - 接收用户的自然语言输入
    - 向系统转发用户请求
    - 向用户展示系统响应
    """

    def __init__(self, model: str, api_key: str, base_url: str):
        super().__init__(
            name="User",
            system_prompt="你是用户代理，负责在用户和系统之间传递消息。你不应该生成分析内容。",
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    def handle_message(self, message: Message) -> Message:
        if message.msg_type == MessageType.SYSTEM and message.metadata.get("action") == "terminate":
            return Message(
                sender=self.name,
                receiver="broadcast",
                content=f"用户收到终止通知: {message.content}",
                msg_type=MessageType.CONTROL,
            )
        return None

    def submit_request(self, user_input: str) -> Message:
        """用户提交分析请求"""
        return self.send_message(
            receiver="Manager",
            content=user_input,
            msg_type=MessageType.TEXT,
        )

    def approve(self) -> Message:
        """用户确认"""
        return self.send_message(
            receiver="Manager",
            content="approve",
            msg_type=MessageType.CONTROL,
        )

    def reject(self, reason: str = "") -> Message:
        """用户拒绝"""
        return self.send_message(
            receiver="Manager",
            content=f"reject: {reason}" if reason else "reject",
            msg_type=MessageType.CONTROL,
        )

    def terminate(self) -> Message:
        """用户主动终止"""
        return self.send_message(
            receiver="broadcast",
            content="用户请求终止会话",
            msg_type=MessageType.CONTROL,
            metadata={"action": "user_terminate"},
        )
