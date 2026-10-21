"""统一消息格式，所有 Agent 间通信的基础数据结构"""

from dataclasses import dataclass, field
from enum import Enum, auto
from datetime import datetime
from typing import Any, Optional
import uuid


class MessageType(Enum):
    """消息类型枚举"""
    TEXT = auto()           # 普通文本消息
    CODE = auto()           # 代码块
    RESULT = auto()         # 执行结果
    ERROR = auto()          # 错误信息
    CONTROL = auto()        # 控制指令（approve/terminate/skip等）
    REVIEW = auto()         # 审查消息
    SYSTEM = auto()         # 系统通知
    DATA_REF = auto()       # 数据引用（用于溯源）


@dataclass
class Message:
    """标准化消息格式"""
    sender: str
    receiver: str
    content: str
    msg_type: MessageType = MessageType.TEXT
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)
    # 数据溯源字段
    data_sources: list = field(default_factory=list)  # 引用的数据源列表
    is_fabricated: bool = False  # 是否被检测为捏造数据

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "content": self.content,
            "msg_type": self.msg_type.name,
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "data_sources": self.data_sources,
        }

    def __repr__(self) -> str:
        return f"[{self.msg_type.name}] {self.sender} → {self.receiver}: {self.content[:80]}"
