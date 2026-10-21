"""消息总线 — 管理 Agent 间的消息路由，支持审查拦截"""

from collections import defaultdict
from typing import Dict, List, Callable, Optional
from .message import Message, MessageType


class MessageBus:
    """消息总线，支持：
    - Agent 间消息路由
    - 审查 Agent 拦截和过滤
    - 消息历史记录
    - 订阅/发布模式
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._history: List[Message] = []
        self._interceptors: List[Callable] = []  # 审查拦截器
        self._round_count: int = 0
        self._agent_registry: Dict[str, object] = {}

    def register_agent(self, name: str, agent: object) -> None:
        """注册 Agent 到总线"""
        self._agent_registry[name] = agent

    def get_agent(self, name: str) -> Optional[object]:
        return self._agent_registry.get(name)

    def subscribe(self, agent_name: str, callback: Callable[[Message], None]) -> None:
        """订阅消息"""
        self._subscribers[agent_name].append(callback)

    def add_interceptor(self, interceptor: Callable[[Message], Optional[Message]]) -> None:
        """添加消息拦截器（用于审查 Agent）
        拦截器返回 None 表示消息被拦截/丢弃
        返回修改后的 Message 则继续传递
        """
        self._interceptors.append(interceptor)

    def publish(self, message: Message) -> bool:
        """发布消息，返回 True 表示消息已传递，False 表示被拦截"""
        # 通过拦截器链
        for interceptor in self._interceptors:
            result = interceptor(message)
            if result is None:
                self._history.append(message)
                return False  # 被拦截
            message = result

        self._history.append(message)
        self._round_count += 1

        # 路由给接收者
        for callback in self._subscribers.get(message.receiver, []):
            try:
                callback(message)
            except Exception as e:
                print(f"[MessageBus] 路由错误 {message.receiver}: {e}")

        # 同时路由给所有订阅了 "broadcast" 的 Agent
        if message.receiver != "broadcast":
            for callback in self._subscribers.get("broadcast", []):
                try:
                    callback(message)
                except Exception:
                    pass

        return True

    def get_history(self, last_n: int = 20) -> List[Message]:
        return self._history[-last_n:]

    def get_full_history(self) -> List[Message]:
        return self._history.copy()

    def get_round_count(self) -> int:
        return self._round_count

    def clear(self) -> None:
        self._history.clear()
        self._round_count = 0
