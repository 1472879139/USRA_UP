"""对现有核验错误作确定性归类，不修改核验规则或读取器。"""


def diagnose_artifact_failure(audit: dict) -> dict:
    """返回可序列化诊断；跨目录产物问题直接阻断，其余交由任务代码修复。"""
    issues = []
    for error in audit.get("errors", []):
        if any(marker in error for marker in ("不在本次任务目录中", "is not in the subpath", "not in the subpath")):
            category = "task_boundary"
            guidance = "产物不属于本任务目录，停止自动修复，不得引用其他任务文件。"
        elif "读取产物失败" in error:
            category = "artifact_read"
            guidance = "按真实数据修复文件编码、格式或字段类型；不要修改宿主读取器。"
            if "could not convert string to float" in error:
                guidance += (
                    "若错误来自确实不可计算的可选面积，不能填0或编造面积；"
                    "现有接口允许省略面积列，可在CSV中省略该可选列并在报告说明原因。"
                    "必需的像元数、占比和类别编号仍须真实有效，不能靠删除必需字段逃避核验。"
                )
        elif any(marker in error for marker in ("像元", "占比", "类别", "面积")):
            category = "statistics_mismatch"
            guidance = "从本任务数值栅格重算统计，检查类别编号、有效像元分母和单位；保留未知类别。"
        elif any(marker in error for marker in ("缺失或为空", "没有生成", "没有本次新增")):
            category = "missing_output"
            guidance = "修复代码并实际生成所需文件；不能把旧任务产物或只有文字的承诺当作交付。"
        else:
            category = "artifact_error"
            guidance = "依据错误定位本任务产物问题；无法从现有数据解决时明确说明，禁止捏造结果。"
        issues.append({"category": category, "error": error, "guidance": guidance})
    return {"repairable": bool(issues) and all(i["category"] != "task_boundary" for i in issues),
            "issues": issues}
