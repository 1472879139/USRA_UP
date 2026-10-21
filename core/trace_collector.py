"""执行追踪收集器 — 记录 LLM 调用、代码执行、工具调用的完整链路"""

import time
import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional


@dataclass
class TraceEvent:
    """单条追踪事件"""
    event_id: str
    timestamp: str
    event_type: str       # "llm_call" | "tool_call" | "code_execution"
    agent: str            # 触发该事件的 Agent 名称
    phase: str            # 当前对话阶段
    data: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    status: str = "success"  # "success" | "error" | "warning"


class TraceCollector:
    """执行追踪收集器 — 线程安全，绑定到 Session 生命周期"""

    def __init__(self):
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._tool_names: List[str] = []  # 已知工具函数名，用于 stdout 检测

    def set_tool_names(self, names: List[str]) -> None:
        """注册工具函数名，用于从 stdout 检测工具调用"""
        self._tool_names = names

    def add_event(
        self,
        event_type: str,
        agent: str = "",
        phase: str = "",
        data: dict = None,
        duration_ms: float = 0.0,
        status: str = "success",
    ) -> TraceEvent:
        """添加一条追踪事件，返回创建的 TraceEvent"""
        event = TraceEvent(
            event_id=uuid.uuid4().hex[:8],
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            event_type=event_type,
            agent=agent,
            phase=phase,
            data=data or {},
            duration_ms=round(duration_ms, 2),
            status=status,
        )
        with self._lock:
            self._events.append(event)
        return event

    def get_events(self) -> List[TraceEvent]:
        """获取全部事件"""
        with self._lock:
            return list(self._events)

    def get_summary(self) -> dict:
        """生成追踪摘要"""
        with self._lock:
            events = list(self._events)

        by_type: Dict[str, int] = {}
        by_agent: Dict[str, int] = {}
        total_tokens = 0
        total_tool_calls = 0
        total_tool_detected = 0
        errors = 0
        total_duration_ms = 0.0

        for e in events:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
            by_agent[e.agent] = by_agent.get(e.agent, 0) + 1

            if e.event_type == "llm_call":
                total_tokens += e.data.get("total_tokens", 0)
            elif e.event_type == "tool_call":
                total_tool_calls += 1
            elif e.event_type == "code_execution":
                total_tool_detected = e.data.get("tools_detected", 0)

            if e.status == "error":
                errors += 1

            total_duration_ms += e.duration_ms

        return {
            "total_events": len(events),
            "by_type": by_type,
            "by_agent": by_agent,
            "total_llm_tokens": total_tokens,
            "total_tool_calls": total_tool_calls,
            "total_tools_detected_from_stdout": total_tool_detected,
            "errors": errors,
            "total_duration_ms": round(total_duration_ms, 2),
            "elapsed_seconds": round(time.time() - self._start_time, 1),
        }

    def get_tree(self) -> List[dict]:
        """返回按 Agent 分组的树形结构，供 GUI 展示"""
        with self._lock:
            events = list(self._events)

        tree: Dict[str, List[dict]] = {}
        for e in events:
            if e.agent not in tree:
                tree[e.agent] = []

            node = {
                "event_id": e.event_id,
                "time": e.timestamp,
                "type": e.event_type,
                "phase": e.phase,
                "duration_ms": e.duration_ms,
                "status": e.status,
                "data": e.data,
            }
            tree[e.agent].append(node)

        return [{"agent": agent, "events": evts} for agent, evts in tree.items()]

    def detect_tools_in_stdout(self, stdout: str) -> List[str]:
        """从子进程 stdout 中检测工具函数调用"""
        detected = []
        stdout_lower = stdout.lower()
        for name in self._tool_names:
            if name.lower() in stdout_lower:
                detected.append(name)
        return list(set(detected))

    def export_json(self, indent: int = 2) -> str:
        """导出完整追踪数据为 JSON 字符串"""
        with self._lock:
            events = list(self._events)

        data = {
            "summary": self.get_summary(),
            "events": [asdict(e) for e in events],
        }
        return json.dumps(data, ensure_ascii=False, indent=indent)

    def clear(self) -> None:
        """清空所有事件"""
        with self._lock:
            self._events.clear()
            self._start_time = time.time()
