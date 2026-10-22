"""科学家 Agent — 定义遥感分析研究问题"""

from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase
from tools.data_context import render_data_context


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

        prompt = f"""用户需求已确认，Manager 的转交信息如下：

{message.content}

{render_data_context(self.session.data_context)}

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
