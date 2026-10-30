"""Landsat 8 已校正辅助物理量的单通道辐射传输重建；不读取答案。"""
import csv
import json
import math
from pathlib import Path

import numpy as np
import rasterio

from .artifact_paths import file_hash

VERSION = 'lst-rte-v1'
BANDS = ('thermal_radiance', 'upwelling_radiance', 'downwelling_radiance',
         'atmospheric_transmittance', 'surface_emissivity', 'QA_PIXEL')
LIMITATION = ('这是过境时刻的地表温度，不能作为气温或危险高温预警。只支持已提供大气辅助量和发射率的Landsat 8数据；'
              '30米输出网格不等于热红外100米原生观测分辨率。云边缘、发射率及大气误差仍可能影响结果。')


def inspect_input(path):
    path = Path(path)
    sidecar = path.parent/'metadata.json'
    if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 256 * 1024:
        raise ValueError('温度分析缺少可核对的metadata.json（波段、物理单位与热常数）')
    metadata = json.loads(sidecar.read_text(encoding='utf-8-sig'))
    if not isinstance(metadata, dict) or metadata.get('platform') != 'landsat-8':
        raise ValueError('当前温度工具仅支持已声明的landsat-8辅助物理量')
    if metadata.get('bands_in_order') != list(BANDS):
        raise ValueError('温度工具需要热辐亮度、上行/下行辐亮度、透过率、发射率和QA_PIXEL')
    encoding = str(metadata.get('encoding', ''))
    if not all(part in encoding for part in ('已应用', '物理量', 'QA_PIXEL', '整数')):
        raise ValueError('温度输入必须明确声明已应用缩放的物理量，QA_PIXEL保留整数')
    units = metadata.get('band_units', [])
    if (len(units) != 6 or units[:3] != ['watt/steradian/square_meter/micrometer'] * 3 or
            units[3] not in ('flag', 'dimensionless', '1') or
            units[4] not in ('emissivity coefficient', 'dimensionless', '1') or units[5] != 'bit index'):
        raise ValueError('温度辅助量的单位不符合当前工具契约')
    constants = metadata.get('thermal_constants_from_scene_MTL', {})
    if not isinstance(constants, dict) or any(not isinstance(constants.get(key), (int, float)) or
            isinstance(constants.get(key), bool) or not math.isfinite(constants[key]) or constants[key] <= 0
            for key in ('K1_BAND_10', 'K2_BAND_10')):
        raise ValueError('缺少来自场景MTL的正数热常数K1/K2')
    with rasterio.open(path) as src:
        if src.count != 6 or tuple(src.descriptions) != BANDS:
            raise ValueError('实际温度波段描述与元数据不一致')
        if src.width * src.height > 4_000_000:
            raise ValueError('当前温度工具只支持不超过400万像元的小型影像')
        if src.scales != (1.,) * 6 or src.offsets != (0.,) * 6:
            raise ValueError('已声明物理量却另有栅格缩放，需先核实，不能重复缩放')
        if not src.crs or not src.crs.is_projected or src.crs.linear_units not in ('metre', 'meter'):
            raise ValueError('温度工具需要已核对的米制投影网格')
    return {'sha256': file_hash(path), 'metadata_sha256': file_hash(sidecar)}


def calculate(path):
    """L=τ[εB(T)+(1-ε)L↓]+L↑；保留缺测，不截断温度以改善评分。"""
    inspect_input(path)
    metadata = json.loads((Path(path).parent/'metadata.json').read_text(encoding='utf-8-sig'))
    constants = metadata['thermal_constants_from_scene_MTL']
    with rasterio.open(path) as src:
        values = src.read(masked=True).astype('float64')
        profile = src.profile.copy()
    available = (~np.ma.getmaskarray(values) & np.isfinite(values.data)).all(axis=0)
    radiance, up, down, tau, emis, qa = values.data
    qa_valid = np.isfinite(qa) & (qa >= 0) & (qa <= 65535) & (qa == np.floor(qa))
    qa_bits = np.where(qa_valid, qa, 0).astype('uint16')
    qa_ok = qa_valid & ((qa_bits & 63) == 0)
    physics = (radiance > 0) & (up >= 0) & (down >= 0) & (tau > 0) & (tau <= 1) & (emis > 0) & (emis <= 1)
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        blackbody = (radiance - up - tau * (1 - emis) * down) / (tau * emis)
        kelvin = constants['K2_BAND_10'] / np.log1p(constants['K1_BAND_10'] / blackbody)
    physics &= (blackbody > 0) & np.isfinite(kelvin) & (kelvin > 0)
    valid = available & qa_ok & physics
    if not valid.any():
        raise ValueError('温度输入经过质量位与物理条件检查后没有可用像元，不能输出假温度')
    result = np.full(valid.shape, np.nan, dtype='float32')
    result[valid] = (kelvin[valid] - 273.15).astype('float32')
    data = result[valid].astype('float64')
    percentile = float(np.percentile(data, 90))
    # 用严格大于P90；等温影像没有相对高温区，不强行标出10%。
    hot = valid & (result > percentile)
    rows, cols = np.where(hot)
    position = '没有明显的相对高温区（温度相同或高于P90的像元不足）'
    if len(rows):
        vertical = '上部' if rows.mean() < (result.shape[0]-1)/3 else '下部' if rows.mean() > 2*(result.shape[0]-1)/3 else '中部'
        horizontal = '左侧' if cols.mean() < (result.shape[1]-1)/3 else '右侧' if cols.mean() > 2*(result.shape[1]-1)/3 else '中间'
        position = f'相对高温像元的平均图内位置在{vertical}{horizontal}，具体分布请看图中轮廓'
    method = {'tool_version': VERSION, 'unit': 'degC', 'input_sha256': file_hash(path),
              'total_pixels': int(result.size), 'valid_pixels': int(valid.sum()),
              'valid_fraction': float(valid.mean()),
              'excluded': {'unavailable': int((~available).sum()),
                           'qa': int((available & ~qa_ok).sum()),
                           'physics': int((available & qa_ok & ~physics).sum())},
              'min_celsius': float(data.min()), 'max_celsius': float(data.max()),
              'mean_celsius': round(float(data.mean()), 6), 'p90_celsius': percentile,
              'hot_pixels': int(hot.sum()), 'hot_position': position,
              'hot_rule': '仅有效像元温度严格大于本图P90，表示相对高温，非风险阈值',
              'formula': 'B=(L-up-tau*(1-emissivity)*down)/(tau*emissivity); C=K2/log(1+K1/B)-273.15',
              'thermal_constants': constants, 'qa_excluded_bits': list(range(6)),
              'unit_note': '透过率按无量纲0..1处理；兼容旧说明flag，不按二值量处理。',
              'limitation': LIMITATION}
    return result, method, profile


def run_temperature(path, folder):
    result, method, profile = calculate(path)
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    profile.update(driver='GTiff', count=1, dtype='float32', nodata=np.nan)
    with rasterio.open(folder/'final_lst_celsius.tif', 'w', **profile) as dst:
        dst.write(result, 1); dst.set_band_description(1, 'land_surface_temperature'); dst.set_band_unit(1, 'degC')
    with (folder/'temperature_statistics.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['统计项', '数值', '单位'])
        for key in ('min_celsius', 'mean_celsius', 'max_celsius', 'p90_celsius'):
            writer.writerow([key, method[key], 'degC'])
        writer.writerow(['valid_pixels', method['valid_pixels'], 'pixels'])
        writer.writerow(['valid_fraction', method['valid_fraction'], 'fraction'])
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font = FontProperties(family=['Microsoft YaHei', 'DejaVu Sans'])
    fig, ax = plt.subplots(figsize=(8, 5), layout='constrained')
    ax.set_facecolor('#d5dbe2')
    image = ax.imshow(np.ma.masked_invalid(result), cmap='inferno', interpolation='nearest')
    hot = np.isfinite(result) & (result > method['p90_celsius'])
    if hot.any() and not hot.all() and min(result.shape) > 1:
        ax.contour(hot.astype(int), levels=[.5], colors=['#00e5ff'], linewidths=1)
    ax.set_title('地表温度（℃）｜青色轮廓：本图相对高温\n灰色：被排除或缺测；相对高温不表示危险等级', fontproperties=font)
    ax.set_xlabel('图内列位置', fontproperties=font); ax.set_ylabel('图内行位置', fontproperties=font)
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label('摄氏度（地表温度，非气温）', fontproperties=font)
    fig.savefig(folder/'final_lst_preview.png', dpi=130); plt.close(fig)
    (folder/'method.json').write_text(json.dumps(method, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return method
