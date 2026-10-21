"""GUI 组件 — Gradio 界面的可复用组件"""

import gradio as gr


def create_header() -> gr.HTML:
    """页面头部"""
    return gr.HTML("""
    <div style="text-align:center; padding:20px; background:linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); border-radius:15px; margin-bottom:20px;">
        <h1 style="color:#e94560; margin:0; font-size:2.2em;">🌍 URSA-UP <span style="font-size:0.5em; color:#8892b0;">v1.14</span></h1>
        <p style="color:#a8a8d0; margin:10px 0 0 0; font-size:1.1em;">
            遥感分析多智能体系统 · 升级版 | 监督监测 · 可视化交互 · 智能托管
        </p>
    </div>
    """)


def create_progress_bar() -> gr.HTML:
    """流程进度条"""
    return gr.HTML("""
    <div id="progress-container" style="margin:15px 0;">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:5px;">
            <div id="step-init" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">⚡</div>
                <div>初始化</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-input" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">👤</div>
                <div>用户输入</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-confirm" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">📋</div>
                <div>Manager确认</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-scientist" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">🔬</div>
                <div>Scientist</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-engineer" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">⚙️</div>
                <div>Engineer</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-executor" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">▶️</div>
                <div>Executor</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-supervisor" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">🛡️</div>
                <div>监督</div>
            </div>
            <div style="color:#999;">→</div>
            <div id="step-done" class="step-item" style="text-align:center; padding:8px 12px; border-radius:8px; background:#e0e0e0; font-size:12px; min-width:80px;">
                <div style="font-weight:bold;">✅</div>
                <div>完成</div>
            </div>
        </div>
    </div>
    """)


CSS_STYLE = """
<style>
.step-active { background: #4CAF50 !important; color: white !important; box-shadow: 0 0 10px rgba(76,175,80,0.5); }
.step-warning { background: #FF9800 !important; color: white !important; box-shadow: 0 0 10px rgba(255,152,0,0.5); }
.step-error { background: #f44336 !important; color: white !important; box-shadow: 0 0 10px rgba(244,67,54,0.5); }
.review-panel { background:#1a1a2e; color:#e0e0e0; padding:15px; border-radius:10px; margin:10px 0; border:1px solid #333; }
.review-ok { color:#4CAF50; }
.review-warn { color:#FF9800; }
.review-error { color:#f44336; }
.auto-pilot-on { background:#4CAF50 !important; }
.chat-user { background:#e3f2fd; border-left:3px solid #2196F3; padding:10px; margin:5px 0; border-radius:5px; }
.chat-assistant { background:#f5f5f5; border-left:3px solid #4CAF50; padding:10px; margin:5px 0; border-radius:5px; }
.chat-system { background:#fff3e0; border-left:3px solid #FF9800; padding:10px; margin:5px 0; border-radius:5px; }
.chat-review { background:#fce4ec; border-left:3px solid #e94560; padding:10px; margin:5px 0; border-radius:5px; }
</style>
"""
