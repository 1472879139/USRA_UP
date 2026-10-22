"""监督 Agent — v1.14：从"拦截者"变为"观察者"，执行后验证结果，不干预开发-调试循环"""

import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List

from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase, AgentState


class SupervisorAgent(BaseAgent):
    """监督 Agent — 系统最高权限监测者（v1.14 重构）

    核心职责：
    1. 被动观察所有消息，不干预 Scientist/Engineer/Executor 之间的正常交互
    2. 执行后验证：Executor 成功后检查 results/ 是否有实际产出
    3. 检测死锁、死循环、重复对话
    4. 处理 Agent 主动上报的问题
    5. 一键托管模式：自动择优回答，加速流程
    6. 异常自动终止（死锁、反复循环、警告超限）
    7. 智能分析 data/ 目录影像的可用分析用途
    """

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str):
        super().__init__(name="SupervisorAgent", system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url)
        # 项目根目录（agents/supervisor_agent.py → ../ 即项目根）
        self._project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # 监控状态
        self._message_count: Dict[str, int] = {}
        self._content_hashes: Dict[str, list] = {}
        self._last_activity_time: Dict[str, float] = {}
        self._round_start_time: float = time.time()
        self._warning_count: int = 0
        self._max_warnings: int = 5
        self._auto_pilot: bool = False
        self._terminated_by_supervisor: bool = False
        self._violations: List[dict] = []

    def enable_auto_pilot(self) -> None:
        self._auto_pilot = True
        print("[SupervisorAgent] 一键托管模式已启用")

    def disable_auto_pilot(self) -> None:
        self._auto_pilot = False
        print("[SupervisorAgent] 一键托管模式已关闭")

    def is_auto_pilot(self) -> bool:
        return self._auto_pilot

    def handle_message(self, message: Message) -> Optional[Message]:
        """监督每条消息（v1.14：纯观察模式，不干预 Scientist/Engineer/Executor 交互）"""
        if not self.session or not self.session.is_active():
            return None

        self._update_stats(message)

        # 0. Agent 主动上报 — 最高优先处理
        if self._is_agent_report(message):
            return self._handle_agent_report(message)

        # Executor RESULT 必须在开发消息的提前返回之前处理。
        if message.sender == "Executor" and message.msg_type == MessageType.RESULT:
            validation_issue = self._validate_execution_results(message)
            if validation_issue:
                self._violations.append({
                    "time": datetime.now().isoformat(),
                    "type": "execution_validation",
                    "detail": validation_issue,
                })
                self.session.terminate("结果核验失败：" + validation_issue)
                return Message(sender=self.name, receiver="Manager", content=validation_issue,
                               msg_type=MessageType.ERROR, metadata={"action": "block"})
            if self.session.result_audit.get("status") == "needs_review":
                return Message(sender=self.name, receiver="Manager",
                               content=self.session.result_audit["facts"],
                               msg_type=MessageType.REVIEW, metadata={"action": "needs_review"})
            return None

        # Engineer-Executor 之间的错误/代码消息属于正常开发流程。
        if message.sender in ("Engineer", "Executor", "Scientist"):
            return None

        # 3. 死锁检测
        deadlock = self.session.state_machine.detect_deadlock()
        if deadlock:
            return self._handle_deadlock(deadlock)

        # 4. 重复内容检测
        if self.session.state_machine.check_repetition(message.sender, message.content):
            self._warning_count += 1
            self._violations.append({
                "time": datetime.now().isoformat(),
                "type": "repetition",
                "detail": f"Agent [{message.sender}] 重复内容",
            })
            return self._handle_repetition(message)

        # 5. 一键托管：自动处理 Manager 确认
        if self._auto_pilot and message.sender == "Manager" and message.receiver == "User":
            return self._auto_approve(message)

        # 6. 警告超限 → 终止
        if self._warning_count >= self._max_warnings:
            return self._force_terminate(
                f"警告次数达到上限 ({self._warning_count}/{self._max_warnings})"
            )

        return None

    # ============================================================
    # 执行后验证
    # ============================================================

    def _validate_execution_results(self, message: Message) -> Optional[str]:
        """Only current-run artifacts are evidence; numeric validity is not semantic accuracy."""
        from tools.result_audit import audit_execution_results

        # Use user texts in chronological order, not the Manager's paraphrase.
        requirements = "\n".join([self.session.user_request, *self.session.revision_feedback])
        audit = audit_execution_results(
            os.path.join(self._project_root, "results"),
            message.metadata.get("files_generated", []), requirements,
        )
        self.session.result_audit = audit
        message.metadata["result_audit"] = audit
        return "\n".join(audit["errors"]) or None

    # ============================================================
    # Agent 上报处理
    # ============================================================

    def _is_agent_report(self, message: Message) -> bool:
        return (
            message.metadata.get("action") == "report_issue"
            or "[Agent上报]" in message.content
        )

    def _handle_agent_report(self, message: Message) -> Optional[Message]:
        severity = message.metadata.get("severity", "warning")
        issue_type = message.metadata.get("issue_type", "unknown")
        reported_by = message.metadata.get("reported_by", message.sender)

        print(f"[SupervisorAgent] 收到 {reported_by} 的上报: [{severity}] {issue_type}")

        if severity == "fatal":
            # 致命问题：给予高权重警告但不绕过配额机制
            self._warning_count += 2
            self._violations.append({
                "time": datetime.now().isoformat(),
                "type": f"agent_report_{issue_type}",
                "detail": f"{reported_by} 上报致命问题: {message.content[:300]}",
            })
            # 如果加上这 2 分已经超限，直接终止
            if self._warning_count >= self._max_warnings:
                return self._force_terminate(
                    f"Agent [{reported_by}] 上报致命问题 [{issue_type}]，警告已达上限: {message.content[:300]}"
                )
            return self._issue_warning(
                message,
                f"Agent [{reported_by}] 上报致命问题 [{issue_type}]: {message.content[:200]}"
            )
        else:
            self._warning_count += 1
            self._violations.append({
                "time": datetime.now().isoformat(),
                "type": f"agent_report_{issue_type}",
                "detail": f"{reported_by} 上报: {message.content[:200]}",
            })
            return self._issue_warning(
                message,
                f"Agent [{reported_by}] 上报问题 [{issue_type}]: {message.content[:200]}"
            )

    # ============================================================
    # 工具方法
    # ============================================================

    def _update_stats(self, message: Message) -> None:
        name = message.sender
        self._message_count[name] = self._message_count.get(name, 0) + 1
        self._last_activity_time[name] = time.time()

    def _issue_warning(self, message: Message, reason: str) -> Message:
        print(f"[SupervisorAgent] 警告 (#{self._warning_count}): {reason}")
        return Message(
            sender=self.name,
            receiver=message.sender,
            content=f"监督警告 (#{self._warning_count}): {reason}",
            msg_type=MessageType.REVIEW,
            metadata={"action": "warn", "warning_count": self._warning_count, "original_message": message.msg_id},
        )

    def _handle_deadlock(self, reason: str) -> Message:
        print(f"[SupervisorAgent] 检测到死锁: {reason}")
        self.send_message(
            receiver="broadcast",
            content=f"死锁检测: {reason}",
            msg_type=MessageType.SYSTEM,
            metadata={"action": "deadlock_warning"},
        )
        return self._force_terminate(f"死锁: {reason}")

    def _handle_repetition(self, message: Message) -> Message:
        print(f"[SupervisorAgent] 检测到 [{message.sender}] 重复内容")
        return Message(
            sender=self.name,
            receiver=message.sender,
            content="检测到重复内容，请提供新的分析或终止对话。",
            msg_type=MessageType.REVIEW,
            metadata={"action": "repetition_warning", "warning_count": self._warning_count},
        )

    def _auto_approve(self, message: Message) -> Message:
        print(f"[SupervisorAgent] 一键托管：自动确认 Manager 消息")
        return Message(
            sender=self.name,
            receiver="User",
            content="[托管模式] 已自动确认。",
            msg_type=MessageType.REVIEW,
            metadata={"action": "auto_approve"},
        )

    def _force_terminate(self, reason: str) -> Message:
        print(f"[SupervisorAgent] 强制终止: {reason}")
        self._terminated_by_supervisor = True

        terminate_msg = Message(
            sender=self.name,
            receiver="broadcast",
            content=f"会话已被监督 Agent 强制终止。原因: {reason}",
            msg_type=MessageType.SYSTEM,
            metadata={"action": "force_terminate", "reason": reason},
        )
        self.session.bus.publish(terminate_msg)
        self.session.terminate(reason)
        return terminate_msg

    # ============================================================
    # 智能文件选取（保留原有工具方法）
    # ============================================================

    def select_relevant_files(self, user_query: str, data_scan_result: dict) -> dict:
        """根据用户问题，智能选取 data/ 目录中最相关的影像文件。"""
        if not data_scan_result.get("success"):
            return {"relevant_files": [], "summary": "数据扫描失败"}

        files = data_scan_result.get("data", {}).get("files", [])
        if not files:
            return {"relevant_files": [], "summary": "无可用文件"}

        NON_DATA_PATTERNS = ['QA_PIXEL', 'QA_RADSAT', 'QA_AEROSOL', 'QA_BAND',
                             'SAA', 'SZA', 'VAA', 'VZA', 'MTL', 'ANG',
                             'ST_QA', 'ST_ATRAN', 'ST_EMIS', 'ST_EMSD', 'ST_TRAD', 'ST_URAD',
                             'ST_DRAD', 'ST_QA', 'ST_CDIST']
        data_files = []
        skipped_files = []
        for f in files:
            fname_upper = f.get('file_name', '').upper()
            is_meta = any(p in fname_upper for p in NON_DATA_PATTERNS)
            if is_meta:
                skipped_files.append(f['file_name'])
            else:
                data_files.append(f)

        if len(data_files) == 0:
            return {
                "relevant_files": [{"file_name": f["file_name"], "reason": "自动选用(无可用数据文件)"} for f in files],
                "summary": "所有文件均为元数据/角度/QA文件，无可分析波段"
            }
        if len(data_files) == 1:
            return {
                "relevant_files": [{"file_name": data_files[0]["file_name"], "reason": "唯一数据波段文件"}],
                "summary": f"已过滤 {len(skipped_files)} 个元数据文件，选用: {data_files[0]['file_name']}"
            }

        prompt_parts = [
            "你是遥感数据匹配专家。用户提出了一个分析需求，请从可用文件中选出最相关的数据波段文件。\n",
            f"## 用户需求\n{user_query}\n",
        ]
        if skipped_files:
            prompt_parts.append("## 已自动排除的元数据文件 (QA/角度/质量标识)\n")
            prompt_parts.append("以下文件不是数据波段，已被排除，不要选取：\n")
            for sf in skipped_files:
                prompt_parts.append(f"  - {sf}")
            prompt_parts.append("")
        prompt_parts.append("## 可用数据文件（均为真实波段数据）\n")
        for i, f in enumerate(data_files, 1):
            prompt_parts.append(f"{i}. {f['file_name']}")
            prompt_parts.append(f"   - 波段: {f.get('bands')}个 ({f.get('dtype', '?')})")
            band_names = f.get("band_names", [])
            if band_names:
                names = [n for n in band_names if n and n != "None"]
                if names:
                    prompt_parts.append(f"   - 波段名: {', '.join(names)}")
            if f.get("crs"):
                prompt_parts.append(f"   - 坐标系: {f.get('crs')}")
            if f.get("resolution"):
                prompt_parts.append(f"   - 分辨率: {f.get('resolution')}")
            prompt_parts.append("")

        prompt_parts.append("## 要求\n")
        prompt_parts.append("选出与用户需求最匹配的文件，输出格式如下（严格按此格式）：\n")
        prompt_parts.append("SUMMARY: 一句话说明选取逻辑\n")
        prompt_parts.append("FILES: 文件名1 | 选取原因1\n")
        prompt_parts.append("FILES: 文件名2 | 选取原因2\n")
        prompt_parts.append("\n只输出匹配的文件，不相关的不要列出。每个文件一行。")

        prompt = "".join(prompt_parts)

        try:
            response = self._call_llm(prompt, temperature=0.2)
            return self._parse_file_selection(response, data_files)
        except Exception as e:
            return {
                "relevant_files": [{"file_name": f["file_name"], "reason": "自动选用(LLM不可用)"} for f in data_files],
                "summary": f"LLM调用失败({e})，返回 {len(data_files)} 个数据波段文件"
            }

    def _parse_file_selection(self, llm_response: str, all_files: list) -> dict:
        relevant = []
        summary = ""
        known_names = {f["file_name"] for f in all_files}

        for line in llm_response.strip().split("\n"):
            line = line.strip()
            if line.startswith("SUMMARY:") or line.startswith("SUMMARY："):
                summary = line.replace("SUMMARY:", "").replace("SUMMARY：", "").strip()
            elif line.startswith("FILES:") or line.startswith("FILES："):
                parts = line.replace("FILES:", "").replace("FILES：", "").strip().split("|", 1)
                fname = parts[0].strip()
                reason = parts[1].strip() if len(parts) > 1 else "匹配用户需求"
                matched = fname
                for known in known_names:
                    if fname.lower() in known.lower() or known.lower() in fname.lower():
                        matched = known
                        break
                if matched in known_names:
                    relevant.append({"file_name": matched, "reason": reason})

        if not relevant:
            return {
                "relevant_files": [{"file_name": f["file_name"], "reason": "自动选用(解析失败)"} for f in all_files],
                "summary": "未能精确匹配，返回全部文件"
            }

        return {
            "relevant_files": relevant,
            "summary": summary or f"根据需求选取了 {len(relevant)} 个相关文件"
        }

    def analyze_data_potential(self, data_scan_result: dict, user_query: str = "") -> str:
        """智能分析 data/ 目录中的影像可以用于哪些遥感分析用途。"""
        if not data_scan_result.get("success"):
            return f"数据扫描失败，无法生成分析建议: {data_scan_result.get('message', '未知错误')}"

        files = data_scan_result.get("data", {}).get("files", [])
        if not files:
            return "data/ 目录中未发现栅格文件，无法生成分析建议。请放入遥感影像后重试。"

        prompt_parts = [
            "你是一名资深遥感科学家。请根据以下 data/ 目录中的影像元数据，",
            "分析这些影像可以用于哪些遥感分析，并给出具体建议。\n",
        ]
        if user_query:
            prompt_parts.append(f"## 用户当前需求\n{user_query}\n")
            prompt_parts.append("请围绕用户需求给出针对性建议，而不是泛泛而谈。\n")
        prompt_parts.append("## 可用影像文件\n")

        for i, f in enumerate(files, 1):
            prompt_parts.append(f"### 文件 {i}: {f.get('file_name', '未知')}")
            prompt_parts.append(f"- 尺寸: {f.get('width')} × {f.get('height')} 像素")
            prompt_parts.append(f"- 波段数: {f.get('bands')} (数据类型: {f.get('dtype')})")
            if f.get("crs"):
                prompt_parts.append(f"- 坐标系: {f.get('crs')}")
            if f.get("resolution"):
                prompt_parts.append(f"- 分辨率: {f.get('resolution')}")
            band_names = f.get("band_names", [])
            if band_names:
                prompt_parts.append(f"- 波段名称: {', '.join(band_names)}")
            prompt_parts.append("")

        prompt_parts.append("## 系统可用分析工具\n")
        prompt_parts.append("- **指数计算**: NDVI (植被), EVI (增强植被), NDWI (水体), NBR (火烧迹地), LST (地表温度), MSAVI (土壤调节植被)")
        prompt_parts.append("- **专题分析**: 阈值分割, 面积计算 (km²/公顷), 掩膜应用, 分区统计")
        prompt_parts.append("- **可视化**: 指数分布图, 专题分类图, 假彩色合成图\n")
        prompt_parts.append("## 要求\n")
        prompt_parts.append("请用中文输出一份简洁的分析建议报告，包含以下内容：")
        prompt_parts.append("1. **数据概况**: 1-2句话总结可用数据")
        prompt_parts.append("2. **推荐分析**: 列出3-5个最适用的分析方向，每个说明为什么适用（关联到具体波段/特征）")
        prompt_parts.append("3. **使用提示**: 1-2条实用建议（波段选择、参数设置等）")
        prompt_parts.append("\n直接输出报告内容，无需额外说明。控制在300字以内。")

        prompt = "".join(prompt_parts)

        try:
            analysis = self._call_llm(prompt, temperature=0.5)
            return analysis
        except Exception as e:
            return f"生成分析建议时出错: {e}"

    def get_monitoring_report(self) -> dict:
        return {
            "agent": self.name,
            "total_messages": sum(self._message_count.values()),
            "messages_per_agent": self._message_count,
            "warnings_issued": self._warning_count,
            "max_warnings": self._max_warnings,
            "auto_pilot_enabled": self._auto_pilot,
            "terminated": self._terminated_by_supervisor,
            "violations": self._violations,
            "elapsed_seconds": time.time() - self._round_start_time,
        }
