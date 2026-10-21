"""管理 Agent — 对话流程管理"""

from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase


class ManagerAgent(BaseAgent):
    """管理 Agent，负责：
    - 理解用户需求
    - 协调分析流程
    - 确认后分派给 Scientist
    - 汇总结果给用户
    """

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str):
        super().__init__(name="Manager", system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url)

    def handle_message(self, message: Message) -> Message:
        content = message.content.lower().strip()

        # 用户确认，进入 Scientist 阶段
        if message.msg_type == MessageType.CONTROL and content in ("approve", "确认", "yes", "ok"):
            self.session.state_machine.force_transition(ConversationPhase.SCIENTIST_ANALYSIS)
            # 确认动作只批准已记录的用户需求，不额外调用模型改写任务。
            self.session.confirmed_task = self.session.user_request
            return Message(
                sender=self.name,
                receiver="Scientist",
                content=self.session.confirmed_task,
                msg_type=MessageType.TEXT,
            )

        # 用户拒绝或终止
        if message.msg_type == MessageType.CONTROL and ("reject" in content or "end" in content or "exit" in content):
            self.session.terminate("用户主动终止")
            return Message(
                sender=self.name,
                receiver="User",
                content="对话已终止。",
                msg_type=MessageType.SYSTEM,
            )

        # Executor 返回结果，汇总报告
        if message.sender == "Executor":
            self.session.state_machine.force_transition(ConversationPhase.REPORTING)
            prompt = f"执行器返回了以下结果，请用中文简洁总结分析结果：\n\n{message.content}"
            summary = self._call_llm(prompt)
            if summary.startswith("ERROR:"):
                return Message(
                    sender=self.name,
                    receiver="User",
                    content=summary,
                    msg_type=MessageType.ERROR,
                )
            return Message(
                sender=self.name,
                receiver="User",
                content=summary,
                msg_type=MessageType.TEXT,
            )

        # Supervisor 监督不通过，需要重做
        if message.sender == "SupervisorAgent" and message.msg_type == MessageType.REVIEW:
            if "block" in message.metadata.get("action", ""):
                return Message(
                    sender=self.name,
                    receiver="Engineer",
                    content=f"审查不通过，请根据以下反馈修改：\n{message.content}",
                    msg_type=MessageType.TEXT,
                )

        # 用户初始输入
        if message.sender == "User":
            self.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
            self.session.user_request = message.content
            prompt = f"用户提出了以下遥感分析需求：\n\n{message.content}\n\n请确认你理解了需求，简要说明分析计划，并询问用户是否确认（回复 approve 确认，或提出修改意见）。注意：不要生成代码，不要捏造结果，所有数据必须来自本地文件。"
            response = self._call_llm(prompt)
            if response.startswith("ERROR:"):
                return Message(
                    sender=self.name,
                    receiver="User",
                    content=response,
                    msg_type=MessageType.ERROR,
                )
            self.session.manager_confirmation = response
            return Message(
                sender=self.name,
                receiver="User",
                content=response,
                msg_type=MessageType.TEXT,
            )

        return None
