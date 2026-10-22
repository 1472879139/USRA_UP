"""执行器 Agent — 安全执行 Python 代码"""

import os
import sys
import time
import shutil
import subprocess
import tempfile
import re
from .base_agent import BaseAgent
from core.message import Message, MessageType
from core.state_machine import ConversationPhase


class ExecutorAgent(BaseAgent):
    """执行器 Agent，负责：
    - 在隔离环境中执行 Engineer 生成的代码
    - 捕获执行输出和错误
    - 返回结果给 Manager 或错误给 Engineer
    """

    # 危险代码模式
    DANGEROUS_PATTERNS = [
        r'os\.system\s*\(',
        r'subprocess\.(call|run|Popen)\s*\(',
        r'eval\s*\(',
        r'exec\s*\(',
        r'__import__\s*\(',
        r'importlib\.import_module',
        r'requests\.(get|post|put|delete|patch)',
        r'urllib\.',
        r'wget\.',
        r'socket\.',
        r'shutil\.rmtree',
        r'os\.remove\s*\(',
        r'os\.unlink\s*\(',
        r'os\.rmdir\s*\(',
    ]

    def __init__(self, model: str, api_key: str, base_url: str, system_prompt: str, code_timeout: int = 300):
        super().__init__(name="Executor", system_prompt=system_prompt, model=model, api_key=api_key, base_url=base_url)
        self._code_timeout = code_timeout
        # 项目根目录（agents/executor_agent.py → ../ 即项目根）
        self._project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def handle_message(self, message: Message) -> Message:
        self.session.state_machine.force_transition(ConversationPhase.EXECUTOR_RUNNING)

        if message.msg_type != MessageType.CODE or not message.content.strip():
            return Message(
                sender=self.name,
                receiver="Engineer",
                content="错误：收到空代码内容",
                msg_type=MessageType.ERROR,
            )

        code = message.content

        # 安全检查
        violations = self._safety_check(code)
        if violations:
            return Message(
                sender=self.name,
                receiver="Engineer",
                content=f"安全检查失败，代码包含危险操作：\n" + "\n".join(f"  - {v}" for v in violations),
                msg_type=MessageType.ERROR,
            )

        # 数据溯源检查
        external_sources = self._check_external_data(code)
        if external_sources:
            return Message(
                sender=self.name,
                receiver="Engineer",
                content=f"代码包含外部数据源引用，只允许使用本地数据：\n" + "\n".join(f"  - {s}" for s in external_sources),
                msg_type=MessageType.ERROR,
            )

        # 语法预检查
        syntax_error = self._check_syntax(code)
        if syntax_error:
            return Message(
                sender=self.name,
                receiver="Engineer",
                content=f"代码语法错误，请修复：\n{syntax_error}\n\n请确保所有字符串都已正确闭合，特别是中文标题字符串。",
                msg_type=MessageType.ERROR,
            )

        # 内存安全预检查
        mem_warning = self._check_memory_safety(code)
        if mem_warning:
            return Message(
                sender=self.name,
                receiver="Engineer",
                content=f"内存安全警告 — 请改为分块处理避免 OOM：\n{mem_warning}",
                msg_type=MessageType.ERROR,
            )

        # 执行代码
        output, error = self._execute_code(code)

        if error:
            return Message(
                sender=self.name,
                receiver="Engineer",
                content=f"代码执行出错：\n{error}",
                msg_type=MessageType.ERROR,
            )

        return Message(
            sender=self.name,
            receiver="Manager",
            content=output,
            msg_type=MessageType.RESULT,
            metadata={"execution_success": True,
                      "files_generated": getattr(self, "_last_result_files", [])},
        )

    def _safety_check(self, code: str) -> list:
        """检查代码是否包含危险操作"""
        violations = []
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                violations.append(f"危险模式: {pattern}")
        return violations

    def _check_external_data(self, code: str) -> list:
        """检查代码是否尝试从外部获取数据"""
        external = []
        # 检查 URL/HTTP
        if re.search(r'https?://', code):
            external.append("检测到URL引用")
        if re.search(r'(requests|urllib|wget|httpx)\.', code):
            external.append("检测到网络请求库使用")
        if re.search(r'(download|fetch|scrape|crawl)', code, re.IGNORECASE):
            external.append("检测到下载/抓取操作")
        return external

    def _execute_code(self, code: str) -> tuple:
        """在子进程中执行代码，返回 (output, error)。
        临时文件保存到 temp/ 文件夹，不自动删除，便于排查问题。"""
        tmp_path = None
        start_time = time.time()
        self._last_result_files = []
        try:
            # 写入临时文件到 temp/ 文件夹（使用项目根目录绝对路径）
            temp_dir = os.path.join(self._project_root, "temp")
            results_dir = os.path.join(self._project_root, "results")
            task_dir = os.path.join(results_dir, self.session.result_subdir)
            os.makedirs(temp_dir, exist_ok=True)
            os.makedirs(task_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix='.py', prefix='ursa_exec_', dir=temp_dir)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(code)

            # 记录执行前的文件列表（检测新生成文件）
            from tools.result_audit import snapshot_result_files
            results_before = snapshot_result_files(results_dir)

            # 构建子进程环境变量，注入 conda 环境的 DLL 搜索路径
            # Windows 下 GDAL/rasterio 需要 GDAL_DATA/PROJ_DATA 等才能正确加载共享 DLL
            _conda_prefix = os.path.dirname(sys.executable)
            _conda_library = os.path.join(_conda_prefix, "Library")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            env["URSA_RESULTS_DIR"] = os.path.abspath(task_dir)
            if os.path.isdir(_conda_library):
                env["CONDA_PREFIX"] = _conda_prefix
                _gdal_data = os.path.join(_conda_library, "share", "gdal")
                if os.path.isdir(_gdal_data):
                    env["GDAL_DATA"] = _gdal_data
                _gdal_drivers = os.path.join(_conda_library, "lib", "gdalplugins")
                if os.path.isdir(_gdal_drivers):
                    env["GDAL_DRIVER_PATH"] = _gdal_drivers
                _proj_data = os.path.join(_conda_library, "share", "proj")
                if os.path.isdir(_proj_data):
                    env["PROJ_DATA"] = _proj_data
                    env["PROJ_NETWORK"] = "ON"
                # 将 conda Library/bin 等加入 PATH 首部，确保共享 DLL 优先被发现
                _path_entries = [
                    os.path.join(_conda_library, "bin"),
                    os.path.join(_conda_library, "mingw-w64", "bin"),
                    os.path.join(_conda_library, "usr", "bin"),
                ]
                _existing_path = env.get("PATH", "")
                _path_add = os.pathsep.join(p for p in _path_entries if os.path.isdir(p))
                if _path_add:
                    env["PATH"] = _path_add + os.pathsep + _existing_path

            # 在项目根目录执行子进程，确保相对路径 data/、results/ 正确解析
            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                encoding='utf-8',
                errors='replace',
                timeout=self._code_timeout,
                cwd=self._project_root,
                env=env,
            )

            output = result.stdout.strip()
            stderr = result.stderr.strip()

            # 过滤无害的第三方库 stderr 警告（如 GDAL_DATA 未设置等）
            _HARMLESS_WARNINGS = [
                r'^Warning \d+: Cannot find gdalvrt\.xsd.*$',
                r'^Warning \d+: Cannot find .*\.xsd.*$',
                r'^Warning \d+: .*GDAL_DATA.*$',
            ]
            if stderr:
                for pattern in _HARMLESS_WARNINGS:
                    stderr = re.sub(pattern, '', stderr, flags=re.MULTILINE).strip()

            duration_ms = (time.time() - start_time) * 1000

            # 检测新生成的文件
            results_after = snapshot_result_files(results_dir)
            new_files = sorted(name for name, signature in results_after.items()
                               if results_before.get(name) != signature)
            self._last_result_files = new_files
            outside_files = [name for name in new_files if not name.startswith(self.session.result_subdir + "/")]

            # 执行追踪
            status = "error" if result.returncode != 0 or outside_files else "success"
            trace_data = {
                "exit_code": result.returncode,
                "temp_file": os.path.basename(tmp_path) if tmp_path else "",
                "stdout_preview": output[:500],
                "files_generated": new_files,
                "result_directory": self.session.result_relative_dir,
            }
            if self.session and hasattr(self.session, 'tracer'):
                tracer = self.session.tracer
                # 从 stdout 检测工具调用
                tools_detected = tracer.detect_tools_in_stdout(output)
                if tools_detected:
                    trace_data["tools_detected"] = len(tools_detected)
                    trace_data["tool_names"] = tools_detected
                phase = self.session.state_machine.get_current_phase().value if self.session.state_machine else ""
                tracer.add_event(
                    event_type="code_execution",
                    agent=self.name,
                    phase=phase,
                    data=trace_data,
                    duration_ms=duration_ms,
                    status=status,
                )

            if result.returncode != 0:
                if stderr:
                    return output, stderr
                return output, f"进程退出码: {result.returncode}"

            if outside_files:
                return output, (
                    f"产物未保存到本次任务目录 {self.session.result_relative_dir}："
                    + ", ".join(outside_files)
                    + "。请改用 os.environ['URSA_RESULTS_DIR'] 作为所有输出路径的根目录后重试。"
                )

            return f"本次任务结果目录：{self.session.result_relative_dir}\n" + (output or "代码执行完成，无输出。"), ""

        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000
            if self.session and hasattr(self.session, 'tracer'):
                self.session.tracer.add_event(
                    event_type="code_execution",
                    agent=self.name,
                    data={"error": "超时 (120秒)", "temp_file": os.path.basename(tmp_path) if tmp_path else ""},
                    duration_ms=duration_ms,
                    status="error",
                )
            return "", "代码执行超时 (120秒)"
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            if self.session and hasattr(self.session, 'tracer'):
                self.session.tracer.add_event(
                    event_type="code_execution",
                    agent=self.name,
                    data={"error": str(e), "temp_file": os.path.basename(tmp_path) if tmp_path else ""},
                    duration_ms=duration_ms,
                    status="error",
                )
            return "", f"执行异常: {e}"
        # 不再自动删除临时文件，保留在 temp/ 方便排查

    def _check_memory_safety(self, code: str) -> str:
        """
        预检查代码是否存在大影像全图内存操作风险。
        检测模式：全图 read() 后跟全尺寸数组分配，且无分块/窗口操作。
        """
        issues = []

        # 检测全图读取后直接创建大数组
        has_full_read = bool(re.search(r'\.read\(\)', code))
        has_full_read_band = bool(re.search(r'\.read\(\s*\d+\s*\)', code))
        has_large_zeros = bool(re.search(
            r'np\.(zeros|ones|empty|full)\s*\(\s*\(\s*(?:n_bands|len\(.*?\)|band_count)\s*,\s*(?:n_pixels|arr.*?\.size|.*?\.shape\[0\]\s*\*\s*.*?\.shape\[1\])',
            code
        ))
        has_window = bool(re.search(r'window\s*=|window=\)|block_size|chunk', code, re.IGNORECASE))
        has_read_window = bool(re.search(r'\.read\(\s*\d+\s*,\s*.*window', code))
        has_block_iter = bool(re.search(r'(block|chunk|tile|row)_iter|for.*block|for.*chunk', code, re.IGNORECASE))

        if (has_full_read or has_full_read_band) and has_large_zeros and not (has_window or has_read_window or has_block_iter):
            issues.append(
                "检测到全图读取后创建 (波段数 × 总像素数) 的大型数组，"
                "影像尺寸较大时会导致内存溢出 (OOM)。"
            )

        # 检测 reshape 操作将整景展平为一维
        if re.search(r'\.reshape\s*\(\s*-1\s*,\s*\d+\s*\)', code) and (has_full_read or has_full_read_band):
            if not (has_window or has_block_iter):
                issues.append(
                    "检测到全图 reshape(-1, N) 操作展平整个影像，"
                    "可能在后续矩阵运算中创建超大中间数组。"
                )

        # 检测 float64 大数组 — 双倍内存
        float64_large = re.findall(
            r'np\.(zeros|ones|empty)\s*\(\s*\([^)]+\)\s*,\s*dtype\s*=\s*np\.float64\s*\)',
            code
        )
        if float64_large and not (has_window or has_block_iter):
            issues.append(
                "检测到 float64 类型大数组分配（每个像素 8 字节），"
                "建议改用 float32（4 字节）可减半内存占用。"
            )

        # 检测 matplotlib 可视化时未下采样（全分辨率传 imshow 内存效率低）
        has_imshow = bool(re.search(r'\.(?:imshow|pcolormesh)\s*\(', code))
        has_read_no_outshape = bool(re.search(r'\.read\(\s*\d*\s*\)', code))
        has_vis_downsample = bool(re.search(
            r'(?:out_shape|max_dim|display_dim|vis_dim|disp_|downsample|resample|_vis\b|_disp\b|_display\b)',
            code, re.IGNORECASE
        ))
        if has_imshow and has_read_no_outshape and not has_vis_downsample:
            issues.append(
                "检测到 matplotlib 可视化使用全分辨率数据（read() 无 out_shape 参数 + imshow()），"
                "大尺寸数组传入 imshow() 会消耗大量内存且远超显示分辨率需求。"
                "建议先用 rasterio out_shape 参数下采样至 2048×2048 以内再传给 imshow()。"
            )

        if not issues:
            return ""

        return "\n".join(f"  - {issue}" for issue in issues) + (
            "\n\n修复方案：\n"
            "  1. 使用 rasterio 的 window 参数分块读取影像\n"
            "  2. 对每个 block 分别处理后再拼接结果\n"
            "  3. 避免创建 (波段数 × 总像素数) 形状的中间数组\n"
            "  4. 能用 float32 就不要用 float64"
        )

    def _check_syntax(self, code: str) -> str:
        """预检查 Python 语法，返回错误信息或空字符串"""
        import ast
        try:
            ast.parse(code)
            return ""
        except SyntaxError as e:
            return f"第 {e.lineno} 行: {e.msg}\n上下文: {e.text[:200] if e.text else ''}"
