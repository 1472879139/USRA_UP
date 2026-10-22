"""管理 Agent — 对话流程管理"""

from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase
from tools.data_context import load_data_context, render_data_context
from tools.result_audit import report_conflicts
from tools.task_contract import ensure_task_contract, render_task_contract


class ManagerAgent(BaseAgent):
    """管理 Agent，负责：
    - 理解用户需求
    - 协调分析流程
    - 确认后分派给 Scientist
    - 汇总结果给用户
    """

    # 每次已显式重建完整原需求、修订和数据快照，避免重复发送过期快照。
    _include_conversation_history = False

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str):
        super().__init__(name="Manager", system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url)

    def _build_confirmed_task(self) -> str:
        request = self.session.user_request
        revisions = self.session.revision_feedback
        if not revisions:
            return request
        numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(revisions, 1))
        return (
            f"用户原始需求：\n{request}\n\n"
            f"用户修改意见（按时间顺序）：\n{numbered}\n\n"
            "Manager 最新确认摘要（辅助理解，不能覆盖用户原文）：\n"
            f"{self.session.manager_confirmation}\n\n"
            "执行规则：后来的明确修改覆盖先前冲突条件；未被修改的条件保持不变。"
        )

    def _data_informed_prompt(self, prompt: str, context=None) -> str:
        self.session.data_context = (context if context is not None else
                                     load_data_context(previous=self.session.data_context))
        self.session.tracer.add_event(
            event_type="data_preflight", agent=self.name,
            data={"snapshot": self.session.data_context},
        )
        ensure_task_contract(self.session)
        return prompt + "\n\n" + render_data_context(self.session.data_context) + render_task_contract(self.session) + (
            "\n\n确认时区分：用户明确要求、已读取数据事实、系统限制、待确认假设。"
            "只用已核验文件判断能力；指定文件不存在/不可读时明确指出，不自动换用其他文件。"
            "若统计 ROI 未核实，说明采用影像有效区还是行政区，不能承诺完整行政区面积。"
            "用户要求各个/全部原始波段时，列明实际全部波段，任何排除都需理由；"
            "用户明确禁用衍生特征且覆盖范围有歧义时，再澄清是否包括簇命名运算，不额外要求用户选择算法。"
        )

    def handle_message(self, message: Message) -> Message:
        content = message.content.lower().strip()

        # 用户确认，进入 Scientist 阶段
        if message.msg_type == MessageType.CONTROL and content in ("approve", "确认", "yes", "ok"):
            if self.session.data_context:
                latest = load_data_context(previous=self.session.data_context)
                if latest.get("signature") != self.session.data_context.get("signature"):
                    self.session.confirmed_task = ""
                    self.session.state_machine.force_transition(ConversationPhase.MANAGER_CONFIRM)
                    feedback = "\n".join(f"{i}. {text}" for i, text in enumerate(self.session.revision_feedback, 1))
                    prompt = self._data_informed_prompt(
                        "用户等待确认期间本地数据发生变化。请依据新数据重新说明可行性、变化和待确认事项。"
                        f"\n原始需求：{self.session.user_request}\n有序修改意见：\n{feedback}", latest,
                    )
                    response = self._call_llm(prompt)
                    if response.startswith("ERROR:"):
                        return Message(sender=self.name, receiver="User", content=response, msg_type=MessageType.ERROR)
                    self.session.manager_confirmation = response
                    return Message(sender=self.name, receiver="User",
                                   content="输入数据已变化，请重新审核以下计划：\n\n" + response,
                                   metadata={"requires_confirmation": True})
            self.session.state_machine.force_transition(ConversationPhase.SCIENTIST_ANALYSIS)
            # 确认动作只批准已记录的用户需求，不额外调用模型改写任务。
            self.session.confirmed_task = self._build_confirmed_task()
            ensure_task_contract(self.session)
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
            audit = message.metadata.get("result_audit") or self.session.result_audit
            if audit.get("status") == "failed":
                return Message(sender=self.name, receiver="User", msg_type=MessageType.ERROR,
                               content=audit.get("facts", "结果核验失败"))
            prompt = (
                f"已确认用户要求：\n{self.session.confirmed_task or self._build_confirmed_task()}\n\n"
                f"本次任务结果目录：{self.session.result_relative_dir}\n\n"
                f"{render_data_context(self.session.data_context)}\n\n"
                f"执行器返回了以下结果，请用中文简洁总结分析结果：\n\n{message.content}\n\n"
                f"{audit.get('facts', '尚无机器核验结果，不能声称已核验正确。')}\n\n"
                "严格依据统计表比较全部类别，不得忽略其他类后声称植被占比最高。"
                "自动阈值/打分映射不能描述为人工判读；数值计算真实不等于语义类别正确。"
                "有待复核项时明确标为候选结果，不能用执行成功或文件生成代替精度验证。"
                "先用两三句普通话回答发现了什么、在哪里、哪些尚不能确认；专业方法放到后面。"
                "不得把文件检查通过称为识别准确，也不得把模型自评当作可信概率。"
                + render_task_contract(self.session)
            )
            summary = self._call_llm(prompt)
            if summary.startswith("ERROR:"):
                return Message(
                    sender=self.name,
                    receiver="User",
                    content=summary,
                    msg_type=MessageType.ERROR,
                )
            if audit:
                if report_conflicts(summary, audit):
                    summary = "模型报告与核验统计不一致，以下为核验后的结果说明：\n\n" + audit["facts"]
                else:
                    summary += "\n\n" + audit.get("facts", "")
            from tools.result_delivery import build_result_delivery, render_plain_delivery
            from tools.io_kit import _get_project_root
            delivery = build_result_delivery(self.session, _get_project_root(), report=summary)
            self.session.result_delivery = delivery
            return Message(
                sender=self.name,
                receiver="User",
                content=render_plain_delivery(delivery) + "\n\n[专业详情]\n" + summary,
                msg_type=MessageType.TEXT,
                metadata={"purpose": "result_report"},
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
            prompt = self._data_informed_prompt(prompt)
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
