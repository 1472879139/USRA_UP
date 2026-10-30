"""科学家 Agent — 定义遥感分析研究问题"""

from copy import deepcopy

from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase
from tools.data_context import render_data_context
from tools.task_contract import ensure_task_contract, render_task_contract


class ScientistAgent(BaseAgent):
    """科学家 Agent，负责：
    - 将用户需求转化为研究问题
    - 定义分析方法和预期输出
    - 只使用本地数据
    """

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str):
        super().__init__(name="Scientist", system_prompt=system_prompt, model=model,
                         api_key=api_key, base_url=base_url, max_tokens=8192)

    def handle_message(self, message: Message) -> Message:
        self.session.state_machine.force_transition(ConversationPhase.SCIENTIST_ANALYSIS)
        if self.session.user_request or self.session.confirmed_task:
            ensure_task_contract(self.session)
        from tools.tool_agent_workflow import enabled, make_message
        if enabled(self.session):
            try:
                return make_message(self.session)
            except (ValueError, OSError) as exc:
                return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR, content=str(exc))
        if self.session.task_capability.get("status") == "blocked":
            return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR,
                           content="\n".join(self.session.task_capability["reasons"]))

        from tools.remote_models import select_remote_tool, confirmation_text
        self.session.tool_plan = {}
        self.session.remote_tool_plan = {}
        try:
            remote = select_remote_tool(self.session)
        except (OSError, ValueError, TypeError):
            return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR,
                           content="本地GPU工具配置不完整，请检查gpu.local.json；尚未执行分析。")
        if remote:
            if not remote["inputs"]:
                self.session.task_capability["status"] = "blocked"
                return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR,
                               content=confirmation_text(remote))
            from tools.remote_models import describe_remote_plan, confirmation_text
            describe_remote_plan(self.session, remote)
            self.session.remote_tool_plan = deepcopy(remote)
            self.session.tracer.add_event("tool_plan", agent=self.name, data=remote)
            return Message(sender=self.name, receiver="Engineer", msg_type=MessageType.TEXT,
                           content=confirmation_text(remote),
                           metadata={"remote_tool_request": deepcopy(remote)})

        from tools.water_workflow import is_basic_water_request
        from tools.installed_workflow import select_installed_tool
        self.session.tool_plan = select_installed_tool(self.session) or {}
        if self.session.tool_plan:
            names = [item["file_name"] for item in self.session.tool_plan["inputs"]]
            self.session.task_capability["reasons"] = [
                reason for reason in self.session.task_capability.get("reasons", []) if "专业识别模型" not in reason]
            is_lst = self.session.tool_plan['name'] == 'lst_rte'
            description = ("已接入固定地表温度重建工具；输出摄氏度图、有效区温度统计和相对高温位置，误差须交卷后独立评价。"
                           if is_lst else "已接入固定水体指数工具；候选类别的准确性须以独立评价为准。")
            self.session.task_capability["reasons"].append(description)
            self.session.tracer.add_event("tool_plan", agent=self.name, data=self.session.tool_plan)
            return Message(sender=self.name, receiver="Engineer", msg_type=MessageType.TEXT,
                           content=description + "波段与编码已经读取核对；生成阶段不读取参考答案。输入：" + "、".join(names),
                           metadata={"tool_request": deepcopy(self.session.tool_plan)})
        if is_basic_water_request(self.session) and not self.session.task_contract.get("inputs"):
            return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR,
                           content="尚未明确使用哪幅影像。请在上方选择文件，或给出不重名的数据目录名称。")

        prompt = f"""用户需求已确认，Manager 的转交信息如下：

{message.content}

{render_data_context(self.session.data_context)}

{render_task_contract(self.session)}

请作为遥感科学家，定义这个分析任务：
1. 问题陈述：明确要分析什么
2. 数据集：根据上方 [系统提示] 中提供的实际波段元数据（文件名、波段数量、波段名称），描述使用的数据。波段用语义名称（如 Red、NIR、SWIR）而非硬编码的数字索引
3. 分析方法：具体的计算步骤，波段参数用语义名称指定（如"使用 Red 和 NIR 波段"），由 Engineer 从实际文件元数据中确定对应的 0-based 索引
4. 预期输出：需要生成什么结果

重要约束：
- 只能使用本地数据文件（见 [系统提示] 中的文件列表和波段元数据）
- 不要建议从网络下载数据
- 不要编写代码（这是 Engineer 的任务）
- 不要捏造或模拟数据
- 不要硬编码波段索引号（如"Band 4, index 3"），使用语义名称（如"Red 波段"）代替"""

        response = self._call_llm(prompt, stream=True, reasoning_effort="low")
        if response.startswith("ERROR:"):
            return Message(
                sender=self.name,
                receiver="Manager",
                content=response,
                msg_type=MessageType.ERROR,
            )

        # 扫描数据引用
        data_sources = self.scan_data_sources(response)

        self.session.state_machine.force_transition(ConversationPhase.ENGINEER_CODING)
        return Message(
            sender=self.name,
            receiver="Engineer",
            content=response,
            msg_type=MessageType.TEXT,
            data_sources=data_sources,
        )
