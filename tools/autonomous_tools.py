"""自主Agent的专用工具、通用执行器、数据适配与任务验收。"""
from copy import deepcopy
import csv
import json
from pathlib import Path
import re

import numpy as np
import rasterio

from .agent_tools import AgentTools,TOOL_SPECS,spec,field,read_small,ToolFailure
from .artifact_paths import confined_path,file_hash
from .task_acceptance import infer_contract,validate_requirements,check_requirements
from core.tool_call_loop import ToolLoopError


OUTPUT_SCHEMA={"type":"object","properties":{
    "filename":field(),"kind":field(enum=["raster","image","table","data"]),"label":field(),
    "role":field(enum=["data","preview","classification","index","temperature","area"]),
    "classes":{"type":"object","additionalProperties":{"type":"string"}},
    "source_raster":field(),"class_codes":field("array",items=field("integer")),"unit":field(enum=["m2","ha","km2"])},
    "required":["filename","kind","label","role"],"additionalProperties":False}
REQUIREMENT_SCHEMA={"type":"object","properties":{"kind":field(enum=["answer","map","table","raster","area","statistics"]),
    "description":field(),"unit":field(),"target":field(),"input_ids":field("array",items=field())},
    "required":["kind","description","unit","target","input_ids"],"additionalProperties":False}
AUTONOMOUS_SPECS=deepcopy(TOOL_SPECS)+[
    spec("set_task_plan","记录当前目标、回答或分析方式和逐项交付要求；可根据反馈补充，不能删除用户明确要求。",
         {"mode":field(enum=["answer","analysis"]),"objective":field(maxLength=2000),"requirements":field("array",items=REQUIREMENT_SCHEMA,maxItems=24)}),
    spec("run_python","在隔离断网容器中执行Python；可用numpy/rasterio/matplotlib/Pillow及标准库。输入路径从URSA_INPUTS_JSON读取，输出到URSA_RESULTS_DIR。只声明实际输出；报错后可改代码重试，最多4次。面积CSV必须声明source_raster、class_codes、unit以供独立重算。",
         {"code":field(maxLength=32000),"input_ids":field("array",items=field(),maxItems=8),"artifact_ids":field("array",items=field(),maxItems=32),"outputs":field("array",items=OUTPUT_SCHEMA,minItems=1,maxItems=32)}),
    spec("align_raster","对齐两幅已批准影像的网格，或把本次分类转为当地UTM米制投影；不会猜原投影，也不代替精细配准。source_id可为input或artifact；reference_id为输入ID，metric模式填空串。",
         {"source_id":field(),"reference_id":field(),"mode":field(enum=["reference","metric"]),"categorical":field("boolean")}),
    spec("request_clarification","只询问影响当前目标的缺失信息，保留原始需求与已完成产物；不强制用户选择算法或重复approve。",
         {"question":field(maxLength=1200),"missing":field("array",items=field(),minItems=1,maxItems=8)})]
for entry in AUTONOMOUS_SPECS:
    if entry["function"]["name"]=="classify_landcover":entry["function"]["description"]="TerraMind Small地物候选分类；自动核对来源、重排波段、转换已声明反射率，保持原分辨率分块及重叠拼接。支持Sentinel-2 L2A十二波段，不猜来源。"


def validate(schema,value):
    kind=schema.get("type")
    types={"string":str,"array":list,"object":dict,"integer":int,"boolean":bool}
    if kind and type(value) is not types[kind]:raise ValueError("参数类型不符："+kind)
    if "enum" in schema and value not in schema["enum"]:raise ValueError("参数不在允许选项中")
    if kind in ("array","string") and not schema.get("minItems",0)<=len(value)<=schema.get("maxItems",schema.get("maxLength",32000)):raise ValueError("参数长度越界")
    if kind=="array":
        for item in value:validate(schema["items"],item)
    if kind=="object":
        properties=schema.get("properties",{})
        if set(schema.get("required",[]))-set(value):raise ValueError("缺少必需参数")
        for key,item in value.items():
            if key in properties:validate(properties[key],item)
            elif schema.get("additionalProperties") is False:raise ValueError("存在未定义参数")
            elif isinstance(schema.get("additionalProperties"),dict):validate(schema["additionalProperties"],item)


class AutonomousTools(AgentTools):
    def __init__(self,root,output,plan,*,active=None,notify=None):
        super().__init__(root,output,plan)
        self.active=active or (lambda:True);self.notify=notify or (lambda event:None)
        self.contract=infer_contract([plan["request"],*plan["revisions"]],list(plan["inputs"]))
        self.code_runs=0;self.model_runs=[];self.acceptance=None

    def execute(self,name,arguments):
        self.verify_inputs()
        if not self.active():raise ToolLoopError("任务已终止或达到预算")
        try:
            schema=next((item["function"]["parameters"] for item in AUTONOMOUS_SPECS if item["function"]["name"]==name),None)
            if schema is None:raise ValueError("未提供该能力；可选择已有工具或run_python")
            validate(schema,arguments)
            key=name+json.dumps(arguments,sort_keys=True,ensure_ascii=False)
            transient={"inspect_data","list_capabilities","validate_artifacts","finish_task","set_task_plan","request_clarification"}
            if name not in transient and key in self.cache:return deepcopy(self.cache[key])
            before=set(self.artifacts)
            result=getattr(self,"tool_"+name)(**arguments)
            inputs=set(arguments.get("input_ids",[]))
            if arguments.get("input_id") in self.plan["inputs"]:inputs.add(arguments["input_id"])
            for ref in [arguments.get("artifact_id"),arguments.get("source_id"),*arguments.get("artifact_ids",[])]:
                if ref in self.plan["inputs"]:inputs.add(ref)
                elif ref in self.artifacts:inputs.update(self.artifacts[ref].get("input_ids",[]))
            for artifact_id in set(self.artifacts)-before:
                self.artifacts[artifact_id]["input_ids"]=sorted(inputs)
                self.artifacts[artifact_id]["producer"]=name
                if name=="render_map":self.artifacts[artifact_id]["source_raster_id"]=arguments["artifact_id"]
            if isinstance(result.get("artifacts"),dict):result["artifacts"]={k:deepcopy(self.artifacts[k]) for k in result["artifacts"]}
            self.verify_inputs()
            if name not in transient and result.get("ok"):self.cache[key]=deepcopy(result)
            return result
        except ToolLoopError:raise
        except (ValueError,OSError,RuntimeError,KeyError,TypeError) as exc:
            result={"ok":False,"error":{"code":"action_failed","message":str(exc)[:1800] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__,
                    "guidance":"依据具体错误修复参数或Python代码，必要时改用其他方法；不能修改原始需求、输入或核验规则。缺信息可request_clarification。"}}
            if 'before' in locals():
                for artifact_id in set(self.artifacts)-before:self.artifacts.pop(artifact_id)
            self.validated=None
            return result

    def on_text(self,text):
        if not text.strip() or self.contract["mode"]!="answer":return None
        return {"ok":True,"_finish":True,"status":"answered","summary":text,"facts":[text],"artifact_ids":[],"unfulfilled":[],
                "acceptance":{"complete":True,"items":[],"mode":"answer","note":"解释性回答；没有宣称执行影像计算"}}

    def tool_set_task_plan(self,mode,objective,requirements):
        known=infer_contract([self.plan["request"],*self.plan["revisions"]],list(self.plan["inputs"]))
        if known["mode"]=="analysis" and mode=="answer":raise ValueError("用户要求实际处理数据，不能改成纯解释回答；缺信息应询问")
        proposed=validate_requirements(requirements,self.plan["inputs"])
        self.contract={**known,"mode":mode,"objective":objective,"requirements":known["requirements"]+proposed}
        self.validated=None;self.acceptance=None
        self.notify({"event":"task_plan","contract":deepcopy(self.contract)})
        return {"ok":True,"task":self.contract,"note":"原始需求及确定性交付要求保留；计划可补充，验收时逐项核对。"}

    def tool_request_clarification(self,question,missing):
        return {"ok":True,"_finish":True,"status":"needs_input","summary":question,"facts":[question],"unfulfilled":missing,"artifact_ids":list(self.artifacts)}

    def tool_inspect_data(self):
        from .data_adapter import describe
        return {"ok":True,"inputs":[{"input_id":key,"name":item["name"],**describe(self.input(key))} for key,item in self.plan["inputs"].items()],
                "note":"未知的来源或编码不会根据文件名补造；可自动适配的转换保留原图与记录。"}

    def tool_list_capabilities(self):
        from .python_runtime import status
        result=super().tool_list_capabilities();result["python_runtime"]=status()
        result["additional_capabilities"]=["直接解释回答","已声明波段/反射率适配","原分辨率分块及拼接","参考网格对齐","隔离Python执行与错误修复","逐项需求验收"]
        result["note"]="固定工具输入检查为旧固定尺寸；新自主分类支持先自动适配和分块，是否可用由来源检查和实际执行决定。未安装专用模型不排除可编程方法，但代码不能补出缺失观测。"
        return result

    def tiled(self,input_id,method):
        from .model_adapter import run_tiled
        from .terramind_protocol import NAMES
        source=self.input(input_id);folder=self.folder()
        target,manifest=run_tiled(source,folder,method,self.root,active=self.active,notify=self.notify)
        classes={str(i):value for i,value in enumerate(NAMES)} if method=="terramind" else {"0":"其他候选","1":"水体候选"}
        key=self.register(target,"raster",classes=classes,role="classification",label="地物候选分类" if method=="terramind" else "水体候选")
        self.model_runs.append({"input_id":input_id,"folder":folder.relative_to(self.output).as_posix()})
        self.artifacts[key]["input_ids"]=[input_id]
        facts=["已覆盖本次影像的有效区域，保留原分辨率与坐标。地物范围是模型候选结果，识别准确性尚未独立验证。"]
        self.facts.extend(facts)
        result={"ok":True,"raster_id":key,"artifacts":{key:self.artifacts[key]},"facts":facts}
        # 自动准备通俗图表；用户后续可筛选类别，或仅交付要求的表格。
        statistics=self.tool_summarize_raster(key);picture=self.tool_render_map(key)
        for added in [*statistics["artifacts"],*picture["artifacts"]]:self.artifacts[added]["input_ids"]=[input_id]
        result["artifacts"].update(statistics["artifacts"]);result["artifacts"].update(picture["artifacts"])
        return result

    def tool_classify_landcover(self,input_id):return self.tiled(input_id,"terramind")

    def tool_extract_water(self,input_id,method):
        return self.tiled(input_id,"prithvi") if method=="prithvi" else super().tool_extract_water(input_id,method)

    def verify_remote_responses(self):
        from .model_adapter import verify_tiled
        for job in self.model_runs:verify_tiled(self.input(job["input_id"]),confined_path(self.output,job["folder"]))

    def tool_align_raster(self,source_id,reference_id,mode,categorical):
        from .data_adapter import align_raster
        if source_id in self.plan["inputs"]:source=self.input(source_id);item={}
        else:source,item=self.artifact(source_id,True)
        reference=self.input(reference_id) if mode=="reference" else None
        if mode=="metric" and reference_id:raise ValueError("metric模式不需要参考ID")
        folder=self.folder();target=folder/"aligned.tif"
        record=align_raster(source,target,reference=reference,metric=mode=="metric",categorical=categorical)
        key=self.register(target,"raster",classes=item.get("classes"),role=item.get("role","data"),label="空间适配结果")
        return {"ok":True,"raster_id":key,"artifacts":{key:self.artifacts[key]},"adaptation":record}

    def tool_calculate_area(self,artifact_id,class_codes):
        path,item=self.artifact(artifact_id,True)
        with rasterio.open(path) as src:metric=bool(src.crs and src.crs.is_projected and src.crs.linear_units in ("metre","meter"))
        if not metric:
            prepared=self.tool_align_raster(artifact_id,"","metric",True)
            derived=prepared["raster_id"];self.artifacts[derived]["input_ids"]=item.get("input_ids",[])
            artifact_id=derived
        result=super().tool_calculate_area(artifact_id,class_codes)
        unit=next((r.get("unit") or "m2" for r in self.contract["requirements"] if r["kind"]=="area"),"m2")
        for key in result["artifacts"]:
            row=self.artifacts[key]
            if unit in {"ha","km2"}:
                table=confined_path(self.output,row["path"])
                with table.open(encoding="utf-8-sig",newline="") as stream:values=list(csv.reader(stream))
                values[0][-1]="面积_"+unit
                for record in values[1:]:record[-1]=float(record[-1])/(10000 if unit=="ha" else 1000000)
                with table.open("w",encoding="utf-8-sig",newline="") as stream:csv.writer(stream).writerows(values)
                row["sha256"]=file_hash(table)
            row["measurement"]={"kind":"area","raster_id":artifact_id,"class_codes":class_codes,"unit":unit}
        return result

    def tool_summarize_raster(self,artifact_id):
        result=super().tool_summarize_raster(artifact_id);stats=result["statistics"]
        if "classes" in stats:
            self.facts.extend(f"{row['name']}候选：{row['pixels']}像元，占有效区域{row['percent']:.2f}%。" for row in stats["classes"] if row["pixels"])
        else:self.facts.append(f"实际栅格统计：均值{stats['mean']:.6g}，范围{stats['min']:.6g}～{stats['max']:.6g}；共{stats['valid_pixels']}个有效像元。")
        return result

    def tool_run_python(self,code,input_ids,artifact_ids,outputs):
        from .python_runtime import run
        if self.code_runs>=4:raise ValueError("本任务代码执行已达4次上限；请交付部分结果或说明未完成项")
        inputs={key:self.input(key) for key in input_ids}
        inputs.update({key:self.artifact(key)[0] for key in artifact_ids})
        for path in inputs.values():
            if Path(path).suffix.lower() in {'.tif','.tiff'}:
                with rasterio.open(path) as src:
                    if src.count>20 or src.width*src.height>4_000_000:raise ValueError("本批代码栅格上限为400万像元、20波段")
        for item in outputs:
            if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff.-]{1,100}",item["filename"]) or item["filename"] in {".",".."}:raise ValueError("代码输出必须是简单文件名")
        if len({x["filename"] for x in outputs})!=len(outputs):raise ValueError("重复输出文件名")
        text="\n".join([self.plan["request"],*self.plan["revisions"]])
        if not inputs and re.search(r"影像|这张|农田|水体|地表温度|地物",text):raise ValueError("没有本次输入，不能用代码生成假影像结果")
        if re.search(r"地表温度|测温|\bLST\b",text,re.I):
            from .data_adapter import describe
            roots=set(input_ids)
            for key in artifact_ids:roots.update(self.artifacts[key].get("input_ids",[]))
            descriptions=[describe(self.input(key)) for key in roots]
            known_temperature=any(self.artifacts[key].get("producer")=="retrieve_temperature" for key in artifact_ids)
            if not known_temperature and descriptions and all(item["metadata"].get("sensor")=="Sentinel-2" or (len(item["bands"])==3 and set(item["bands"])=={"RED","GREEN","BLUE"}) for item in descriptions):
                raise ValueError("所选输入没有热红外观测；Python代码不能从普通RGB或Sentinel-2光谱生成可靠地表温度，请补充测温数据")
        self.code_runs+=1;folder=self.folder();job=folder/"python_job"
        result=run(code,inputs,job,active=self.active)
        # stdout和stderr是诊断数据，不能视为已核实的遥感事实或下一步授权。
        diagnostic={k:v for k,v in result.items() if k!="output_dir"}
        if not result["ok"]:return diagnostic
        registered={};names={}
        for output in outputs:
            path=confined_path(Path(result["output_dir"]),output["filename"])
            if path.is_symlink() or not path.is_file() or path.stat().st_size>64*1024**2:raise ValueError("声明产物不存在、含链接或超过上限")
            suffix=path.suffix.lower();kind=output["kind"]
            allowed={"raster":{'.tif','.tiff'},"image":{'.png','.jpg','.jpeg'},"table":{'.csv'},"data":{'.json','.txt','.geojson'}}
            if suffix not in allowed[kind]:raise ValueError("产物类型与文件后缀不一致")
            classes=output.get("classes")
            if classes and (kind!="raster" or len(classes)>32 or any(not re.fullmatch(r"\d{1,3}",key) for key in classes)):raise ValueError("类别定义应为有限编号与中文名称")
            if kind=="raster":
                values,valid,profile,_=read_small(path)
                if classes and not np.isin(values[0][valid],[int(v) for v in classes]).all():raise ValueError("分类栅格存在未声明的类别")
                self.check_code_grid(path,inputs.values())
            elif kind=="image":
                from PIL import Image
                with Image.open(path) as image:image.verify()
            elif kind=="table":
                with path.open(encoding="utf-8-sig",newline="") as stream:
                    if not next(csv.reader(stream),[]):raise ValueError("统计表为空")
            key=self.register(path,kind,classes=classes,role=output["role"],label=output["label"])
            if kind=="table":
                with path.open(encoding="utf-8-sig",newline="") as stream:
                    reader=csv.DictReader(stream)
                    for number,row in enumerate(reader):
                        if number>=8:
                            self.facts.append("页面仅展示前8条记录，其余结果请查看完整统计表。")
                            break
                        def display(value):
                            try:
                                number=float(value)
                                if np.isfinite(number):return format(number,'.6g')
                            except (ValueError,TypeError):pass
                            return str(value)[:80]
                        self.facts.append(output["label"]+"："+'；'.join(str(k)[:40]+'='+display(v) for k,v in list(row.items())[:8]))
            names[output["filename"]]=key;registered[key]=self.artifacts[key]
        for output in outputs:
            if output.get("source_raster"):
                source=names.get(output["source_raster"],output["source_raster"])
                self.artifact(source,True)
                if output["kind"]=="table":
                    self.artifacts[names[output["filename"]]]["measurement"]={"kind":"area","raster_id":source,"class_codes":output.get("class_codes",[]),"unit":output.get("unit","")}
                else:self.artifacts[names[output["filename"]]]["source_raster_id"]=source
        self.facts.append(f"已生成{len(registered)}个本次结果文件，数值记录可在文件中查看。")
        return {**diagnostic,"artifacts":registered,"note":"代码输出仍须validate_artifacts和逐项需求核验；stdout不是经过核验的分析结论。"}

    def check_code_grid(self,path,sources):
        with rasterio.open(path) as dst:
            for source in sources:
                if Path(source).suffix.lower() not in {'.tif','.tiff'}:continue
                with rasterio.open(source) as src:
                    if dst.crs!=src.crs:continue
                    a,b,c,d,e,f=dst.transform[:6];aa,bb,cc,dd,ee,ff=src.transform[:6]
                    if not np.allclose([a,b,d,e],[aa,bb,dd,ee],rtol=0,atol=1e-9):continue
                    x,y=(~src.transform)*(c,f)
                    if abs(x-round(x))<1e-6 and abs(y-round(y))<1e-6 and x>=-1e-6 and y>=-1e-6 and x+dst.width<=src.width+1e-6 and y+dst.height<=src.height+1e-6:return
        raise ValueError("代码栅格没有继承输入网格或其整数像元子窗口；需要空间转换时先使用align_raster")

    def tool_validate_artifacts(self):
        from .result_audit import audit_execution_results
        self.verify_remote_responses()
        for key,item in self.artifacts.items():
            path,_=self.artifact(key)
            if item["kind"]=="raster":read_small(path)
            elif item["kind"]=="image":
                from PIL import Image
                with Image.open(path) as image:image.verify()
        files=[path.relative_to(self.root/'results').as_posix() for path,_ in [self.artifact(key) for key in self.artifacts]]
        audit=audit_execution_results(self.root/'results',files,self.plan['request'],task_subdir=self.output.name)
        if audit['errors']:raise ValueError("产物核验失败："+'；'.join(audit['errors']))
        self.validated=self.fingerprint()
        result={"ok":True,"artifact_count":len(self.artifacts),"warnings":audit['warnings'],"semantic_accuracy":"没有读取答案，识别精度未确认"}
        self.acceptance=check_requirements(self.contract,self.artifacts,self.output,inputs={key:self.input(key) for key in self.plan["inputs"]})
        result["requirements"]=self.acceptance
        return result

    def tool_finish_task(self,status,artifact_ids,summary,unfulfilled):
        from .result_delivery import has_unsupported_accuracy_claim
        if has_unsupported_accuracy_claim(summary):raise ValueError("不能声称没有独立依据的识别准确率")
        for key in artifact_ids:self.artifact(key)
        if status=="blocked":
            if not unfulfilled:raise ValueError("必须说明缺少的条件")
        else:
            if self.validated!=self.fingerprint():raise ValueError("必须先核验当前实际产物")
            delivered={key:self.artifacts[key] for key in artifact_ids}
            acceptance=check_requirements(self.contract,delivered,self.output,evidence_artifacts=self.artifacts,inputs={key:self.input(key) for key in self.plan["inputs"]})
            missing=[item["description"] for item in acceptance["items"] if not item["met"]]
            if status=="completed" and (missing or unfulfilled):raise ValueError("尚未满足用户要求："+"；".join(missing+unfulfilled))
            if status=="partial" and not unfulfilled:raise ValueError("部分完成必须列明未完成项")
            if status=="partial":unfulfilled=list(dict.fromkeys([*missing,*unfulfilled]))
            if not artifact_ids:raise ValueError("数据处理必须引用实际交付产物，纯解释可直接回复")
            self.acceptance=acceptance
        return {"ok":True,"_finish":True,"status":status,"artifact_ids":artifact_ids,"summary":summary,"unfulfilled":unfulfilled,
                "facts":list(dict.fromkeys(self.facts)),"acceptance":self.acceptance}
