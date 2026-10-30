"""固定模型的原分辨率分块、重叠投票拼接与原始响应重核。"""
import json
from pathlib import Path
import uuid

import numpy as np
import rasterio

from .artifact_paths import confined_path,file_hash
from .data_adapter import prepare_tiles,verify_prepared,normalized
from .agent_tools import write_array


def protocols(method):
    from . import terramind_protocol,terramind_remote,prithvi_protocol,prithvi_remote
    return (terramind_protocol,terramind_remote) if method=="terramind" else (prithvi_protocol,prithvi_remote)


def merge(source,folder,manifest):
    """从每块原始GPU包恢复候选类别，重叠处多数投票，同票选较小编码。"""
    method=manifest["adaptation"]["method"];protocol,_=protocols(method)
    raw,valid,profile,_=normalized(source,method)
    votes=np.zeros((10 if method=="terramind" else 2,*valid.shape),dtype="uint8")
    covered=np.zeros(valid.shape,dtype=bool)
    for entry in manifest["responses"]:
        tile=entry["tile"];path=confined_path(folder,tile["path"])
        tile_raw,tile_valid,tile_profile=protocol.read_input(path)
        response_path=confined_path(folder,entry["response_path"])
        if file_hash(response_path)!=entry["response_sha256"]:raise ValueError("原始GPU返回包已改变")
        content=response_path.read_bytes()
        if method=="terramind":mask,reply=protocol.unpack_response(content,entry["request_id"],file_hash(path),tile_profile,tile_valid)
        else:
            masks,reply=protocol.unpack_response(content,entry["request_id"],file_hash(path),tile_raw,tile_valid,tile_profile)
            mask=masks["model_mask.tif"]
        if reply!=entry["response"]:raise ValueError("GPU响应身份被改变")
        y,x,h,w=tile["window"];good=tile_valid[:h,:w]
        for code in range(votes.shape[0]):votes[code,y:y+h,x:x+w]+=((mask[:h,:w]==code)&good).astype("uint8")
        covered[y:y+h,x:x+w]|=good
    if not np.array_equal(covered,valid):raise ValueError("模型结果没有覆盖原图的全部有效像元")
    mask=votes.argmax(axis=0).astype("uint8");mask[~valid]=255
    return mask,valid,profile


def run_tiled(source,folder,method,project_root,*,active=None,notify=None):
    source=Path(source);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    protocol,remote=protocols(method);config=remote.settings(project_root)
    if config is None:raise ValueError("该GPU模型尚未配置启用")
    adaptation=prepare_tiles(source,folder/"prepared",method)
    responses=[]
    for index,tile in enumerate(adaptation["tiles"]):
        if active and not active():raise ValueError("用户已终止，不再请求下一块GPU推理")
        path=confined_path(folder/"prepared",tile["path"])
        request_id=uuid.uuid4().hex
        if notify:notify({"event":"model_tile","index":index+1,"total":len(adaptation["tiles"]),"method":method})
        content=remote.post(config,path.read_bytes(),request_id,file_hash(path))
        raw,valid,profile=protocol.read_input(path)
        if method=="terramind":_,reply=protocol.unpack_response(content,request_id,file_hash(path),profile,valid)
        else:_,reply=protocol.unpack_response(content,request_id,file_hash(path),raw,valid,profile)
        target=path.parent/"response.zip";target.write_bytes(content)
        responses.append({"tile":{**tile,"path":"prepared/"+tile["path"]},"request_id":request_id,"response":reply,
                          "response_path":target.relative_to(folder).as_posix(),"response_sha256":file_hash(target)})
    manifest={"version":1,"adaptation":adaptation,"responses":responses,"merge":"majority_vote_tie_smallest_code","source_sha256":file_hash(source)}
    verify_prepared(source,folder/"prepared",adaptation)
    mask,valid,profile=merge(source,folder,manifest)
    target=folder/"classification.tif";write_array(target,mask,valid,profile,categorical=True)
    manifest["classification_sha256"]=file_hash(target)
    (folder/"tiled_model_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return target,manifest


def verify_tiled(source,folder):
    folder=Path(folder);manifest=json.loads((folder/"tiled_model_manifest.json").read_text(encoding="utf-8"))
    if manifest["source_sha256"]!=file_hash(source):raise ValueError("分块推理来源已改变")
    verify_prepared(source,folder/"prepared",manifest["adaptation"])
    expected,valid,profile=merge(source,folder,manifest)
    with rasterio.open(folder/"classification.tif") as src:
        if src.shape!=valid.shape or src.crs!=profile["crs"] or src.transform!=profile["transform"] or src.nodata!=255 or not np.array_equal(src.read(1),expected):raise ValueError("拼接结果与原图网格或原始GPU包不一致")
    if file_hash(folder/"classification.tif")!=manifest["classification_sha256"]:raise ValueError("拼接产物哈希不一致")
    return True
