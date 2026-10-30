"""需求完成性检查；可计算的交付要求与语义准确性分开记录。"""
import csv
from pathlib import Path
import re

import numpy as np
import rasterio

from .artifact_paths import confined_path


def is_processing_request(text):
    explicit=re.search(r"(?:帮我|请|需要|想要|给我|为我).{0,12}(?:计算|处理|提取|统计|标出|找出|生成|输出|分类|分析|比较|筛选|去云|检测|重投影)",text)
    if explicit:return True
    if re.search(r"哪里.{0,6}(?:热|凉|水|农田|建筑)|(?:这张|这幅|这些|影像|选中|上传).{0,15}(?:有多少|面积|主要.{0,5}地物|有什么|看看)",text):return True
    if re.search(r"什么|如何|怎么|为什么|解释|区别|原理|公式|介绍|方法",text):return False
    return bool(re.search(r"计算|处理|提取|统计|标出|找出|生成|输出|分类|分析|比较|筛选|去云|检测|重投影|分布图|面积",text))


def infer_contract(texts,input_ids):
    """少量确定规则保底，开放语义要求由规划器补充并保留原文。"""
    text="\n".join(texts);processing=is_processing_request(text)
    if texts and re.search(r"只(?:需|要)?解释|改为.{0,6}(?:解释|说明)|不(?:用|需要)处理.{0,8}解释",texts[-1]):processing=False
    requirements={}
    def add(kind,unit="",target=""):
        requirements[kind]={"kind":kind,"description":{"map":"可查看的本次结果图","table":"本次统计表","raster":"本次结果栅格","area":"要求单位的面积","statistics":"要求的数值统计"}[kind],
                            "unit":unit,"target":target,"input_ids":list(input_ids),"source":"user_text"}
    picture=processing
    for part in texts:
        for clause in re.split(r"[，。；;\n]",part):
            negative=bool(re.search(r"不要|无需|取消|不需要|不用|不必|不输出|不生成|只要数字|只要数值|只要CSV|仅需CSV|仅要CSV|只要统计表",clause,re.I))
            if re.search(r"图|图片|PNG|JPG|CSV|统计表",clause,re.I):
                if negative:picture=False
                elif re.search(r"(?:给|要|需要|输出|生成|保留|增加|提供).{0,12}(?:图|PNG|JPG)",clause,re.I):picture=True
            for word,kind in [(r"CSV|统计表|表格","table"),(r"GeoTIFF|结果栅格|栅格文件","raster"),(r"面积|平方米|平方公里|公顷|m2|㎡","area"),(r"均值|平均|最小|最大|占比|比例|统计","statistics")]:
                if re.search(word,clause,re.I):
                    if negative and not re.search(r"只要|仅需|仅要",clause):requirements.pop(kind,None)
                    else:add(kind,"km2" if "平方公里" in clause else "ha" if "公顷" in clause else "m2" if kind=="area" else "")
    if picture:
        add("map",target="temperature" if re.search(r"温度|哪里.{0,6}(?:热|凉)|\bLST\b",text,re.I) else "")
    return {"version":1,"original_request":texts[0] if texts else "","ordered_revisions":list(texts[1:]),
            "mode":"analysis" if processing else "answer","requirements":list(requirements.values()) if processing else [],"semantic_accuracy":"not_evaluated"}


def validate_requirements(items,approved_inputs):
    if not isinstance(items,list) or len(items)>24:raise ValueError("最多24项需求")
    result=[]
    for item in items:
        if not isinstance(item,dict) or set(item)!={"kind","description","unit","target","input_ids"}:raise ValueError("需求字段应为kind/description/unit/target/input_ids")
        if item["kind"] not in {"map","table","raster","area","statistics","answer"}:raise ValueError("未知需求类型")
        if any(not isinstance(item[k],str) or len(item[k])>500 for k in ("description","unit","target")):raise ValueError("需求说明过长或不是文字")
        if not isinstance(item["input_ids"],list) or any(v not in approved_inputs for v in item["input_ids"]):raise ValueError("需求引用了未批准的输入")
        result.append({**item,"source":"planner"})
    return result


def area_evidence(item,artifacts,output,unit):
    """按声明的源分类重算面积，不信任程序stdout或单独的面积CSV。"""
    evidence=item.get("measurement",{})
    if evidence.get("kind")!="area" or evidence.get("raster_id") not in artifacts:return False
    source=artifacts[evidence["raster_id"]]
    codes=evidence.get("class_codes",[])
    if not codes or not source.get("classes") or any(str(code) not in source["classes"] for code in codes):return False
    with rasterio.open(confined_path(output,source["path"])) as src:
        if not src.crs or not src.crs.is_projected or src.crs.linear_units not in ("metre","meter"):return False
        data=src.read(1,masked=True);pixel=abs(src.transform.a*src.transform.e-src.transform.b*src.transform.d)
        expected=[int(((data.data==code)&~np.ma.getmaskarray(data)).sum())*pixel for code in codes]
    factor={"m2":1,"ha":10000,"km2":1000000}.get(evidence.get("unit"))
    if not factor or evidence.get("unit")!=unit:return False
    with confined_path(output,item["path"]).open(encoding="utf-8-sig",newline="") as stream:
        rows=list(csv.DictReader(stream))
    columns={"m2":("面积_m2","area_m2"),"ha":("面积_ha","area_ha"),"km2":("面积_km2","area_km2")}[unit]
    try:
        values=[float(next(row[k] for k in columns if k in row))*factor for row in rows]
        return len(values)==len(expected) and bool(np.allclose(sorted(values),sorted(expected),rtol=1e-5,atol=.01))
    except (ValueError,StopIteration):return False


def verify_band_means(path,input_ids,inputs):
    """对逐波段均值请求独立读取源栅格，避免只交一行表格就被认为完成。"""
    if not inputs:return False
    with path.open(encoding="utf-8-sig",newline="") as stream:rows=list(csv.DictReader(stream))
    if not rows:return False
    mean_col=next((key for key in rows[0] if key.lower() in {"mean","均值","平均值"}),None)
    band_col=next((key for key in rows[0] if key.lower() in {"band","band_name","波段","波段编号"}),None)
    input_col=next((key for key in rows[0] if key.lower() in {"input_id","输入"}),None)
    if not mean_col or not band_col or (len(input_ids)>1 and not input_col):return False
    for input_id in input_ids:
        if input_id not in inputs:return False
        actual=rows if input_col is None else [row for row in rows if row[input_col]==input_id]
        with rasterio.open(inputs[input_id]) as src:
            if len(actual)!=src.count:return False
            for index in range(1,src.count+1):
                names={str(index),str(src.descriptions[index-1] or '').upper(),"BAND_"+str(index)}
                matches=[row for row in actual if row[band_col].upper() in names]
                if len(matches)!=1:return False
                values=src.read(index,masked=True)
                valid=(~np.ma.getmaskarray(values))&np.isfinite(values.data)
                if not valid.any():return False
                try:
                    expected=float(values.data[valid].mean(dtype="float64"));value=float(matches[0][mean_col])
                    if not np.isfinite(value) or not np.isclose(value,expected,rtol=1e-5,atol=1e-6):return False
                except (ValueError,TypeError):return False
    return True


def check_requirements(contract,artifacts,output,*,inputs=None,evidence_artifacts=None):
    output=Path(output);items=[]
    for requirement in contract["requirements"]:
        kind=requirement["kind"];matches=[]
        for key,item in artifacts.items():
            path=confined_path(output,item["path"])
            if not path.is_file():continue
            met=False
            if kind=="map":
                met=item["kind"]=="image" and item.get("role")=="preview"
                if met and requirement.get("target")=="temperature":
                    related=(evidence_artifacts or artifacts).get(item.get("source_raster_id"),{})
                    met=item.get("producer")=="retrieve_temperature" or related.get("role")=="temperature"
            elif kind=="table":met=item["kind"]=="table"
            elif kind=="raster":met=item["kind"]=="raster"
            elif kind=="area":met=item["kind"]=="table" and area_evidence(item,evidence_artifacts if evidence_artifacts is not None else artifacts,output,requirement.get("unit") or "m2")
            elif kind=="statistics":
                if item["kind"]=="table":
                    try:
                        with path.open(encoding="utf-8-sig",newline="") as stream:
                            rows=list(csv.reader(stream))
                        met=len(rows)>1 and any(re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?",cell.strip()) for row in rows[1:] for cell in row)
                        request="\n".join([contract.get("original_request",""),*contract.get("ordered_revisions",[])])
                        if re.search(r"(?:每个|各个|全部|所有).{0,6}波段.{0,8}(?:均值|平均)|各波段.{0,5}(?:均值|平均)",request) and not re.search(r"分区|地块|区域内",request):
                            met=verify_band_means(path,requirement.get("input_ids",[]),inputs)
                    except UnicodeError:met=False
            if met:matches.append(key)
        covered=set(v for key in matches for v in artifacts[key].get("input_ids",[]))
        enough=bool(matches) and set(requirement.get("input_ids",[]))<=covered
        items.append({**requirement,"met":enough,"artifact_ids":matches,"reason":"有本次实际产物与输入来源证据" if enough else "缺少满足格式、单位或输入覆盖要求的实际产物"})
    return {"complete":all(item["met"] for item in items),"items":items,"semantic_accuracy":"未评估；需求覆盖和数值一致不等于识别正确"}
