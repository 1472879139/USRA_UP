"""有预算的结构化工具调用循环；只调度，不执行模型生成的代码。"""
from copy import deepcopy
import json
import re
import time


class ToolLoopError(RuntimeError):
    """调用预算、接口或会话状态导致工具循环停止。"""


def strict_json(text: str) -> dict:
    """拒绝重复字段、非有限数字以及非对象参数。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON包含重复字段")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("JSON包含非有限数字")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("工具参数必须为JSON对象")
    return value


class ToolCallLoop:
    """将执行结果按tool消息回传；结束必须得到执行器认可的finish_task。"""

    def __init__(self, client, model, *, max_rounds=12, max_calls=24,
                 max_seconds=300, max_tokens=4096, active=None, notify=None, on_text=None, record=None, max_argument_chars=16000):
        for name, value in [("max_rounds", max_rounds), ("max_calls", max_calls),
                            ("max_seconds", max_seconds), ("max_tokens", max_tokens), ("max_argument_chars", max_argument_chars)]:
            if type(value) is not int or value < 1:
                raise ValueError(name + "必须为正整数")
        self.client, self.model = client, model
        self.max_rounds, self.max_calls = max_rounds, max_calls
        self.max_seconds, self.max_tokens = max_seconds, max_tokens
        self.active = active or (lambda: True)
        self.notify = notify or (lambda event: None)
        self.max_argument_chars = max_argument_chars
        self.on_text = on_text
        self.record = record or (lambda event: None)
        self.events = []
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def run(self, messages: list[dict], schemas: list[dict], execute) -> dict:
        """执行白名单回调；API异常不重试，工具错误可由下一轮重新规划。"""
        messages = deepcopy(messages)
        started, calls, seen_ids = time.monotonic(), 0, set()
        for turn in range(1, self.max_rounds + 1):
            remaining = self.max_seconds - (time.monotonic() - started)
            if not self.active():
                raise ToolLoopError("用户已终止，未继续工具调用")
            if remaining <= 0 or len(json.dumps(messages, ensure_ascii=False)) > 250000:
                raise ToolLoopError("工具循环已达到时间或上下文上限")
            self.notify({"event": "planning", "turn": turn})
            self.usage["requests"] += 1
            request = {"model": self.model, "messages": deepcopy(messages), "tools": schemas,
                       "max_tokens": self.max_tokens, "stream": False, "timeout": min(60, remaining)}
            self.record({"event": "request", "turn": turn, "request": deepcopy(request)})
            self.notify({"event": "request_started", "turn": turn})
            try:
                reply = self.client.chat.completions.create(**request)
                usage = getattr(reply, "usage", None)
                for key in ("prompt_tokens", "completion_tokens"):
                    self.usage[key] += getattr(usage, key, 0) or 0
                choice = reply.choices[0]
                msg = choice.message
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                self.record({"event": "request_error", "turn": turn, "error_type": type(exc).__name__, "status": status})
                raise ToolLoopError("工具调用接口未完成：" + type(exc).__name__ +
                                    (f"（HTTP {status}）" if isinstance(status, int) else "") +
                                    "；请核对当前接口的tools兼容性或连接状态；未自动重试或更换模型") from exc
            finally:
                self.notify({"event": "request_finished", "turn": turn})
            tool_calls = getattr(msg, "tool_calls", None) or []
            assistant = {"role": "assistant", "content": msg.content or ""}
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning is not None:
                assistant["reasoning_content"] = reasoning
            self.record({"event": "response", "turn": turn, "finish_reason": choice.finish_reason,
                         "assistant": {"role": "assistant", "content": msg.content or "", "tool_calls": [
                             {"id": call.id, "type": call.type, "function": {"name": call.function.name, "arguments": call.function.arguments}}
                             for call in tool_calls]}, "has_reasoning_content": reasoning is not None})
            if not self.active():
                raise ToolLoopError("用户已终止，未执行模型返回的工具")
            if choice.finish_reason not in ("stop", "tool_calls"):
                raise ToolLoopError("模型响应未完整结束：" + str(choice.finish_reason))
            if not tool_calls:
                answer = self.on_text(msg.content or "") if self.on_text else None
                if answer is not None:
                    return answer
                messages.append(assistant)
                messages.append({"role": "user", "content": "请使用finish_task提交当前证据或说明未完成；纯文字不能作为执行成功收据。"})
                continue
            if calls + len(tool_calls) > self.max_calls:
                raise ToolLoopError("工具调用次数达到上限，保留已生成文件，尚无最终交付")
            normalized = []
            for call in tool_calls:
                if (call.type != "function" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", call.function.name) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call.id)
                        or call.id in seen_ids):
                    raise ToolLoopError("工具请求类型或调用ID不合法，未执行该批请求")
                seen_ids.add(call.id)
                normalized.append({"id": call.id, "type": "function", "function": {
                    "name": call.function.name, "arguments": call.function.arguments}})
            assistant["tool_calls"] = normalized
            messages.append(assistant)
            finish_in_batch = len(normalized) > 1 and any(c["function"]["name"] in {"finish_task", "request_clarification"} for c in normalized)
            for call in normalized:
                if not self.active() or time.monotonic() - started >= self.max_seconds:
                    raise ToolLoopError("任务已终止或超时，未继续执行工具")
                calls += 1
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                self.notify({"event": "tool_start", "name": name, "call": calls})
                try:
                    if not isinstance(arguments, str) or len(arguments) > self.max_argument_chars:
                        raise ValueError("工具参数过大或类型不符")
                    args = strict_json(arguments)
                    if finish_in_batch:
                        raise ValueError("finish_task必须单独提交，先等待此前工具结果")
                    result = execute(name, args)
                    if not isinstance(result, dict):
                        raise ValueError("工具必须返回结构化对象")
                    content = json.dumps(result, ensure_ascii=False, allow_nan=False)
                    if len(content) > 48000:
                        raise ValueError("工具结果过大，需返回摘要和产物ID")
                except ToolLoopError:
                    raise
                except Exception as exc:
                    result = {"ok": False, "error": {"code": "invalid_tool_request", "message":
                              str(exc) if isinstance(exc, ValueError) else type(exc).__name__}}
                    content = json.dumps(result, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
                event = {"turn": turn, "call": calls, "name": name, "arguments": arguments,
                         "result": deepcopy(result)}
                self.events.append(event)
                self.record({"event": "tool_result", **deepcopy(event)})
                self.notify({"event": "tool_end", "name": name, "ok": result.get("ok"), "call": calls})
                if result.get("ok") and result.get("_finish") and name in {"finish_task", "request_clarification"}:
                    return result
        raise ToolLoopError("DeepSeek已达到规划轮次上限，保留过程记录，未宣称任务完成")
