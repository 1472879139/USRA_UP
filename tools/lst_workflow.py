"""温度输入锁定、固定执行和无答案产物核验。"""
import json
import re
from pathlib import Path

from .artifact_paths import confined_path, file_hash
from .lst_tool import VERSION, LIMITATION, inspect_input, calculate, run_temperature
from .task_contract import _intent_text

TOOL_NAME = 'lst_rte'
MANIFEST = 'lst_workflow.json'


def select_lst_tool(session):
    if session.task_contract.get('task_type') != 'lst':
        return None
    text = _intent_text('\n'.join([session.user_request, *session.revision_feedback]))
    text = re.sub(r'哪里比较[热凉]', '哪里热', text)
    if re.search(r'气温|水温|变化|前后|对比|比较|模型|深度学习|指定|按照|采用|行政|边界|裁剪|缓冲|'
                 r'只要|只用|仅需|不要|无需|不需要|不输出|矢量|热浪|预警|[<>≥≤]', text, re.I):
        return None
    names = session.task_contract.get('inputs', [])
    root = session.data_context.get('data_root')
    if not root or not names or len(names) > 8:
        return None
    try:
        inputs = [{'file_name': name, **inspect_input(confined_path(root, name))} for name in names]
    except (OSError, ValueError, TypeError):
        return None
    return {'name': TOOL_NAME, 'version': VERSION, 'inputs': inputs,
            'scope': '已提供大气辅助物理量与发射率的Landsat 8地表温度重建；不读取参考答案'}


def run_lst_workflow(plan, results_dir):
    if plan.get('name') != TOOL_NAME or plan.get('version') != VERSION or not plan.get('inputs'):
        raise ValueError('温度工具名称、版本或输入不符合锁定契约')
    root = Path.cwd(); output = Path(results_dir).resolve()
    output.relative_to((root/'results').resolve()); output.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, item in enumerate(plan['inputs'], 1):
        path = confined_path(root/'data', item['file_name'])
        identity = {key: item[key] for key in ('sha256', 'metadata_sha256')}
        if inspect_input(path) != identity:
            raise ValueError('确认后的温度影像或元数据已变化，需要重新规划')
        folder = output/f'input_{index:02}'
        method = run_temperature(path, folder)
        if inspect_input(path) != identity:
            raise ValueError('温度执行过程中输入发生改变，不能交付')
        entries.append({'input': item['file_name'], 'input_sha256': item['sha256'],
                        'label': f'{path.parent.name}/{path.name}', 'method': method,
                        'prediction': (folder/'final_lst_celsius.tif').relative_to(output).as_posix(),
                        'prediction_sha256': file_hash(folder/'final_lst_celsius.tif'),
                        'preview': (folder/'final_lst_preview.png').relative_to(output).as_posix(),
                        'statistics': (folder/'temperature_statistics.csv').relative_to(output).as_posix()})
        print(f"[固定温度工具] {path.name}：有效{method['valid_pixels']}像元，平均{method['mean_celsius']:.2f}摄氏度")
    manifest = {'tool': TOOL_NAME, 'version': VERSION, 'scenes': entries}
    (output/MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return manifest


def attach_lst_audit(session, project_root):
    from .water_audit import _current_file
    import csv
    import numpy as np
    import rasterio
    manifest_path = _current_file(session, project_root, MANIFEST)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError('温度产物清单过大')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    plan = session.tool_plan
    if manifest.get('tool') != TOOL_NAME or manifest.get('version') != VERSION or len(manifest.get('scenes', [])) != len(plan['inputs']):
        raise ValueError('温度产物清单与计划不一致')
    scenes = []
    for entry, item in zip(manifest['scenes'], plan['inputs']):
        source = confined_path(Path(project_root)/'data', item['file_name'])
        if inspect_input(source) != {key: item[key] for key in ('sha256', 'metadata_sha256')}:
            raise ValueError('温度核验时输入已变化')
        if entry['input'] != item['file_name'] or entry['input_sha256'] != item['sha256']:
            raise ValueError('温度产物未绑定锁定输入')
        expected, method, profile = calculate(source)
        if entry['method'] != method:
            raise ValueError('温度统计或方法与独立重算不一致')
        prediction = _current_file(session, project_root, entry['prediction'])
        statistics = _current_file(session, project_root, entry['statistics'])
        _current_file(session, project_root, entry['preview'])
        with statistics.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        expected_rows = [['统计项', '数值', '单位']]
        expected_rows += [[key, str(method[key]), 'degC'] for key in ('min_celsius', 'mean_celsius', 'max_celsius', 'p90_celsius')]
        expected_rows += [['valid_pixels', str(method['valid_pixels']), 'pixels'], ['valid_fraction', str(method['valid_fraction']), 'fraction']]
        method_file = _current_file(session, project_root, str(Path(entry['prediction']).parent/'method.json').replace('\\', '/'))
        if rows != expected_rows or json.loads(method_file.read_text(encoding='utf-8')) != method:
            raise ValueError('温度CSV或方法记录与重算统计不一致')
        if file_hash(prediction) != entry['prediction_sha256']:
            raise ValueError('温度预测哈希不一致')
        with rasterio.open(prediction) as src:
            if (src.count != 1 or src.shape != expected.shape or src.crs != profile['crs'] or
                    not src.transform.almost_equals(profile['transform'], precision=1e-8) or src.units != ('degC',) or
                    not np.array_equal(src.read(1, masked=True).filled(np.nan), expected, equal_nan=True)):
                raise ValueError('温度预测的像元、单位或网格与声明方法不一致')
        if inspect_input(source) != {key: item[key] for key in ('sha256', 'metadata_sha256')}:
            raise ValueError('温度核验过程中输入已变化')
        findings = [f"可分析区域地表温度{method['min_celsius']:.1f}～{method['max_celsius']:.1f}℃，平均{method['mean_celsius']:.1f}℃。",
                    f"有效区域占本图{method['valid_fraction']:.1%}；其余位置没有用0℃填充。",
                    method['hot_position']+'。相对高温表示比本图约九成有效位置更热，不是危险等级。',
                    '本次没有读取参考答案，温度误差尚未验证。']
        scenes.append({'task_type': 'lst', 'label': entry['label'], 'input': entry['input'], **method,
                       'quality_flags': ['incomplete_prediction'] if method['valid_pixels'] < method['total_pixels'] else [],
                       'findings': findings, 'evaluation': {'status': 'unavailable', 'reason': '交卷后外部评价'},
                       'prediction_path': session.result_subdir+'/'+entry['prediction'],
                       'preview_path': session.result_subdir+'/'+entry['preview']})
    session.result_audit['scenes'] = scenes
    session.result_audit['facts'] += '\n[固定温度工具逐幅无答案核验]\n'+LIMITATION+'\n'+'\n'.join(s['label']+'：'+'；'.join(s['findings']) for s in scenes)
    session.tracer.add_event('tool_call', agent='SupervisorAgent', data={'tool_name': 'lst_artifact_audit', 'scenes': scenes}, status='warning')
