"""USRA_UP 可视化界面 — 基于 Flask 的三栏暗色主题 GUI"""

import os
import sys

# Windows 编码修复
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdin.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    os.environ.setdefault('PYTHONUTF8', '1')

import json
import re
import time
import threading
from datetime import datetime
from queue import Queue, Empty

from flask import Flask, render_template, request, jsonify, send_file

from core.message import Message, MessageType
from core.session import Session
from core.state_machine import ConversationPhase
from agents.user_agent import UserAgent
from agents.manager_agent import ManagerAgent
from agents.scientist_agent import ScientistAgent
from agents.engineer_agent import EngineerAgent
from agents.executor_agent import ExecutorAgent
from agents.supervisor_agent import SupervisorAgent
from agents.proxy_agent import ProxyAgent


def load_prompt(path: str) -> str:
    """加载提示词文件"""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def load_config() -> dict:
    """加载配置"""
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


class URSAGUI:
    """USRA_UP 可视化应用控制器"""

    # 中文阶段名映射
    PHASE_NAMES = {
        "初始化": "init",
        "用户输入": "input",
        "Manager 确认": "confirm",
        "Scientist 分析": "scientist",
        "Engineer 编码": "engineer",
        "Executor 执行": "executor",
        "监督检查": "supervisor",
        "生成报告": "done",
        "完成": "done",
        "已终止": "done",
    }

    def __init__(self):
        self.config = load_config()
        self.model = self.config["model"]
        self.api_key = self.config["api_key"]
        self.base_url = self.config["base_url"]

        # 清理上次运行的临时文件
        import shutil
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "temp")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

        self.session: Session = None
        self.agents: dict = {}
        self.proxy: ProxyAgent = None
        self.ui_queue: Queue = Queue()
        self._ui_lock = threading.RLock()
        self._ui_events: list[dict] = []
        self._ui_seq = 0
        self._running = False
        self._thread: threading.Thread = None
        self._chat_history: list = []
        self._monitor_history: list = []
        self._raw_history: list = []
        self._trace_events: list = []
        # 启动时扫描 data/ 目录
        self._data_summary_html: str = self._build_data_summary_html()
        self._data_analysis_html: str = self._build_data_analysis_html()

    def _build_data_summary_html(self) -> str:
        """生成 data/ 目录文件摘要的 HTML"""
        from tools.io_kit import scan_data_directory
        scan = scan_data_directory()
        if not scan["success"]:
            return f"<p style='color:#999;'>数据扫描失败: {scan['message']}</p>"

        data = scan["data"]
        if data["file_count"] == 0:
            return "<p style='color:var(--orange);'>&#9888; data/ 目录中未发现栅格文件 (.tif/.tiff)，请放入数据后刷新。</p>"

        html = ""
        for f in data["files"]:
            html += "<div class='data-file-card'>"
            html += f"<div class='fname'>&#128193; {f['file_name']}</div>"
            html += f"<div class='meta'>尺寸: {f['width']}×{f['height']} | 波段: {f['bands']} | 类型: {f['dtype']}</div>"
            if f["crs"]:
                html += f"<div class='meta'>坐标系: {f['crs']}</div>"
            if f["resolution"]:
                html += f"<div class='meta'>分辨率: {f['resolution']}</div>"
            if f["band_names"] and not f["band_names"][0].startswith("读取失败"):
                names = [n for n in f["band_names"] if n and n != "None"]
                if names and not all(n.startswith("Band ") for n in names):
                    html += f"<div class='meta'>波段: {', '.join(names)}</div>"
            html += "</div>"
        return html

    def _build_data_analysis_html(self) -> str:
        """使用 SupervisorAgent 智能分析 data/ 目录文件的遥感分析用途"""
        try:
            from tools.io_kit import scan_data_directory
            from agents.supervisor_agent import SupervisorAgent

            scan = scan_data_directory()
            if not scan["success"] or scan["data"]["file_count"] == 0:
                return ""

            prompts = self._load_prompts()
            temp_supervisor = SupervisorAgent(
                system_prompt=prompts["supervisor"],
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
            )
            analysis = temp_supervisor.analyze_data_potential(scan)
            body_html = self._markdown_to_html(analysis)

            html = """
            <div class="data-analysis-card">
                <div class="title"><span>&#129504;</span> 智能分析建议</div>
                <div class="content">""" + body_html + """</div>
            </div>"""
            return html
        except Exception as e:
            return f"<div style='color:var(--orange);font-size:12px;'>智能分析生成失败: {e}</div>"

    def _markdown_to_html(self, text: str) -> str:
        """将 LLM 返回的简易 Markdown 转为 HTML"""
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = text.strip().split("\n")
        html_parts = []
        in_list = False

        for line in lines:
            line = line.strip()
            if not line:
                if in_list:
                    html_parts.append("</ul>")
                    in_list = False
                html_parts.append("<div style='margin:6px 0;'></div>")
                continue

            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)

            if line.startswith("## "):
                if in_list:
                    html_parts.append("</ul>")
                    in_list = False
                html_parts.append(
                    f"<div style='color:var(--accent);font-size:15px;font-weight:700;"
                    f"margin:12px 0 6px 0;padding-bottom:4px;"
                    f"border-bottom:1px solid var(--border);'>{line[3:]}</div>"
                )
                continue

            if line.startswith("### "):
                if in_list:
                    html_parts.append("</ul>")
                    in_list = False
                html_parts.append(
                    f"<div style='color:var(--text-dim);font-size:14px;font-weight:600;"
                    f"margin:8px 0 4px 0;'>{line[4:]}</div>"
                )
                continue

            ol_match = re.match(r"^(\d+)[\.\、]\s?(.+)", line)
            if ol_match:
                if not in_list:
                    html_parts.append("<ul style='padding-left:16px;margin:4px 0;'>")
                    in_list = True
                html_parts.append(
                    f"<li style='color:var(--text);margin:3px 0;list-style-type:decimal;'>{ol_match.group(2)}</li>"
                )
                continue

            ul_match = re.match(r"^[-•]\s(.+)", line)
            if ul_match:
                if not in_list:
                    html_parts.append("<ul style='padding-left:16px;margin:4px 0;'>")
                    in_list = True
                html_parts.append(
                    f"<li style='color:var(--text);margin:3px 0;'>{ul_match.group(1)}</li>"
                )
                continue

            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<div style='color:var(--text);margin:2px 0;'>{line}</div>")

        if in_list:
            html_parts.append("</ul>")

        return "\n".join(html_parts)

    def _load_prompts(self) -> dict:
        """加载各 Agent 的系统提示词"""
        base = "prompts"
        return {
            "manager": load_prompt(os.path.join(base, "manager_prompt.txt")),
            "scientist": load_prompt(os.path.join(base, "scientist_prompt.txt")),
            "engineer": load_prompt(os.path.join(base, "engineer_prompt.txt")),
            "executor": load_prompt(os.path.join(base, "executor_prompt.txt")),
            "supervisor": load_prompt(os.path.join(base, "supervisor_prompt.txt")),
        }

    def _create_agents(self) -> dict:
        prompts = self._load_prompts()
        kw = {"model": self.model, "api_key": self.api_key, "base_url": self.base_url}
        engineer_max_tokens = self.config.get("engineer_max_tokens", 32768)
        code_timeout = self.config.get("code_timeout", 300)

        agents = {
            "user": UserAgent(**kw),
            "manager": ManagerAgent(system_prompt=prompts["manager"], **kw),
            "scientist": ScientistAgent(system_prompt=prompts["scientist"], **kw),
            "engineer": EngineerAgent(system_prompt=prompts["engineer"], max_tokens=engineer_max_tokens, **kw),
            "executor": ExecutorAgent(system_prompt=prompts["executor"], code_timeout=code_timeout, **kw),
            "supervisor": SupervisorAgent(system_prompt=prompts["supervisor"], **kw),
        }
        return agents

    def _setup_session(self):
        """初始化会话和消息路由"""
        self.session = Session()
        self.session.start()

        for agent in self.agents.values():
            self.session.register_agent(agent)

        bus = self.session.bus

        # 监督拦截器
        self.proxy = ProxyAgent(self.agents["supervisor"])
        bus.add_interceptor(self.proxy)

        # UI 通知回调
        def ui_notify(msg: Message):
            self.ui_queue.put({"type": "message", "data": msg})

        bus.subscribe("broadcast", ui_notify)

    def _process_flow(self, user_input: str, auto_pilot: bool = False):
        """后台线程：运行分析流程"""
        try:
            supervisor: SupervisorAgent = self.agents["supervisor"]
            if auto_pilot:
                supervisor.enable_auto_pilot()
                self.ui_queue.put({"type": "system", "data": "&#9889; 一键托管模式已启用 — 系统将自动择优，加速流程"})

            user: UserAgent = self.agents["user"]
            manager: ManagerAgent = self.agents["manager"]
            scientist: ScientistAgent = self.agents["scientist"]
            engineer: EngineerAgent = self.agents["engineer"]
            executor: ExecutorAgent = self.agents["executor"]
            sm = self.session.state_machine

            content_lower = user_input.strip().lower()
            is_continue = content_lower in ('approve', '确认', 'yes', 'ok')
            is_end = content_lower in ('end', 'exit', '终止')

            # === 终止命令 ===
            if is_end:
                self.session.terminate("用户主动终止")
                self.ui_queue.put({"type": "system", "data": "会话已终止。"})
                return

            # === 在确认阶段收到用户回复 ===
            if sm.get_current_phase() == ConversationPhase.MANAGER_CONFIRM:
                if is_continue:
                    # 先让 Manager 生成并持久化正式交接任务；approve 仅是控制指令，
                    # 不能直接作为 Scientist 的分析需求。
                    approval = manager.handle_message(Message(
                        sender="User", receiver="Manager",
                        content=user_input, msg_type=MessageType.CONTROL,
                    ))
                    if not approval or approval.msg_type == MessageType.ERROR:
                        self.ui_queue.put({"type": "error", "data": approval.content if approval else "Manager 未返回确认结果"})
                        self.session.terminate("Manager 确认失败")
                        return
                    if approval.receiver != "Scientist":
                        self.ui_queue.put({"type": "message", "data": approval})
                        return
                    self._run_analysis_pipeline(manager, scientist, engineer, executor, supervisor, sm, user_input)
                else:
                    self.ui_queue.put({"type": "system", "data": "收到修改意见，Manager 重新确认..."})
                    self.ui_queue.put({"type": "phase", "data": "Manager 确认"})
                    msg = Message(
                        sender="User", receiver="Manager",
                        content=user_input, msg_type=MessageType.TEXT,
                    )
                    resp = manager.handle_message(msg)
                    if resp and resp.msg_type == MessageType.ERROR:
                        self.ui_queue.put({"type": "error", "data": resp.content})
                        self.session.terminate("Manager LLM 调用失败")
                        return
                    if resp:
                        try:
                            self.ui_queue.put({"type": "message", "data": resp})
                        except Exception:
                            pass
                return

            # === 新请求 ===
            self.ui_queue.put({"type": "phase", "data": "用户输入"})
            msg = user.submit_request(user_input)

            self.ui_queue.put({"type": "phase", "data": "Manager 确认"})
            resp = manager.handle_message(msg)
            if resp and resp.msg_type == MessageType.ERROR:
                self.ui_queue.put({"type": "error", "data": resp.content})
                self.session.terminate("Manager LLM 调用失败")
                return
            if resp:
                try:
                    self.ui_queue.put({"type": "message", "data": resp})
                except Exception:
                    pass
            if not self.session.is_active():
                return

            if auto_pilot or is_continue:
                self.ui_queue.put({"type": "system", "data": "&#8594; 进入自动分析管线（跳过冗余确认）"})
                sm.force_transition(ConversationPhase.SCIENTIST_ANALYSIS)
                self._run_analysis_pipeline(manager, scientist, engineer, executor, supervisor, sm, user_input)
            else:
                self.ui_queue.put({"type": "system", "data": "&#128070; 请审核 Manager 的计划。点击「确认」继续分析，或输入修改意见后点「发送」。"})
                sm.force_transition(ConversationPhase.MANAGER_CONFIRM)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.ui_queue.put({"type": "error", "data": f"流程异常: {e}\n{tb[-300:]}"})
            if self.session and self.session.is_active():
                self.session.terminate("流程异常")
        finally:
            self._running = False
            self.ui_queue.put({"type": "done", "data": ""})

    def _run_analysis_pipeline(self, manager, scientist, engineer, executor, supervisor, sm, user_input=""):
        """执行分析管线：Scientist → Engineer → 监督 → Executor → 报告"""
        try:
            self.ui_queue.put({"type": "phase", "data": "Scientist 分析"})

            from tools.io_kit import scan_data_directory
            scan = scan_data_directory()
            confirmed_task = (
                self.session.confirmed_task
                or self.session.user_request
                or self.session.manager_confirmation
            )
            file_selection = supervisor.select_relevant_files(confirmed_task, scan)
            file_path_map = {}
            for f in scan.get("data", {}).get("files", []):
                fname = f.get("file_name", "")
                fpath = f.get("file_path", "")
                if fname and fpath:
                    try:
                        file_path_map[fname] = os.path.relpath(fpath, os.getcwd())
                    except ValueError:
                        file_path_map[fname] = fname
            selected_names = [f['file_name'] for f in file_selection.get('relevant_files', [])]
            selected_files = [file_path_map.get(n, n) for n in selected_names]
            self.ui_queue.put({"type": "system", "data": f"&#128193; 智能匹配文件: {', '.join(selected_files)}"})

            context_parts = [f"已确认任务: {confirmed_task}"] if confirmed_task else []
            context_parts.append(f"[系统提示] 相关数据文件: {', '.join(selected_files)}")
            context = "\n\n".join(context_parts) or "分析遥感数据"
            sci_msg = Message(
                sender="Manager", receiver="Scientist",
                content=context, msg_type=MessageType.TEXT,
            )
            sci_resp = scientist.handle_message(sci_msg)
            if not sci_resp or sci_resp.msg_type == MessageType.ERROR:
                reason = sci_resp.content if sci_resp else "Scientist 未返回分析结果"
                self.ui_queue.put({"type": "error", "data": reason})
                self.session.terminate(reason)
                return
            if sci_resp:
                self.ui_queue.put({"type": "message", "data": sci_resp})
            if not self.session.is_active():
                return

            self.ui_queue.put({"type": "phase", "data": "Engineer 编码"})
            eng_resp = engineer.handle_message(sci_resp)
            if not eng_resp or eng_resp.msg_type == MessageType.ERROR:
                reason = eng_resp.content if eng_resp else "Engineer 未返回代码"
                self.ui_queue.put({"type": "error", "data": reason})
                self.session.terminate(reason)
                return
            if eng_resp:
                self.ui_queue.put({"type": "message", "data": eng_resp})
            if not self.session.is_active():
                return

            max_retries = self.config.get("executor_max_retries", 5)
            last_code = eng_resp.content if eng_resp else ""
            for attempt in range(max_retries):
                self.ui_queue.put({"type": "phase", "data": "Executor 执行"})
                exec_resp = executor.handle_message(eng_resp)
                if exec_resp:
                    self.ui_queue.put({"type": "message", "data": exec_resp})
                if not self.session.is_active():
                    return

                if exec_resp and exec_resp.msg_type == MessageType.ERROR:
                    if self._is_fatal_data_error(exec_resp.content):
                        self.ui_queue.put({"type": "system", "data": "检测到数据缺失问题，上报监督Agent..."})
                        engineer.report_issue(
                            issue_type="missing_data",
                            description=f"执行代码时发现关键数据缺失: {exec_resp.content[:500]}",
                            severity="fatal",
                        )
                        reason = "任务因数据缺失终止，请检查 data/ 文件夹。"
                        self.ui_queue.put({"type": "error", "data": reason})
                        self.session.terminate(reason)
                        return

                    if attempt < max_retries - 1:
                        self.ui_queue.put({"type": "system", "data": f"执行失败，Engineer 定点修复中... (尝试 {attempt+2}/{max_retries})"})
                        self.ui_queue.put({"type": "phase", "data": "Engineer 编码"})
                        repair_context = (
                            f"## 代码执行出错，请针对以下错误进行定点修复（不要重写整个代码）\n\n"
                            f"### 错误信息\n{exec_resp.content[-2000:]}\n\n"
                            f"### 上次提交的代码（仅需修复错误相关部分）\n```python\n{last_code[-3000:]}\n```"
                        )
                        repair_msg = Message(
                            sender="Executor", receiver="Engineer",
                            content=repair_context, msg_type=MessageType.ERROR,
                        )
                        eng_resp = engineer.handle_message(repair_msg)
                        if not eng_resp or eng_resp.msg_type == MessageType.ERROR:
                            reason = eng_resp.content if eng_resp else "Engineer 未返回修复代码"
                            self.ui_queue.put({"type": "error", "data": reason})
                            self.session.terminate(reason)
                            return
                        if eng_resp:
                            self.ui_queue.put({"type": "message", "data": eng_resp})
                            last_code = eng_resp.content
                        if not self.session.is_active():
                            return
                    else:
                        reason = f"执行失败，已达最大重试次数 ({max_retries})"
                        self.ui_queue.put({"type": "error", "data": reason})
                        self.session.terminate(reason)
                        return
                else:
                    break

            self.ui_queue.put({"type": "phase", "data": "监督检查"})
            sm.force_transition(ConversationPhase.REVIEW_CHECKING)
            if exec_resp and exec_resp.msg_type == MessageType.RESULT:
                final_supervisor = supervisor.handle_message(exec_resp)
                if final_supervisor:
                    if final_supervisor.msg_type == MessageType.ERROR:
                        self.ui_queue.put({"type": "error", "data": final_supervisor.content})
                    else:
                        self.ui_queue.put({"type": "message", "data": final_supervisor})

            if self.session.is_active():
                self.ui_queue.put({"type": "phase", "data": "生成报告"})
                if exec_resp and exec_resp.msg_type == MessageType.RESULT:
                    report = manager.handle_message(exec_resp)
                    if report and report.msg_type == MessageType.ERROR:
                        self.ui_queue.put({"type": "error", "data": report.content})
                        self.session.terminate("Manager 报告生成失败")
                        return
                    if report:
                        self.ui_queue.put({"type": "message", "data": report})

                self.ui_queue.put({"type": "phase", "data": "完成"})
                sm.force_transition(ConversationPhase.COMPLETED)

        except Exception as e:
            import traceback
            self.ui_queue.put({"type": "error", "data": f"管线异常: {e}\n{traceback.format_exc()[-200:]}"})
            if self.session and self.session.is_active():
                self.session.terminate("管线异常")

    def start_analysis(self, user_input: str, auto_pilot: bool = False):
        """启动或继续分析流程"""
        with self._ui_lock:
            worker = getattr(self, "_thread", None)
            if self._running or (worker is not None and worker.is_alive()):
                return {"success": False, "busy": True,
                        "message": "系统正在运行中，请等待当前分析完成。"}

            if not user_input.strip():
                return {"success": False, "message": "请输入分析需求。"}

            content = user_input.strip().lower()
            is_continue = content in ('approve', '确认', 'yes', 'ok', 'end', 'exit', '终止')
            is_manager_revision = (
                not auto_pilot
                and not is_continue
                and self.session is not None
                and self.session.is_active()
                and self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM
                and bool(self.agents)
            )
            if (not is_continue and not is_manager_revision) or not self.agents:
                self._chat_history = []
                self._monitor_history = []
                self._raw_history = []
                self._trace_events = []
                self._ui_events = []
                while True:
                    try:
                        self.ui_queue.get_nowait()
                    except Empty:
                        break
                self.agents = self._create_agents()
                self._setup_session()

            self._running = True
            self.ui_queue.put({
                "type": "user_input",
                "data": {"content": user_input.strip(), "auto_pilot": auto_pilot},
            })
            self._thread = threading.Thread(
                target=self._process_flow,
                args=(user_input, auto_pilot),
                daemon=True,
            )
            self._thread.start()

            return {"success": True, "message": f"分析已启动: {user_input[:100]}..."}

    def terminate_session(self):
        """终止当前会话"""
        if self.session:
            self.session.terminate("用户手动终止")
        self._running = False
        return {"success": True, "message": "会话已终止。"}

    def get_updates(self, since: int = 0) -> dict:
        """把队列转入当前会话日志，按游标返回可重复读取的 UI 更新。"""
        with self._ui_lock:
            updates = []
            while True:
                try:
                    updates.append(self.ui_queue.get_nowait())
                except Empty:
                    break

            self._sync_trace_events()
            if updates:
                processed = self._process_updates(updates)
                for event in processed["updates"]:
                    self._ui_seq += 1
                    self._ui_events.append({**event, "seq": self._ui_seq})

            result = self._build_state_response()
            result["updates"] = [
                event.copy() for event in self._ui_events
                if event["seq"] > max(0, since)
            ]
            result["cursor"] = self._ui_seq
            return result

    def _sync_trace_events(self):
        """从 Session.tracer 同步事件到 UI 展示列表"""
        if not self.session or not hasattr(self.session, 'tracer'):
            return
        tracer = self.session.tracer
        raw_events = tracer.get_events()
        current_count = len(self._trace_events)
        if len(raw_events) > current_count:
            from dataclasses import asdict
            self._trace_events = [asdict(e) for e in raw_events]

    def _process_updates(self, updates: list) -> dict:
        """处理更新队列"""
        result = {
            "updates": [],
            "phase": None,
            "status": None,
            "done": False,
            "waiting_confirm": False,
            "monitor_html": "",
            "trace_html": "",
        }

        for item in updates:
            t = item["type"]
            data = item["data"]

            if t == "phase":
                result["phase"] = self.PHASE_NAMES.get(data, "init")
                result["status"] = f"当前阶段: {data}"

            elif t == "user_input":
                content = data["content"]
                time_str = datetime.now().strftime("%H:%M:%S")
                self._raw_history.append({
                    "time": time_str, "sender": "User", "content": content,
                })
                result["updates"].append({
                    "type": "message", "sender": "User", "content": content,
                    "auto_pilot": data["auto_pilot"], "time": time_str,
                })

            elif t == "message":
                msg: Message = data
                sender = msg.sender
                if sender == "User":  # 已由获准的提交事件记录
                    continue
                content = msg.content

                if sender == "SupervisorAgent":
                    self._monitor_history.append(f"&#128737;&#65039; <b>{sender}</b>: {content}")
                elif sender in ("Executor", "System"):
                    self._chat_history.append({"role": "system", "sender": sender, "content": content})
                else:
                    self._chat_history.append({"role": "bot", "sender": sender, "content": content})

                self._raw_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "sender": sender,
                    "content": content,
                })

                result["updates"].append({
                    "type": "message",
                    "sender": sender,
                    "content": content,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

            elif t == "system":
                self._chat_history.append({"role": "system", "sender": "系统", "content": data})
                self._raw_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "sender": "系统",
                    "content": data,
                })
                result["updates"].append({
                    "type": "system",
                    "content": data,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

            elif t == "error":
                self._chat_history.append({"role": "error", "sender": "错误", "content": data})
                self._raw_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "sender": "错误",
                    "content": data,
                })
                result["updates"].append({
                    "type": "error",
                    "content": data,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
                result["status"] = f"错误: {data}"

            elif t == "done":
                if not result["status"] or not result["status"].startswith("错误:"):
                    if self.session and self.session.is_terminated():
                        result["status"] = "已终止"
                    elif self.session and self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM:
                        result["status"] = "等待确认"
                    elif self.session and self.session.result_audit.get("status") == "needs_review":
                        result["status"] = "分析结束（结果待复核）"
                    else:
                        result["status"] = "分析完成"
                result["phase"] = "confirm" if (
                    self.session and self.session.state_machine.get_current_phase() == ConversationPhase.MANAGER_CONFIRM
                ) else "done"
                result["done"] = True

        # 附加监控和追溯 HTML
        result["monitor_html"] = self._render_monitor_html()
        result["trace_html"] = self._render_trace_html()

        # 检查是否在等待用户确认
        if self.session:
            current = self.session.state_machine.get_current_phase()
            result["waiting_confirm"] = (
                current == ConversationPhase.MANAGER_CONFIRM and not self._running
            )

        return result

    def _build_state_response(self) -> dict:
        """从当前会话构建状态，不依赖旧队列事件。"""
        phase = "init"
        phase_val = None
        if self.session:
            phase_val = self.session.state_machine.get_current_phase()
            phase = self.PHASE_NAMES.get(str(phase_val.value) if hasattr(phase_val, 'value') else str(phase_val), "init")

        waiting_confirm = phase_val == ConversationPhase.MANAGER_CONFIRM and not self._running
        done = not self._running and phase_val in (
            ConversationPhase.MANAGER_CONFIRM,
            ConversationPhase.COMPLETED,
            ConversationPhase.TERMINATED,
        )
        if self._running:
            status = "运行中..."
        elif waiting_confirm:
            status = "等待确认"
        elif phase_val == ConversationPhase.COMPLETED:
            status = ("分析结束（结果待复核）" if self.session.result_audit.get("status") == "needs_review"
                      else "分析完成")
        elif phase_val == ConversationPhase.TERMINATED:
            status = "已终止"
        else:
            status = "就绪"

        return {
            "updates": [],
            "phase": phase,
            "status": status,
            "running": self._running,
            "done": done,
            "waiting_confirm": waiting_confirm,
            "monitor_html": self._render_monitor_html(),
            "trace_html": self._render_trace_html(),
        }

    def _render_monitor_html(self) -> str:
        """渲染监督面板 HTML"""
        if not self._monitor_history:
            return "<div class='monitor-entry' style='color:var(--text-dim)'>supervisor_Agent 就绪，等待消息...</div>"
        return "".join(
            f"<div class='monitor-entry'>{m}</div>"
            for m in self._monitor_history[-10:]
        )

    def _render_trace_html(self) -> str:
        """渲染执行追溯面板 HTML"""
        if not self._trace_events:
            return "<tr><td colspan='4' style='text-align:center;padding:16px;'>等待执行事件...</td></tr>"

        icon_map = {"llm_call": "&#128260;", "tool_call": "&#128295;", "code_execution": "&#9654;&#65039;"}
        status_color = {"success": "var(--green)", "error": "var(--red)", "warning": "var(--orange)"}

        rows = ""
        for e in self._trace_events[-20:]:
            event_type = e.get("event_type", "")
            icon = icon_map.get(event_type, "")
            color = status_color.get(e.get("status", ""), "var(--text-dim)")
            detail = ""
            d = e.get("data", {})
            if event_type == "llm_call":
                detail = f"模型:{d.get('model','')} token:{d.get('total_tokens',0)}"
            elif event_type == "tool_call":
                detail = d.get("tool_name", "")
            elif event_type == "code_execution":
                exit_code = d.get("exit_code", "")
                tools = d.get("tools_detected", 0)
                parts = []
                if exit_code is not None:
                    parts.append(f"exit={exit_code}")
                if tools:
                    parts.append(f"工具x{tools}")
                detail = " ".join(parts)
                if d.get("files_generated"):
                    detail += f" 文件:{','.join(d['files_generated'][:3])}"

            duration = f"{e.get('duration_ms', 0):.0f}ms"

            rows += "<tr>"
            rows += f"<td style='color:{color}'>{icon} {event_type}</td>"
            rows += f"<td>{e.get('agent','')}</td>"
            rows += f"<td>{detail}</td>"
            rows += f"<td style='text-align:right'>{duration}</td>"
            rows += "</tr>"

        return rows

    def _is_fatal_data_error(self, error_text: str) -> bool:
        """判断执行错误是否为数据缺失类致命问题"""
        fatal_patterns = [
            r'FileNotFoundError',
            r'No such file or directory',
            r'✗.*文件不存在',
            r'✗.*文件未找到',
            r'错误:.*文件夹.*不存在',
            r'[✗×].*不存在.*\.(?:tif|tiff|TIF|TIFF)',
        ]
        syntax_indicators = [
            r'SyntaxError', r'unterminated string', r'was never closed',
            r'invalid syntax', r'EOL while scanning',
        ]
        if any(re.search(p, error_text) for p in syntax_indicators):
            return False
        return any(re.search(p, error_text) for p in fatal_patterns)

    def refresh_data_summary(self) -> str:
        """刷新 data/ 目录文件摘要和智能分析"""
        self._data_summary_html = self._build_data_summary_html()
        self._data_analysis_html = self._build_data_analysis_html()
        return self._data_summary_html + self._data_analysis_html

    def export_conversation(self) -> str:
        """导出对话记录为 txt 文件，返回文件路径"""
        if not self._raw_history:
            return ""
        return self._write_conversation_file()

    def save_conversation_local(self) -> str:
        """保存对话记录到项目 exports/ 文件夹"""
        if not self._raw_history:
            return "暂无对话记录可保存。"
        filepath = self._write_conversation_file()
        return f"已保存到: {filepath}"

    def _write_conversation_file(self) -> str:
        """写入对话文件，返回文件路径"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"conversation_{timestamp}.txt"
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "exports", filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        lines = []
        lines.append("=" * 60)
        lines.append("  USRA_UP 遥感分析系统 — 对话记录")
        lines.append(f"  导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 60)
        lines.append("")

        for entry in self._raw_history:
            sender = entry["sender"]
            time_str = entry["time"]
            content = entry["content"]
            lines.append(f"[{time_str}] {sender}:")
            lines.append(content)
            lines.append("-" * 40)
            lines.append("")

        lines.append("=" * 60)
        lines.append("  记录结束")
        lines.append("=" * 60)

        if self.session and hasattr(self.session, 'tracer'):
            try:
                lines.append("")
                lines.append("=" * 60)
                lines.append("  执行追溯")
                lines.append("=" * 60)
                lines.append("")
                lines.append(self.session.tracer.export_json())
                lines.append("")
            except Exception:
                pass

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return filepath

    def get_supervisor_report(self) -> str:
        """获取监督报告"""
        if not self.agents or "supervisor" not in self.agents:
            return "监督 Agent 尚未初始化。"
        report = self.agents["supervisor"].get_monitoring_report()
        return json.dumps(report, ensure_ascii=False, indent=2)

    def new_chat(self):
        """重置会话，开始新对话"""
        with self._ui_lock:
            worker = getattr(self, "_thread", None)
            if self._running or (worker is not None and worker.is_alive()):
                return {"success": False, "busy": True,
                        "message": "后台任务尚未退出，请稍后再新建会话。"}
            self._chat_history = []
            self._monitor_history = []
            self._raw_history = []
            self._trace_events = []
            self._ui_events = []
            while True:
                try:
                    self.ui_queue.get_nowait()
                except Empty:
                    break
            self.agents = {}
            self.session = None
            self.proxy = None
            self._running = False
            self._data_summary_html = self._build_data_summary_html()
            self._data_analysis_html = self._build_data_analysis_html()
            return {"success": True}


# === Flask 应用 ===

def create_app() -> Flask:
    """创建 Flask 应用"""
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    app = Flask(__name__, template_folder=template_dir)
    ctrl = URSAGUI()

    @app.route('/')
    def index():
        data_html = ctrl._data_summary_html + ctrl._data_analysis_html
        return render_template('index.html', model=ctrl.model, data_html=data_html)

    @app.route('/api/chat', methods=['POST'])
    def api_chat():
        data = request.get_json()
        message = data.get('message', '')
        auto_pilot = data.get('auto_pilot', False)
        result = ctrl.start_analysis(message, auto_pilot=auto_pilot)
        return jsonify(result)

    @app.route('/api/updates')
    def api_updates():
        result = ctrl.get_updates(since=request.args.get('since', 0, type=int))
        return jsonify(result)

    @app.route('/api/terminate', methods=['POST'])
    def api_terminate():
        result = ctrl.terminate_session()
        return jsonify(result)

    @app.route('/api/new_chat', methods=['POST'])
    def api_new_chat():
        result = ctrl.new_chat()
        return jsonify(result)

    @app.route('/api/refresh_data', methods=['POST'])
    def api_refresh_data():
        html = ctrl.refresh_data_summary()
        return jsonify({"success": True, "html": html})

    @app.route('/api/export')
    def api_export():
        filepath = ctrl.export_conversation()
        if filepath:
            return jsonify({"success": True, "filepath": filepath})
        return jsonify({"success": False, "message": "暂无对话记录"})

    @app.route('/api/download')
    def api_download():
        filepath = request.args.get('file', '')
        if filepath and os.path.exists(filepath):
            return send_file(filepath, as_attachment=True)
        return jsonify({"success": False, "message": "文件不存在"}), 404

    @app.route('/api/save_local', methods=['POST'])
    def api_save_local():
        message = ctrl.save_conversation_local()
        return jsonify({"success": True, "message": message})

    @app.route('/api/supervisor_report')
    def api_supervisor_report():
        report = ctrl.get_supervisor_report()
        return jsonify({"success": True, "report": report})

    @app.route('/api/files')
    def api_files():
        from tools.io_kit import scan_data_directory
        scan = scan_data_directory()
        return jsonify(scan)

    return app


def launch_app(server_name: str = "127.0.0.1", server_port: int = 7860):
    """启动 Flask 应用"""
    app = create_app()
    print(f"USRA_UP v1.14 界面启动: http://{server_name}:{server_port}")
    app.run(host=server_name, port=server_port, debug=False, threaded=True)
