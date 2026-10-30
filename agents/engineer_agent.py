"""工程师 Agent — 生成遥感分析代码（v1.14：错误记忆系统 + max_tokens可配置）"""

import os
import re
import json
from datetime import datetime
from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase
from tools.data_context import render_data_context
from tools.task_contract import render_task_contract

# 错误类型分类表（纯正则，不依赖LLM）
ERROR_CLASSIFICATION = [
    ("MemoryError", [
        r"MemoryError", r"3228369023", r"ArrayMemoryError",
        r"ACCESS_VIOLATION", r"内存溢出", r"OOM",
    ], "数据处理：rasterio Window分块，逐块处理后del释放，float64→float32。可视化：每个plt调用必须try/except包裹，单图失败print跳过信息并continue"),
    ("AttributeError", [
        r"AttributeError.*has no attribute", r"module.*has no attribute",
    ], "显式导入缺失的模块或属性（如 import matplotlib.font_manager）"),
    ("ValueError_broadcast", [
        r"ValueError.*broadcast", r"could not broadcast",
        r"shape.*into shape",
    ], "分块/缩放操作时确保输出数组维度与输入块精确匹配，用 min() 处理边界像素"),
    ("FileNotFoundError", [
        r"FileNotFoundError", r"No such file", r"文件不存在", r"文件未找到",
    ], "用 os.walk() 递归扫描 data/ 获取实际文件名，注意大小写"),
    ("SyntaxError", [
        r"SyntaxError", r"unterminated string", r"EOL while scanning",
        r"invalid syntax",
    ], "检查字符串引号闭合、括号匹配、缩进；特别注意中文标题中的引号"),
    ("IndexError", [
        r"IndexError", r"out of range", r"索引超出",
    ], "rasterio波段索引从1开始（src.read(1)），numpy数组索引从0开始"),
]


class EngineerAgent(BaseAgent):
    """工程师 Agent，负责：
    - 根据 Scientist 的分析计划生成可执行代码
    - 确保代码只使用本地数据
    - 修复 Executor 返回的错误
    - （v1.14新增）持久化错误记忆，跨重试和跨会话学习
    """

    MEMORY_FILE = "data/.engineer_memory.json"

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str, max_tokens: int = 32768):
        super().__init__(name="Engineer", system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url, max_tokens=max_tokens)

    # ============================================================
    # 错误记忆系统
    # ============================================================

    def _load_memories(self) -> list:
        """加载持久化错误记忆"""
        try:
            if os.path.exists(self.MEMORY_FILE):
                with open(self.MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("memories", [])
        except Exception:
            pass
        return []

    def _save_memories(self, memories: list) -> None:
        """保存错误记忆到文件"""
        try:
            os.makedirs(os.path.dirname(self.MEMORY_FILE), exist_ok=True)
            with open(self.MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump({"version": "1.0", "memories": memories}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _classify_error(self, error_text: str) -> tuple:
        """用正则快速分类错误类型，返回 (error_type, pattern, fix_suggestion)

        先清洗输入：去除 Engine 修复 prompt 的包装文本（"## 代码执行出错..."等），
        只保留 Executor 的实际报错内容，避免将 markdown 标记当作错误模式存储。
        """
        # 清洗：去除修复 prompt 的包装头和代码块标记，只提取实际错误
        cleaned = error_text
        # 如果有 "### 错误信息" 标记，只取之后的内容
        m = re.search(r'###\s*错误信息\s*\n(.+)', cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
        # 截断在代码块之前（"### 上次提交的代码"）
        m = re.search(r'###\s*上次提交的代码', cleaned)
        if m:
            cleaned = cleaned[:m.start()]
        # 提取最后一段 Traceback 或关键错误行（最多取最后 500 字符）
        tb_match = re.search(r'(Traceback\s*\(most recent call last\):.*)', cleaned, re.DOTALL)
        if tb_match:
            cleaned = tb_match.group(1)
        cleaned = cleaned.strip()[:500]

        for err_type, patterns, fix in ERROR_CLASSIFICATION:
            if err_type == "Other":
                continue
            for pat in patterns:
                if re.search(pat, cleaned):
                    # 提取关键描述：取错误文本的最后一行或 Traceback 的最后一行
                    lines = [l for l in cleaned.split("\n") if l.strip() and not l.startswith("##")]
                    pattern_desc = lines[-1].strip()[:120] if lines else cleaned[:120]
                    return (err_type, pattern_desc, fix)
        return ("Other", cleaned[:200] if cleaned else error_text[:200], ERROR_CLASSIFICATION[-1][2])

    def _update_memory(self, error_text: str) -> None:
        """从错误文本中提取经验并持久化"""
        err_type, pattern, fix = self._classify_error(error_text)
        if err_type == "Other" and len(error_text) < 30:
            return  # 太短的通用错误不记录

        memories = self._load_memories()

        for m in memories:
            if m["error_type"] == err_type:
                m["count"] += 1
                m["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                m["pattern"] = pattern  # 更新为最新描述
                self._save_memories(memories)
                return

        memories.append({
            "error_type": err_type,
            "pattern": pattern,
            "fix": fix,
            "count": 1,
            "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 按发生次数降序，保留最近30条
        memories.sort(key=lambda m: m["count"], reverse=True)
        self._save_memories(memories[:30])

    def _build_memories_prompt(self) -> str:
        """构建错误记忆提示词片段"""
        memories = self._load_memories()
        if not memories:
            return ""

        # 只取前10条最高频的记忆
        top = sorted(memories, key=lambda m: m["count"], reverse=True)[:10]
        lines = [
            "## 🧠 历史错误记忆（请特别注意避免以下问题）\n",
            "以下是从之前的代码执行中积累的错误经验，请务必在生成代码时避免这些已知问题：\n",
        ]
        for m in top:
            lines.append(
                f"- **[{m['error_type']}]** {m['pattern']}\n"
                f"  修复方案：{m['fix']}\n"
                f"  （已发生 {m['count']} 次，最近: {m['last_seen']}）\n"
            )
        lines.append("---\n")
        return "\n".join(lines)

    def _call_llm_with_memory(self, user_message: str, temperature: float = 0.3) -> str:
        """调用 LLM，自动注入错误记忆"""
        memory_prompt = self._build_memories_prompt()
        if memory_prompt:
            user_message = memory_prompt + user_message
        return self._call_llm(user_message, temperature, stream=True)

    def _code_message(self, prompt: str) -> Message:
        """仅将完整的模型响应作为可执行代码交给 Executor。"""
        prompt += (
            f"\n\n本次任务唯一产物目录：{self.session.result_relative_dir}\n"
            "执行器通过 URSA_RESULTS_DIR 提供该目录的绝对路径。代码必须统一初始化：\n"
            "import os\nRESULTS_DIR = os.environ['URSA_RESULTS_DIR']\n"
            "os.makedirs(RESULTS_DIR, exist_ok=True)\n"
            "所有 JPG、TIFF、CSV、报告和中间文件都使用 os.path.join(RESULTS_DIR, 文件名) 保存。"
            "禁止直接写入 results/ 根目录或其他任务目录；不要每轮重新创建任务文件夹。"
            "本任务的修复、重试及子步骤继续复用上述目录，读取本任务中间文件也从该目录获取。"
            "代码末尾打印本目录及实际产物完整路径。"
        )
        response = self._call_llm_with_memory(prompt)
        if response.startswith("ERROR:"):
            return Message(
                sender=self.name,
                receiver="Manager",
                content=response,
                msg_type=MessageType.ERROR,
            )
        code = self._sanitize_response(response)
        return Message(
            sender=self.name,
            receiver="Executor",
            content=code,
            msg_type=MessageType.CODE,
            data_sources=self.scan_data_sources(code),
        )

    # ============================================================
    # 消息处理
    # ============================================================

    def handle_message(self, message: Message) -> Message:
        self.session.state_machine.force_transition(ConversationPhase.ENGINEER_CODING)
        if message.metadata.get("tool_agent_request"):
            from tools.tool_agent_workflow import code_message
            return code_message(self.session, message.metadata["tool_agent_request"])
        if message.metadata.get("remote_tool_request"):
            from tools.remote_models import tool_message
            return tool_message(self.session, message.metadata["remote_tool_request"])
        if getattr(self.session, "remote_tool_plan", {}) and message.msg_type == MessageType.ERROR:
            return Message(sender=self.name, receiver="Manager", msg_type=MessageType.ERROR,
                           content="固定GPU工具未通过执行或核验，请检查隧道、服务及输入；不让模型改写已锁定算法。")

        if self.session.tool_plan or message.metadata.get("tool_request"):
            from tools.installed_workflow import tool_code_message
            return tool_code_message(self.session, message.metadata.get("tool_request", {}))

        # Executor 返回错误，修复代码
        if message.sender == "Executor" and message.msg_type == MessageType.ERROR:
            error_text = message.content

            # 持久化错误记忆（v1.14 新增）
            self._update_memory(error_text)

            # 判断错误类型，给出针对性指导
            if "SyntaxError" in error_text or "语法错误" in error_text:
                extra_guidance = (
                    "\n这是语法错误！请仔细检查：\n"
                    "1. 所有字符串引号是否正确闭合（特别是中文标题中的引号）\n"
                    "2. 括号是否成对匹配\n"
                    "3. 冒号是否正确使用\n"
                    "4. f-string 格式是否正确\n"
                    "请只修复语法问题，不要改动已验证通过的逻辑。"
                )
            elif "MemoryError" in error_text or "ArrayMemoryError" in error_text or "3228369023" in error_text:
                extra_guidance = (
                    "\n内存/访问错误 (3228369023 = STATUS_ACCESS_VIOLATION)！请检查：\n"
                    "1. 确保 import matplotlib + matplotlib.use('Agg') 位于代码最开头，"
                    "在 import rasterio 之前（Windows 下 matplotlib 与 GDAL 的 DLL 加载顺序冲突）\n"
                    "2. 所有 plt.subplots() / plt.savefig() / plt.imshow() 必须包在 try/except 中\n"
                    "3. 每张图失败时打印跳过信息（不要终止整个流程）\n"
                    "4. 确保数据处理和结果文件保存（.tif/.txt）在可视化之前已完成\n"
                    "5. 大影像使用 rasterio Window 分块处理，逐块 del 释放，float64→float32"
                )
            elif "不存在" in error_text or "not found" in error_text.lower() or "FileNotFoundError" in error_text:
                extra_guidance = (
                    "\n文件不存在！请：\n"
                    "1. 先扫描 data/ 文件夹中的实际文件名（os.walk 递归扫描）\n"
                    "2. 注意文件名大小写\n"
                    "3. 使用 os.path.join() 构建路径"
                )
            elif "timeout" in error_text.lower() or "超时" in error_text:
                extra_guidance = (
                    "\n代码超时 (300秒)！请：\n"
                    "1. 简化不必要的复杂计算\n"
                    "2. 减少 matplotlib 图表数量或降低 DPI\n"
                    "3. 对大影像使用分块处理"
                )
            elif "IndexError" in error_text or "索引" in error_text:
                extra_guidance = (
                    "\n索引错误！请检查：\n"
                    "1. rasterio 波段索引从 1 开始（src.read(1) 是第1波段）\n"
                    "2. numpy 数组索引从 0 开始\n"
                    "3. 波段匹配逻辑是否正确"
                )
            elif "ValueError" in error_text and ("broadcast" in error_text or "shape" in error_text):
                extra_guidance = (
                    "\n数组形状不匹配！请检查：\n"
                    "1. 分块/缩放后输出数组维度是否与数据块精确匹配\n"
                    "2. 使用 min() 处理边界像素（最后一块可能不满）\n"
                    "3. 检查 block_size 是否能被影像尺寸整除"
                )
            elif "AttributeError" in error_text and "has no attribute" in error_text:
                extra_guidance = (
                    "\n模块属性不可用！请：\n"
                    "1. 显式导入缺失的子模块（如 import matplotlib.font_manager）\n"
                    "2. 不要假设 import 包名就会自动加载所有子模块"
                )
            else:
                extra_guidance = ""

            prompt = f"""代码执行出错，请针对错误信息进行定点修复。不要重写整个代码。

错误详情：
{error_text}
{extra_guidance}

修复要求：
- 只修改出错的部分，保持已验证通过的逻辑不变
- 输出纯 Python 代码，不要 markdown 标记
- 确保所有字符串闭合（尤其中文标题）
- 只使用本地数据文件
- 所有结果保存到本次任务指定的 RESULTS_DIR，修复时复用
- 代码末尾打印已保存的结果文件清单"""

            return self._code_message(prompt)

        # Supervisor 要求修改
        if message.sender == "SupervisorAgent":
            prompt = f"审查反馈：{message.content}\n\n请根据反馈修改代码。"
            return self._code_message(prompt)

        # Scientist 的计划
        if message.sender in ("Scientist", "Manager"):
            prompt = f"""根据以下分析计划生成可执行的 Python 代码：

{message.content}

已确认用户要求（用户原文与有序修改优先于技术计划）：
{self.session.confirmed_task or self.session.user_request}

{render_data_context(self.session.data_context)}

{render_task_contract(self.session)}

代码要求：
1. 首先用 os.walk() 递归扫描 data/ 文件夹，列出所有可用文件
2. 只使用本地数据文件，不下载任何数据
3. 使用 rasterio 读取数据，numpy 进行计算，matplotlib 生成图表
4. 所有结果保存到本次任务的 RESULTS_DIR（由 URSA_RESULTS_DIR 提供），先创建目录
5. 使用 os.path.join() 处理文件路径，确保跨平台兼容
6. 添加必要的 print 语句输出进度和结果
7. 不要使用 pip 安装包
8. 不要生成假数据或模拟数据
9. 默认生成能让普通用户看懂的结果图：中文标题、中文图例；变化任务用前后对照或在原图叠加标记。用户明确取消图像时遵守其要求。
10. 在报告中逐项说明用户目标完成到什么程度；亮度变化候选不能改称已验证建筑变化。去云补全必须说明是推算。

内存安全要求（v1.14，Windows 环境尤为重要）：
- 大影像（>5000×5000 像素）必须使用 rasterio Window 参数分块处理
- 避免创建 (波段数 × 总像素数) 形状的中间数组，逐波段或逐块处理
- 每处理完一个块后及时 del 释放引用，帮助垃圾回收
- float64 → float32 可减半内存占用
- rasterio 读取操作包裹在 try/except 中
- 处理完成后 plt.close('all') 释放 matplotlib 内存
- 代码末尾打印实际 RESULTS_DIR 路径及生成的文件清单"""

            return self._code_message(prompt)

        return None

    # ============================================================
    # 代码清理与字体注入
    # ============================================================

    def _sanitize_response(self, response: str) -> str:
        """清理响应，提取纯代码（防御性处理截断的 markdown 块）"""
        code = response

        for marker in ("```python", "```Python", "```"):
            if marker in code:
                start = code.index(marker) + len(marker)
                code = code[start:]
                end_marker = code.rfind("```")
                if end_marker > 0:
                    code = code[:end_marker]
                break

        code = code.strip()

        lines = code.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        code = "\n".join(lines)

        code = self._inject_chinese_font(code)
        return code

    FONT_INJECTION = """
# === matplotlib 全局配置（非交互后端 + 中文字体，防止弹窗和乱码） ===
import matplotlib
matplotlib.use("Agg")  # 必须在 import pyplot 之前设置，避免弹窗阻塞
import matplotlib.font_manager  # 显式导入，修复某些环境下 font_manager 不可用
_ursa_fonts = ['Microsoft YaHei', 'SimHei', 'SimSun', 'KaiTi', 'FangSong', 'Noto Sans CJK SC']
_ursa_avail = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
_ursa_fb = next((f for f in _ursa_fonts if f in _ursa_avail), None)
if _ursa_fb:
    matplotlib.rcParams['font.sans-serif'] = [_ursa_fb, 'DejaVu Sans']
    matplotlib.rcParams['axes.unicode_minus'] = False
"""

    def _inject_chinese_font(self, code: str) -> str:
        """自动注入中文字体配置，防止图表中文乱码"""
        if 'font.sans-serif' in code or 'unicode_minus' in code:
            return code
        if 'import matplotlib' in code:
            idx = code.index('import matplotlib')
            end = code.index('\n', idx)
            return code[:end+1] + self.FONT_INJECTION + code[end+1:]
        if 'import' in code:
            last_import = code.rindex('import')
            end = code.index('\n', last_import)
            return code[:end+1] + self.FONT_INJECTION + code[end+1:]
        return self.FONT_INJECTION + code
