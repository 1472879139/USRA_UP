"""Agent 基类 — 所有 Agent 的抽象基础"""

import re
import os
import time
from abc import ABC, abstractmethod
from typing import Optional, List
from openai import OpenAI

from core.message import Message, MessageType
from core.state_machine import AgentState


class BaseAgent(ABC):
    """Agent 基类，提供 LLM 调用、消息处理等通用能力"""

    _include_conversation_history = True

    def __init__(self, name: str, system_prompt: str, model: str, api_key: str, base_url: str, max_tokens: int = 4096):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model
        self.max_tokens = max_tokens
        self.session = None  # 由 Session 注入
        # 失败后由应用显式决定是否重试，避免 SDK 在连接中断且服务端
        # 已处理请求时自动重复提交，从而造成重复计费。
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._conversation_history: List[dict] = []
        self._local_data_cache: dict = {}

    def _call_llm(self, user_message: str, temperature: float = 0.3,
                  stream: bool = False, reasoning_effort: Optional[str] = None) -> str:
        """调用 LLM，统一入口"""
        history = self._conversation_history[-10:] if self._include_conversation_history else []
        messages = [
            {"role": "system", "content": self.system_prompt},
            *history,
            {"role": "user", "content": user_message},
        ]
        start_time = time.time()
        first_chunk_ms = None
        reasoning_chars = 0
        finish_reason = None
        usage = None
        activity_session = self.session if self.session and hasattr(self.session, "begin_llm_call") else None
        activity_token = activity_session.begin_llm_call(self.name) if activity_session else None
        try:
            request = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": self.max_tokens,
            }
            if reasoning_effort is not None:
                request["reasoning_effort"] = reasoning_effort
            if stream:
                chunks = self._client.chat.completions.create(
                    **request, stream=True, stream_options={"include_usage": True},
                )
                parts = []
                for chunk in chunks:
                    if first_chunk_ms is None:
                        first_chunk_ms = (time.time() - start_time) * 1000
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.choices:
                        choice = chunk.choices[0]
                        reasoning_chars += len(getattr(choice.delta, "reasoning_content", None) or "")
                        finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                        part = choice.delta.content
                        if part:
                            parts.append(part)
                content = "".join(parts)
                if finish_reason != "stop":
                    raise ValueError(
                        f"模型流式响应未正常结束 (finish_reason={finish_reason or 'unknown'})"
                    )
                if not content:
                    raise ValueError(
                        f"模型流式响应未包含最终正文 (finish_reason={finish_reason or 'unknown'}, "
                        f"reasoning_chars={reasoning_chars})"
                    )
            else:
                response = self._client.chat.completions.create(**request)
                content = response.choices[0].message.content
                usage = response.usage
            self._conversation_history.append({"role": "user", "content": user_message})
            self._conversation_history.append({"role": "assistant", "content": content})

            # 执行追踪：记录 LLM 调用
            duration_ms = (time.time() - start_time) * 1000
            if self.session and hasattr(self.session, 'tracer'):
                phase = self.session.state_machine.get_current_phase().value if self.session.state_machine else ""
                self.session.tracer.add_event(
                    event_type="llm_call",
                    agent=self.name,
                    phase=phase,
                    data={
                        "model": self.model,
                        "temperature": temperature,
                        "prompt_tokens": usage.prompt_tokens if usage else 0,
                        "completion_tokens": usage.completion_tokens if usage else 0,
                        "total_tokens": usage.total_tokens if usage else 0,
                        "prompt_preview": user_message[:200],
                        "response_preview": content[:500],
                        "streamed": stream,
                        "first_chunk_ms": round(first_chunk_ms, 2) if first_chunk_ms is not None else None,
                        "reasoning_chars": reasoning_chars,
                        "finish_reason": finish_reason,
                    },
                    duration_ms=duration_ms,
                )

            return content
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"[{self.name}] LLM调用失败: {e}"
            root_cause = e.__cause__ or e.__context__
            diagnostics = {
                "model": self.model,
                "error": str(e),
                "error_type": type(e).__name__,
                "cause_type": type(root_cause).__name__ if root_cause else "",
                "request_id": getattr(e, "request_id", None),
                "status_code": getattr(e, "status_code", None),
                "attempts": 1,
                "prompt_chars": len(user_message),
                "history_message_count": len(history),
                "max_tokens": self.max_tokens,
                "streamed": stream,
                "first_chunk_ms": round(first_chunk_ms, 2) if first_chunk_ms is not None else None,
                "reasoning_chars": reasoning_chars,
                "finish_reason": finish_reason,
                "completion_tokens": usage.completion_tokens if usage else None,
            }
            print(
                f"{error_msg} "
                f"[type={diagnostics['error_type']}, "
                f"cause={diagnostics['cause_type'] or 'none'}, "
                f"request_id={diagnostics['request_id'] or 'none'}, attempts=1]"
            )
            # 执行追踪：记录 LLM 错误
            if self.session and hasattr(self.session, 'tracer'):
                self.session.tracer.add_event(
                    event_type="llm_call",
                    agent=self.name,
                    data=diagnostics,
                    duration_ms=duration_ms,
                    status="error",
                )
            return f"ERROR: {error_msg}"
        finally:
            if activity_session:
                activity_session.end_llm_call(activity_token)

    def report_issue(self, issue_type: str, description: str, severity: str = "warning") -> Optional[Message]:
        """
        向 SupervisorAgent 上报问题。任何 Agent 在发现致命问题时均可调用。

        Args:
            issue_type: 问题类型，如 "missing_data", "impossible_task", "data_mismatch"
            description: 问题详细描述
            severity: "warning"（警告但继续）或 "fatal"（建议终止）

        Returns:
            发送的消息或 None
        """
        return self.send_message(
            receiver="SupervisorAgent",
            content=f"[Agent上报] 类型: {issue_type}\n严重程度: {severity}\n描述: {description}",
            msg_type=MessageType.CONTROL,
            metadata={
                "action": "report_issue",
                "issue_type": issue_type,
                "severity": severity,
                "reported_by": self.name,
            },
        )

    def send_message(self, receiver: str, content: str, msg_type: MessageType = MessageType.TEXT,
                     metadata: dict = None, data_sources: list = None) -> Optional[Message]:
        """发送消息到消息总线"""
        if not self.session or not self.session.is_active():
            return None

        msg = Message(
            sender=self.name,
            receiver=receiver,
            content=content,
            msg_type=msg_type,
            metadata=metadata or {},
            data_sources=data_sources or [],
        )
        self.session.bus.publish(msg)
        return msg

    def receive_message(self, message: Message) -> Optional[Message]:
        """接收并处理消息，返回响应消息或 None"""
        if not self.session or not self.session.is_active():
            return None

        self.session.state_machine.set_agent_state(self.name, AgentState.THINKING)
        try:
            response = self.handle_message(message)
            self.session.state_machine.set_agent_state(self.name, AgentState.IDLE)
            return response
        except Exception as e:
            self.session.state_machine.set_agent_state(self.name, AgentState.ERROR)
            return Message(
                sender=self.name,
                receiver=message.sender,
                content=f"处理消息时出错: {e}",
                msg_type=MessageType.ERROR,
            )

    @abstractmethod
    def handle_message(self, message: Message) -> Optional[Message]:
        """处理接收到的消息，子类实现"""
        pass

    # === 数据溯源工具方法 ===

    def scan_data_sources(self, content: str) -> List[str]:
        """扫描内容中引用的数据源"""
        sources = []
        # 检测文件路径
        path_patterns = [
            r'(?:data|results)/[^\s,;:"]+\.(?:tif|tiff|geotiff|nc|hdf|img|png|jpg|jpeg)',
            r'(?:/[\w-]+)+\.(?:tif|tiff|geotiff|nc|hdf)',
        ]
        for pattern in path_patterns:
            sources.extend(re.findall(pattern, content, re.IGNORECASE))

        # 检测 URL
        urls = re.findall(r'https?://[^\s,;:"]+', content)
        sources.extend(urls)

        return list(set(sources))

    def is_local_data(self, source: str) -> bool:
        """判断数据源是否为本地文件"""
        return not source.startswith("http://") and not source.startswith("https://")

    def check_local_file_exists(self, path: str) -> bool:
        """检查本地文件是否存在，包括在 data/ 下递归搜索（大小写不敏感）"""
        if os.path.exists(path):
            return True
        # 在 data/ 根目录下查找（处理 LLM 忽略目录前缀的情况）
        if os.path.exists(os.path.join("data", path)):
            return True
        # 裸文件名：在 data/ 下递归搜索，大小写不敏感（Windows 兼容）
        if not os.path.sep in path and not os.path.isabs(path):
            data_dir = os.path.join(os.getcwd(), "data")
            if os.path.isdir(data_dir):
                path_lower = path.lower()
                for _dirpath, _dirnames, filenames in os.walk(data_dir):
                    for fname in filenames:
                        if fname.lower() == path_lower:
                            return True
        return False

    def reset_conversation(self) -> None:
        """重置对话历史"""
        self._conversation_history.clear()
