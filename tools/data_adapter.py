"""按可核对元数据适配模型输入；不修改原图，不猜传感器，不缩小整幅图。"""
import json
from pathlib import Path
import re

import numpy as np
import rasterio
from rasterio.windows import Window

from .artifact_paths import confined_path,file_hash


def band_name(value):
    return re.sub(r"^B0+", "B", str(value or "").upper().strip())


def metadata(path):
    path=Path(path);sidecar=path.parent/"metadata.json"
    result={}
    if sidecar.is_file():
        if sidecar.is_symlink() or sidecar.stat().st_size>262144:raise ValueError("来源元数据不是合适的普通小文件")
        result=json.loads(sidecar.read_text(encoding="utf-8-sig"))
        if not isinstance(result,dict):raise ValueError("来源元数据应为JSON对象")
    with rasterio.open(path) as src:
        tags=src.tags()
        for key in ("sensor","product_level","encoding"):
            tagged=tags.get(key) or tags.get(key.upper())
            if tagged:
                if key in result and result[key]!=tagged:raise ValueError("栅格标签与来源元数据矛盾："+key)
                result[key]=tagged
    return result


def describe(path):
    """返回数据事实和未知项，缩略统计不作为精确面积。"""
    path=Path(path);meta=metadata(path)
    with rasterio.open(path) as src:
        labels=list(src.descriptions)
        if not all(labels) and len(meta.get("bands",[]))==src.count:labels=meta["bands"]
        sample=src.read(indexes=list(range(1,min(src.count,32)+1)),out_shape=(min(src.count,32),min(src.height,128),min(src.width,128)),masked=True)
        valid=(~np.ma.getmaskarray(sample)).all(axis=0)&np.isfinite(sample.data).all(axis=0)
        pixel=abs(src.transform.a*src.transform.e-src.transform.b*src.transform.d)
        metric=bool(src.crs and src.crs.is_projected and src.crs.linear_units in ("metre","meter") and pixel>0)
        return {"bands":[band_name(x) for x in labels],"shape":[src.height,src.width],"crs":str(src.crs) if src.crs else None,
                "dtype":list(src.dtypes),"scales":list(src.scales),"offsets":list(src.offsets),"nodata":src.nodata,
                "resolution":list(src.res),"pixel_area_m2":pixel if metric else None,"metadata":meta,
                "sample":{"shape":list(valid.shape),"valid_fraction":float(valid.mean()),"exact_coverage":False},
                "unknown":[key for key in ("sensor","product_level","encoding") if not meta.get(key)],
                "notes":["来源声明可核对但不等于已经验证真实性；文件名不提供传感器证据。",
                         "面积需要真实米制投影；缺失坐标不自动编造。"]}


def normalized(path,method):
    from . import terramind_protocol as tm,prithvi_protocol as pr
    protocol=tm if method=="terramind" else pr if method=="prithvi" else None
    if protocol is None:raise ValueError("未知的模型输入契约")
    meta=metadata(path);level="L2A" if method=="terramind" else "L1C"
    if meta.get("sensor")!="Sentinel-2" or meta.get("product_level")!=level:
        raise ValueError("需要明确的Sentinel-2 "+level+"来源，不能根据文件名或波段数猜测")
    with rasterio.open(path) as src:
        if src.count>20 or src.height*src.width>4_000_000:raise ValueError("本批适配上限为400万像元、20波段")
        if src.crs is None:raise ValueError("缺少空间参考，不能猜测投影")
        names=[band_name(v) for v in src.descriptions]
        declared=[band_name(v) for v in meta.get("bands",[])]
        if not all(names) and len(declared)==src.count:names=declared
        elif declared and names!=declared:raise ValueError("波段描述与来源声明的顺序矛盾")
        if any(names.count(v)!=1 for v in protocol.BAND_NAMES):raise ValueError("缺少唯一明确的模型所需波段")
        indexes=[names.index(v)+1 for v in protocol.BAND_NAMES]
        value=src.read(indexes,masked=True).astype("float32")
        valid=(~np.ma.getmaskarray(value)).all(axis=0)&np.isfinite(value.data).all(axis=0)
        if not valid.any():raise ValueError("输入没有有效像元")
        scales=np.asarray([src.scales[i-1] for i in indexes],dtype="float32")[:,None,None]
        offsets=np.asarray([src.offsets[i-1] for i in indexes],dtype="float32")[:,None,None]
        encoding=meta.get("encoding")
        if encoding=="DN/10000" and np.all(scales==1) and np.all(offsets==0):raw=value.filled(0)
        elif encoding in ("reflectance","reflectance_0_1","0-1反射率") or (encoding=="DN/10000" and np.allclose(scales,.0001) and np.all(offsets==0)):
            raw=(value.filled(0)*scales+offsets)*10000
        else:raise ValueError("无法可靠确定反射率缩放，不重复缩放或猜测编码")
        if not np.isfinite(raw[:,valid]).all() or (raw[:,valid]<0).any():raise ValueError("有效范围中存在模型不接受的负数或非有限DN")
        if encoding=="DN/10000" and float(raw[:,valid].max())<=2:raise ValueError("0～1数值与原始DN声明矛盾")
        profile=src.profile.copy()
    return raw,valid,profile,{"sensor":"Sentinel-2","product_level":level,"encoding":"DN/10000","bands":protocol.BAND_NAMES}


def anchors(length,size,overlap):
    if length<=size:return [0]
    count=int(np.ceil((length-size)/(size-overlap)))+1
    return [int(round(value)) for value in np.linspace(0,length-size,count)]


def prepare_tiles(source,folder,method):
    """重排/缩放并按原分辨率分块；边界填NoData，保留原网格。"""
    source=Path(source);folder=Path(folder)
    raw,valid,profile,meta=normalized(source,method)
    size,overlap=(224,32) if method=="terramind" else (512,64)
    ys,xs=anchors(profile["height"],size,overlap),anchors(profile["width"],size,overlap)
    if len(ys)*len(xs)>64:raise ValueError("本次分块超过64块的资源上限，请先缩小分析范围")
    folder.mkdir(parents=True,exist_ok=True);tiles=[]
    for y in ys:
        for x in xs:
            h,w=min(size,profile["height"]-y),min(size,profile["width"]-x)
            if not valid[y:y+h,x:x+w].any():continue
            data=np.full((len(meta["bands"]),size,size),-9999,dtype="float32")
            data[:,:h,:w]=np.where(valid[y:y+h,x:x+w],raw[:,y:y+h,x:x+w],-9999)
            target=folder/f"tile_{len(tiles):03}"/"image.tif";target.parent.mkdir()
            transform=rasterio.windows.transform(Window(x,y,w,h),profile["transform"])
            with rasterio.open(target,"w",driver="GTiff",width=size,height=size,count=data.shape[0],dtype="float32",crs=profile["crs"],transform=transform,nodata=-9999,compress="deflate") as dst:
                dst.write(data);dst.descriptions=tuple(meta["bands"])
            (target.parent/"metadata.json").write_text(json.dumps(meta,ensure_ascii=False),encoding="utf-8")
            tiles.append({"path":target.relative_to(folder).as_posix(),"window":[y,x,h,w],"sha256":file_hash(target),"metadata_sha256":file_hash(target.parent/"metadata.json")})
    result={"version":1,"method":method,"source_sha256":file_hash(source),"metadata_sha256":file_hash(source.parent/"metadata.json") if (source.parent/"metadata.json").exists() else None,
            "source_shape":[profile["height"],profile["width"]],"source_valid_pixels":int(valid.sum()),"tile_size":size,"overlap":overlap,
            "resampling":"none","padding":"NoData","tiles":tiles}
    (folder/"adaptation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result


def verify_prepared(source,folder,manifest):
    """从原始输入重算每块，不能仅信任适配器自己写下的哈希。"""
    source=Path(source);folder=Path(folder)
    raw,valid,profile,meta=normalized(source,manifest["method"])
    if manifest["source_sha256"]!=file_hash(source):raise ValueError("适配来源影像已变化")
    expected_meta=file_hash(source.parent/"metadata.json") if (source.parent/"metadata.json").exists() else None
    if manifest["metadata_sha256"]!=expected_meta:raise ValueError("适配来源元数据已变化")
    coverage=np.zeros(valid.shape,dtype=bool)
    for tile in manifest["tiles"]:
        path=confined_path(folder,tile["path"])
        if file_hash(path)!=tile["sha256"] or file_hash(path.parent/"metadata.json")!=tile["metadata_sha256"]:raise ValueError("适配后的文件被改变")
        y,x,h,w=tile["window"];size=manifest["tile_size"]
        if not (0<=y<valid.shape[0] and 0<=x<valid.shape[1] and 0<h<=size and 0<w<=size and y+h<=valid.shape[0] and x+w<=valid.shape[1]):raise ValueError("分块窗口越界")
        with rasterio.open(path) as src:
            expect=np.full((len(meta["bands"]),size,size),-9999,dtype="float32")
            expect[:,:h,:w]=np.where(valid[y:y+h,x:x+w],raw[:,y:y+h,x:x+w],-9999)
            if src.crs!=profile["crs"] or src.transform!=rasterio.windows.transform(Window(x,y,w,h),profile["transform"]) or not np.array_equal(src.read(),expect):raise ValueError("分块数值或空间网格与原图不一致")
        coverage[y:y+h,x:x+w]|=valid[y:y+h,x:x+w]
    if not np.array_equal(coverage,valid):raise ValueError("分块没有覆盖全部原图有效区域")
    return True


def align_raster(source,target,*,reference=None,metric=False,categorical=False):
    """显式对齐参考网格，或将小范围地理坐标转为当地UTM以计算面积。"""
    from rasterio.warp import calculate_default_transform,reproject,transform_bounds
    from rasterio.enums import Resampling
    with rasterio.open(source) as src:
        if not src.crs:raise ValueError("缺少来源坐标，不能进行空间转换")
        if reference:
            with rasterio.open(reference) as ref:
                if not ref.crs:raise ValueError("参考影像缺少空间参考")
                crs,transform,width,height=ref.crs,ref.transform,ref.width,ref.height
        elif metric:
            bounds=transform_bounds(src.crs,"EPSG:4326",*src.bounds)
            west,south,east,north=bounds
            if not (-180<=west<east<=180 and -80<=south<north<=84 and east-west<=6):raise ValueError("范围过大或超出UTM常规范围，需要明确面积投影方案")
            lon,lat=(west+east)/2,(south+north)/2
            crs=rasterio.crs.CRS.from_epsg((32600 if lat>=0 else 32700)+min(60,int((lon+180)//6)+1))
            transform,width,height=calculate_default_transform(src.crs,crs,src.width,src.height,*src.bounds)
        else:raise ValueError("需要参考网格或明确的米制投影转换")
        if width*height>4_000_000:raise ValueError("空间转换结果超过400万像元")
        value=src.read(masked=True).astype("float32");valid=(~np.ma.getmaskarray(value)).all(axis=0)&np.isfinite(value.data).all(axis=0)
        destination=np.full((src.count,height,width),-9999,dtype="float32")
        for index in range(src.count):
            reproject(np.where(valid,value.data[index],-9999),destination[index],src_transform=src.transform,src_crs=src.crs,src_nodata=-9999,
                      dst_transform=transform,dst_crs=crs,dst_nodata=-9999,resampling=Resampling.nearest if categorical else Resampling.bilinear)
        good=(destination!=-9999).all(axis=0)&np.isfinite(destination).all(axis=0)
        if not good.any():raise ValueError("参考网格与来源没有可用重叠")
        with rasterio.open(target,"w",driver="GTiff",height=height,width=width,count=src.count,dtype="float32",crs=crs,transform=transform,nodata=-9999,compress="deflate") as dst:
            dst.write(np.where(good,destination,-9999));dst.descriptions=src.descriptions
    return {"source_crs":str(src.crs),"target_crs":str(crs),"resampling":"nearest" if categorical else "bilinear","output_shape":[height,width],
            "note":"网格对齐和投影转换不是多时相影像的精细配准；面积为平面近似，语义仍待核实。"}
