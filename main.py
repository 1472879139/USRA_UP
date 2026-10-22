#!/usr/bin/env python3
"""
URSA-UP — 遥感分析多智能体系统 (升级版)

升级特性：
  1. 监督 Agent 全程监测 — 数据溯源、死锁检测、一键托管
  2. 可视化交互 — Gradio 多面板 GUI，流程清晰可见
  3. 高级权限 — 监督 Agent 可自动终止异常对话

用法:
  python main.py              # 启动 Gradio 可视化界面
  python main.py --cli        # 命令行模式
  python main.py --port 8080  # 指定端口
"""

import os
import sys

# === Windows 编码修复（必须在任何其他导入之前） ===
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdin.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8'] = '1'

import argparse

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_cli():
    """命令行模式"""
    import json
    from core.session import Session
    from core.state_machine import ConversationPhase
    from agents.user_agent import UserAgent
    from agents.manager_agent import ManagerAgent
    from agents.scientist_agent import ScientistAgent
    from agents.engineer_agent import EngineerAgent
    from agents.executor_agent import ExecutorAgent
    from agents.supervisor_agent import SupervisorAgent
    from agents.proxy_agent import ProxyAgent

    # 加载配置
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)

    model = config["model"]
    api_key = config["api_key"]
    base_url = config["base_url"]

    # 加载提示词
    def load_prompt(name):
        with open(f"prompts/{name}_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()

    def is_fatal_data_error(error_text: str) -> bool:
        """判断执行错误是否为数据缺失类致命问题（v1.14：排除语法错误和一般运行错误）"""
        syntax_indicators = [
            r'SyntaxError', r'unterminated string', r'was never closed',
            r'invalid syntax', r'EOL while scanning',
        ]
        if any(re.search(p, error_text) for p in syntax_indicators):
            return False
        fatal_patterns = [
            r'FileNotFoundError', r'No such file or directory',
            r'✗.*文件不存在', r'✗.*文件未找到',
            r'错误:.*文件夹.*不存在',
            r'[✗×].*不存在.*\.(?:tif|tiff)',
        ]
        return any(re.search(p, error_text) for p in fatal_patterns)

    kw = {"model": model, "api_key": api_key, "base_url": base_url}

    from tools.io_kit import print_data_summary, scan_data_directory

    print("=" * 60)
    print("  URSA-UP 遥感分析多智能体系统 v1.14")
    print("=" * 60)
    print(f"  模型: {model}")
    print("  特性: 监督监测 | 数据溯源 | 死锁检测 | 一键托管")
    print("=" * 60)
    print()

    # 启动时扫描 data/ 目录
    print_data_summary()

    # SupervisorAgent 智能分析：这些数据可以用于哪些遥感分析？
    temp_supervisor = SupervisorAgent(system_prompt=load_prompt("supervisor"), **kw)
    scan_result = scan_data_directory()
    if scan_result["success"] and scan_result["data"]["file_count"] > 0:
        print()
        print("=" * 60)
        print("  🧠 智能分析：这些影像可以用于哪些分析？")
        print("=" * 60)
        analysis = temp_supervisor.analyze_data_potential(scan_result)
        print(f"  {analysis}")
        print("=" * 60)
        print()

    # 创建会话
    session = Session()
    session.start()

    # 创建 Agents
    engineer_max_tokens = config.get("engineer_max_tokens", 32768)
    code_timeout = config.get("code_timeout", 300)
    agents = {
        "user": UserAgent(**kw),
        "manager": ManagerAgent(system_prompt=load_prompt("manager"), **kw),
        "scientist": ScientistAgent(system_prompt=load_prompt("scientist"), **kw),
        "engineer": EngineerAgent(system_prompt=load_prompt("engineer"), max_tokens=engineer_max_tokens, **kw),
        "executor": ExecutorAgent(system_prompt=load_prompt("executor"), code_timeout=code_timeout, **kw),
        "supervisor": SupervisorAgent(system_prompt=load_prompt("supervisor"), **kw),
    }

    for agent in agents.values():
        session.register_agent(agent)

    # 添加监督拦截器
    proxy = ProxyAgent(agents["supervisor"])
    session.bus.add_interceptor(proxy)

    # 广播消息打印
    def print_msg(msg):
        if msg.sender != "SupervisorAgent":
            print(f"\n  [{msg.sender} → {msg.receiver}] {msg.content[:200]}")

    session.bus.subscribe("broadcast", print_msg)

    print("使用说明：")
    print("  1. 输入遥感分析需求")
    print("  2. 输入 'approve' 确认 → 自动分析")
    print("  3. 输入 'auto' 启用一键托管模式")
    print("  4. 输入 'end' 终止")
    print("-" * 60)
    print()

    user_agent = agents["user"]
    manager = agents["manager"]
    scientist = agents["scientist"]
    engineer = agents["engineer"]
    executor = agents["executor"]
    supervisor = agents["supervisor"]
    sm = session.state_machine

    while session.is_active():
        try:
            user_input = input("👤 您: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        if user_input.lower() == "end":
            session.terminate("用户终止")
            break

        if user_input.lower() == "auto":
            supervisor.enable_auto_pilot()
            print("⚡ 一键托管模式已启用")
            continue

        if user_input.lower() == "report":
            print("\n📊 监督报告:")
            print(json.dumps(supervisor.get_monitoring_report(), ensure_ascii=False, indent=2))
            continue

        # 用户输入 → Manager
        msg = user_agent.submit_request(user_input)
        sm.force_transition(ConversationPhase.USER_INPUT)

        # Manager 确认
        sm.force_transition(ConversationPhase.MANAGER_CONFIRM)
        resp = manager.handle_message(msg)
        if resp:
            print(f"\n🤖 Manager: {resp.content[:500]}")

        if supervisor.is_auto_pilot():
            print("\n⚡ [托管模式] 自动确认，跳过手动确认...")
            resp = manager.handle_message(Message(
                sender="User", receiver="Manager",
                content="approve", msg_type=MessageType.CONTROL,
            ))

        if not session.is_active():
            break

        # 等待用户确认或自动确认
        if not supervisor.is_auto_pilot() and (not resp or resp.receiver != "Scientist"):
            confirm = input("\n👤 确认? (approve/end): ").strip()
            if confirm.lower() in ("end", "exit"):
                session.terminate("用户终止")
                break
            resp = manager.handle_message(Message(
                sender="User", receiver="Manager",
                content=confirm, msg_type=MessageType.CONTROL,
            ))

        if not session.is_active():
            break

        if not resp or resp.msg_type == MessageType.ERROR:
            session.terminate(resp.content if resp else "Manager 未返回确认结果")
            break
        if resp.receiver != "Scientist":
            print(f"\n🤖 Manager: {resp.content}")
            continue

        # Scientist — 先由 SupervisorAgent 智能选取相关文件
        print("\n🔬 [Scientist 阶段]")
        confirmed_task = (
            session.confirmed_task
            or session.user_request
            or session.manager_confirmation
        )
        file_selection = supervisor.select_relevant_files(confirmed_task, scan_result)
        file_path_map = {}
        for f in scan_result.get("data", {}).get("files", []):
            fname = f.get("file_name", "")
            fpath = f.get("file_path", "")
            if fname and fpath:
                try:
                    file_path_map[fname] = os.path.relpath(fpath, os.getcwd())
                except ValueError:
                    file_path_map[fname] = fname
        selected_names = [f['file_name'] for f in file_selection.get('relevant_files', [])]
        selected_files = [file_path_map.get(n, n) for n in selected_names]
        print(f"   📁 智能匹配文件: {', '.join(selected_files)}")
        print(f"   📝 {file_selection.get('summary', '')}")

        context_parts = [f"已确认任务: {confirmed_task}"] if confirmed_task else []
        context_parts.append(f"[系统提示] 相关数据文件: {', '.join(selected_files)}")
        # 动态注入选中文件的波段元数据，避免 Scientist 使用硬编码波段索引
        for fname in selected_names:
            file_info = next((f for f in scan_result["data"]["files"] if f["file_name"] == fname), None)
            if file_info:
                band_names = file_info.get("band_names", [])
                bands_str = ", ".join(band_names) if band_names else "未识别波段名称"
                context_parts.append(
                    f"[系统提示] 文件 {fname}: {file_info['bands']} 个波段, "
                    f"分辨率 {file_info['resolution']}, "
                    f"波段名称: {bands_str}"
                )
        context = "\n".join(context_parts)
        sci_msg = Message(sender="Manager", receiver="Scientist", content=context, msg_type=MessageType.TEXT)
        sci_resp = scientist.handle_message(sci_msg)
        if sci_resp:
            print(f"   {sci_resp.content[:3000]}")
        if not sci_resp or sci_resp.msg_type == MessageType.ERROR:
            session.terminate(sci_resp.content if sci_resp else "Scientist 未返回分析结果")
            break

        # Engineer
        print("\n⚙️ [Engineer 阶段]")
        eng_resp = engineer.handle_message(sci_resp)
        if not eng_resp or eng_resp.msg_type == MessageType.ERROR:
            session.terminate(eng_resp.content if eng_resp else "Engineer 未返回代码")
            break

        # Executor（含增量错误修复重试，从config读取次数 + 数据问题上报告警）
        print("\n▶️ [Executor 执行]")
        max_retries = config.get("executor_max_retries", 5)
        last_code = eng_resp.content if eng_resp else ""
        for attempt in range(max_retries):
            exec_resp = executor.handle_message(eng_resp)
            if not exec_resp:
                break
            if exec_resp.msg_type == MessageType.ERROR:
                print(f"   执行失败 (尝试 {attempt+1}/{max_retries}): {exec_resp.content[:3000]}")
                # 检测数据缺失致命问题 → 上报监督Agent
                if is_fatal_data_error(exec_resp.content):
                    print("   检测到数据缺失问题，上报监督Agent...")
                    engineer.report_issue(
                        issue_type="missing_data",
                        description=f"执行代码发现关键数据缺失: {exec_resp.content[:500]}",
                        severity="fatal",
                    )
                    break
                if attempt < max_retries - 1:
                    # 增量修复：发送错误 + 上次代码
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
                        session.terminate(eng_resp.content if eng_resp else "Engineer 未返回修复代码")
                        break
                    if eng_resp:
                        last_code = eng_resp.content
                else:
                    print(f"   错误: 已达最大重试次数")
                    break
            else:
                print(f"   执行成功: {exec_resp.content[:1000]}")
                break

        if not session.is_active():
            break

        # 最终监督
        final_review = supervisor.handle_message(exec_resp)
        if final_review:
            print(f"\n监督核验：{final_review.content}")
        if not session.is_active():
            break

        # 报告
        if exec_resp and exec_resp.msg_type == MessageType.RESULT:
            print("\n📊 [生成报告]")
            report = manager.handle_message(exec_resp)
            if report:
                print(f"   {report.content[:500]}")

        sm.force_transition(ConversationPhase.COMPLETED)
        if session.result_audit.get("status") == "needs_review":
            print("\n分析结束，结果待复核。产物在 results/ 文件夹")
        else:
            print("\n✅ 分析完成！结果在 results/ 文件夹")
        break

    print(f"\n{'=' * 60}")
    print("📊 最终监督报告:")
    print(json.dumps(supervisor.get_monitoring_report(), ensure_ascii=False, indent=2))
    print("=" * 60)


def run_gui(port: int = 7860):
    """启动 Flask 可视化界面"""
    from gui.app import launch_app
    print(f"启动界面: http://127.0.0.1:{port}")
    launch_app(server_port=port)


def main():
    parser = argparse.ArgumentParser(
        description="URSA-UP 遥感分析多智能体系统 (升级版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py              # 启动 Gradio 可视化界面
  python main.py --cli        # 命令行模式
  python main.py --port 8080  # 指定端口
        """,
    )
    parser.add_argument("--cli", action="store_true", help="使用命令行模式")
    parser.add_argument("--port", type=int, default=7860, help="Gradio 服务端口 (默认: 7860)")

    args = parser.parse_args()

    # 确保工作目录正确
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 确保必要目录存在
    os.makedirs("results", exist_ok=True)
    os.makedirs("exports", exist_ok=True)

    # 清理上次运行的临时文件
    import shutil
    temp_dir = "temp"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir, exist_ok=True)

    if args.cli:
        run_cli()
    else:
        run_gui(port=args.port)


if __name__ == "__main__":
    main()
