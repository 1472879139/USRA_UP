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

        # 用户初始输入或确认阶段的修改意见
        if message.sender == "User":
            is_revision = bool(self.session.user_request) and (
                self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM
            )
            if is_revision:
                previous_confirmation = self.session.manager_confirmation
                self.session.revision_feedback.append(message.content)
                revisions = "\n".join(
                    f"{index}. {feedback}"
                    for index, feedback in enumerate(self.session.revision_feedback, 1)
                )
                prompt = (
                    f"用户原始遥感分析需求：\n{self.session.user_request}\n\n"
                    f"按时间顺序的修改意见：\n{revisions}\n\n"
                    f"上次 Manager 确认摘要（仅供参考）：\n{previous_confirmation}\n\n"
                    "请综合原始需求和所有修改意见重新确认分析计划。后来的明确修改覆盖先前冲突条件，"
                    "未被修改的条件保持不变。只追问真正缺失的信息；不要生成代码、捏造结果或假定不存在的数据。"
                )
            else:
                self.session.user_request = message.content
                prompt = (
                    f"用户提出了以下遥感分析需求：\n\n{message.content}\n\n"
                    "请确认你理解了需求，简要说明分析计划，并询问用户是否确认"
                    "（回复 approve 确认，或提出修改意见）。注意：不要生成代码，"
                    "不要捏造结果，所有数据必须来自本地文件。"
                )
            self.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
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
