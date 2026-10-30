"""现有固定工具的少量分派适配；保留冻结的水体实现。"""
from core.message import Message, MessageType
from .water_workflow import select_water_tool, tool_code_message as water_code_message
from .lst_workflow import select_lst_tool, TOOL_NAME
from .lst_tool import VERSION


def select_installed_tool(session):
    return select_lst_tool(session) if session.task_contract.get('task_type') == 'lst' else select_water_tool(session)


def tool_code_message(session, request):
    if session.tool_plan.get('name') != TOOL_NAME:
        return water_code_message(session, request)
    if request != session.tool_plan or request.get('name') != TOOL_NAME or request.get('version') != VERSION:
        return Message(sender='Engineer', receiver='Manager', msg_type=MessageType.ERROR, content='温度工具计划与本次锁定计划不一致')
    code = ("import os\nfrom tools.lst_workflow import run_lst_workflow\n"
            f"run_lst_workflow({request!r}, os.environ['URSA_RESULTS_DIR'])\n")
    return Message(sender='Engineer', receiver='Executor', msg_type=MessageType.CODE, content=code, metadata={'tool_request': request})


def attach_tool_audit(session, project_root):
    if session.tool_plan.get('name') == TOOL_NAME:
        from .lst_workflow import attach_lst_audit
        attach_lst_audit(session, project_root)
    else:
        from .water_audit import attach_water_audit
        attach_water_audit(session, project_root)
