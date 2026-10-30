"""已安装远程模型的统一分派；输入适配失败与执行失败不静默换方法。"""
from . import prithvi_workflow as water
from . import terramind_workflow as landcover


def select_remote_tool(session,project_root=None):
    plan=landcover.select_remote_tool(session,project_root)
    return plan if plan is not None else water.select_remote_tool(session,project_root)


def provider(plan):
    return landcover if plan.get("name")==landcover.protocol.VERSION else water


def describe_remote_plan(session,plan):
    return provider(plan).describe_remote_plan(session,plan)


def confirmation_text(plan):
    return provider(plan).confirmation_text(plan)


def tool_message(session,request):
    return provider(request).tool_message(session,request)


def attach_remote_audit(session,project_root):
    return provider(session.remote_tool_plan).attach_remote_audit(session,project_root)


def enrich_delivery(delivery,audit):
    return landcover.enrich_delivery(delivery,audit) if audit.get("remote_landcover") else water.enrich_delivery(delivery,audit)
