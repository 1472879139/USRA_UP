"""
io_kit — Raster I/O and metadata utilities

Provides standardized read/write access to satellite raster data.
All functions return structured dicts with 'success', 'message', and 'data' fields.
"""

import os
import json
import numpy as np

try:
    import rasterio
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_project_root() -> str:
    """Return the ExpertsRS project root (parent of tools/)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ensure_results_dir(subfolder: str = "") -> str:
    """Create results/ subfolder and return its absolute path."""
    root = _get_project_root()
    results_root = os.path.realpath(os.path.join(root, "results"))
    results = os.path.realpath(os.environ.get("URSA_RESULTS_DIR") or results_root)
    if os.path.commonpath([results_root, results]) != results_root:
        raise ValueError("URSA_RESULTS_DIR 必须位于项目 results/ 下")
    base = results
    if subfolder:
        results = os.path.realpath(os.path.join(results, subfolder))
        if os.path.commonpath([base, results]) != base:
            raise ValueError("结果子目录不能越出本次任务目录")
    os.makedirs(results, exist_ok=True)
    return results


def _resolve_data_path(file_path: str = None) -> str:
    """Resolve a data file path to absolute, checking both relative and absolute."""
    root = _get_project_root()
    if file_path:
        if os.path.isabs(file_path) and os.path.exists(file_path):
            return file_path
        rel = os.path.join(root, file_path)
        if os.path.exists(rel):
            return rel
        # Try as-is
        if os.path.exists(file_path):
            return os.path.abspath(file_path)
        raise FileNotFoundError(f"Data file not found: {file_path}")
    # Auto-discover: recursively look for .tif files in data/
    data_dir = os.path.join(root, "data")
    if not os.path.exists(data_dir):
        raise FileNotFoundError("No 'data/' directory found in project root.")
    tif_files = []
    for dirpath, _, filenames in os.walk(data_dir):
        for fn in filenames:
            if fn.lower().endswith((".tif", ".tiff")):
                tif_files.append(os.path.join(dirpath, fn))
    if not tif_files:
        raise FileNotFoundError("No .tif files found in data/ directory (including subdirectories).")
    if len(tif_files) == 1:
        return tif_files[0]
    return tif_files[0]


# ──────────────────────────────────────────────────────────────────────────────
# Startup scanner
# ──────────────────────────────────────────────────────────────────────────────

def _read_tiff_basic(filepath: str) -> dict:
    """
    纯 Python 读取 TIFF 基本头信息（不依赖 rasterio）。
    作为 rasterio 不可用时的 fallback。
    """
    import struct
    try:
        with open(filepath, 'rb') as f:
            header = f.read(8)
            byte_order = '<' if header[:2] == b'II' else '>'
            magic = struct.unpack(byte_order + 'H', header[2:4])[0]
            if magic not in (42, 43):
                return {"success": False, "message": f"不是有效的 TIFF/GeoTIFF 文件 (magic={magic})"}

            bigtiff = (magic == 43)
            if bigtiff:
                offset_size = struct.unpack(byte_order + 'H', header[4:6])[0]
                _ = struct.unpack(byte_order + 'H', header[6:8])[0]
                ifd_offset = struct.unpack(byte_order + 'Q', f.read(8))[0]
            else:
                ifd_offset = struct.unpack(byte_order + 'I', header[4:8])[0]

            tags = {}
            f.seek(ifd_offset)
            if bigtiff:
                num_entries = struct.unpack(byte_order + 'Q', f.read(8))[0]
            else:
                num_entries = struct.unpack(byte_order + 'H', f.read(2))[0]

            for _ in range(num_entries):
                entry = f.read(12) if not bigtiff else f.read(20)
                tag = struct.unpack(byte_order + 'H', entry[:2])[0]
                dtype = struct.unpack(byte_order + 'H', entry[2:4])[0]
                count = struct.unpack(byte_order + 'I', entry[4:8])[0]
                type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 11: 4, 12: 8, 16: 8}
                size = type_sizes.get(dtype, 1)
                total = count * size
                offset_pos = 8 if not bigtiff else 12

                if total <= (4 if not bigtiff else 8):
                    value = entry[offset_pos:offset_pos + total]
                else:
                    cur = f.tell()
                    if bigtiff:
                        data_offset = struct.unpack(byte_order + 'Q', entry[offset_pos:offset_pos + 8])[0]
                    else:
                        data_offset = struct.unpack(byte_order + 'I', entry[offset_pos:offset_pos + 4])[0]
                    f.seek(data_offset)
                    value = f.read(total)
                    f.seek(cur)

                if dtype == 3:
                    vals = struct.unpack(byte_order + str(count) + 'H', value[:count * 2])
                elif dtype == 4:
                    vals = struct.unpack(byte_order + str(count) + 'I', value[:count * 4])
                elif dtype == 5:
                    vals = []
                    for j in range(count):
                        num = struct.unpack(byte_order + 'I', value[j * 8:j * 8 + 4])[0]
                        den = struct.unpack(byte_order + 'I', value[j * 8 + 4:j * 8 + 8])[0]
                        vals.append(num / den if den != 0 else 0)
                elif dtype == 16 and bigtiff:
                    vals = struct.unpack(byte_order + str(count) + 'Q', value[:count * 8])
                else:
                    vals = value
                tags[tag] = vals

            width = tags.get(256, [0])[0]
            height = tags.get(257, [0])[0]
            samples = tags.get(277, [1])[0]
            bits = tags.get(258, [8])
            if isinstance(bits, (list, tuple)):
                bits_str = str(bits[0]) if bits else "?"
            else:
                bits_str = str(bits)

            # 解析坐标系统
            crs_str = ""
            if 34735 in tags:
                gk = tags[34735]
                proj_map = {32632: "WGS 84 / UTM zone 32N", 32650: "WGS 84 / UTM zone 50N",
                            32651: "WGS 84 / UTM zone 51N"}
                for i in range(0, len(gk) - 3, 4):
                    if gk[i] == 3072 and i + 3 < len(gk):
                        crs_str = proj_map.get(gk[i + 3], f"EPSG:{gk[i + 3]}")
                        break

            # 解析分辨率
            res_str = ""
            if 33550 in tags:  # ModelPixelScaleTag
                res_str = f"{tags[33550][0]} x {tags[33550][1]}"

            # 尝试读取 GDALMetadata 中的波段名称
            band_names = []
            if 42112 in tags:
                gdal_meta = tags[42112]
                if isinstance(gdal_meta, bytes):
                    try:
                        gdal_meta = gdal_meta.decode('utf-8', errors='replace')
                    except Exception:
                        pass
                    import re
                    desc_matches = re.findall(r'DESCRIPTION.*?>(B\w+)<', str(gdal_meta))
                    if desc_matches:
                        band_names = desc_matches

            return {
                "success": True,
                "message": f"TIFF basic read: {width}x{height}, {samples} bands",
                "data": {
                    "width": int(width), "height": int(height),
                    "count": int(samples),
                    "dtype": f"uint{bits_str}" if bits_str.isdigit() else str(bits_str),
                    "crs": crs_str,
                    "bounds": None,
                    "res": res_str if res_str else None,
                    "nodata": None,
                    "band_descriptions": band_names if band_names else [f"Band {i+1}" for i in range(int(samples))],
                    "file_path": filepath,
                    "file_name": os.path.basename(filepath),
                }
            }
    except Exception as e:
        return {"success": False, "message": f"TIFF basic read failed: {e}"}


def scan_data_directory() -> dict:
    """
    扫描 data/ 目录，返回所有栅格文件的元数据摘要。
    在系统启动时调用，让用户了解可用数据。

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {
                "file_count": int,
                "files": [{
                    "file_name": str,
                    "file_path": str,
                    "width": int, "height": int,
                    "bands": int,
                    "band_names": list[str],
                    "dtype": str,
                    "crs": str,
                    "resolution": str,
                    "extent": str,
                }]
            }
        }
    """
    result = {"success": False, "message": "", "data": {"file_count": 0, "files": []}}
    try:
        file_list = list_available_data_files()
        if not file_list["success"]:
            result["message"] = file_list["message"]
            return result

        files = file_list["data"]["files"]
        result["data"]["file_count"] = len(files)

        for f in files:
            abs_path = file_list["data"]["abs_paths"][file_list["data"]["files"].index(f)]
            # 尝试 rasterio，失败则用纯 Python fallback
            meta = read_raster_metadata(abs_path)
            if not meta["success"]:
                meta = _read_tiff_basic(abs_path)
            file_info = {
                "file_name": f,
                "file_path": abs_path,
                "width": 0, "height": 0, "bands": 0,
                "band_names": [], "dtype": "", "crs": "", "resolution": "", "extent": "",
            }
            if meta["success"]:
                d = meta["data"]
                file_info.update({
                    "width": d["width"], "height": d["height"],
                    "bands": d["count"],
                    "band_names": d.get("band_descriptions", []),
                    "dtype": d["dtype"],
                    "crs": d.get("crs", ""),
                    "resolution": str(d.get("res", "")),
                    "extent": str(d["bounds"]) if d.get("bounds") else "",
                })
            else:
                file_info["band_names"] = [f"读取失败: {meta['message']}"]
            result["data"]["files"].append(file_info)

        result["success"] = True
        result["message"] = f"扫描完成，共 {len(files)} 个栅格文件"
        return result
    except Exception as e:
        result["message"] = str(e)
        return result


def print_data_summary() -> str:
    """
    打印 data/ 目录的数据摘要（人类可读格式）。
    返回格式化的摘要字符串，同时打印到控制台。
    """
    scan = scan_data_directory()
    lines = []
    lines.append("=" * 60)
    lines.append("  DATA 目录文件扫描")
    lines.append("=" * 60)

    if not scan["success"]:
        lines.append(f"  扫描失败: {scan['message']}")
        text = "\n".join(lines)
        print(text)
        return text

    data = scan["data"]
    lines.append(f"  发现 {data['file_count']} 个栅格文件\n")

    for i, f in enumerate(data["files"], 1):
        lines.append(f"  [{i}] {f['file_name']}")
        lines.append(f"      尺寸: {f['width']} × {f['height']} 像素")
        lines.append(f"      波段数: {f['bands']}  ({f['dtype']})")
        if f["crs"]:
            lines.append(f"      坐标系: {f['crs']}")
        if f["resolution"]:
            lines.append(f"      分辨率: {f['resolution']}")
        # 波段名称（跳过全是 "Band N" 格式的无意义名称，波段数≤4时标题重复率高）
        if f["band_names"]:
            names = f["band_names"]
            # 过滤掉值为 "None" 的字符串
            names = [n for n in names if n and n != "None"]
            if names and not all(n.startswith("Band ") for n in names):
                if len(names) <= 12:
                    lines.append(f"      波段: {', '.join(names)}")
                else:
                    lines.append(f"      波段(共{len(names)}): {', '.join(names[:6])} ... {', '.join(names[-3:])}")
            elif f["bands"] <= 4:
                # 少量波段且都是通用名，直接显示数量即可
                pass
            else:
                lines.append(f"      波段: {', '.join(names)}")
        lines.append("")

    lines.append("=" * 60)
    text = "\n".join(lines)
    print(text)
    return text


# ──────────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────────

def list_available_data_files() -> dict:
    """
    List all available raster data files in the project's data/ directory.

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {
                "files": list[str],          # relative file paths
                "abs_paths": list[str],      # absolute file paths
                "count": int
            }
        }
    """
    try:
        root = _get_project_root()
        data_dir = os.path.join(root, "data")
        if not os.path.exists(data_dir):
            return {
                "success": False,
                "message": "No 'data/' directory found.",
                "data": {"files": [], "abs_paths": [], "count": 0}
            }
        # 递归扫描 data/ 目录（包括子文件夹）
        files_rel = []
        abs_paths = []
        for dirpath, _, filenames in os.walk(data_dir):
            for fn in filenames:
                if fn.lower().endswith((".tif", ".tiff")):
                    abs_path = os.path.join(dirpath, fn)
                    rel_path = os.path.relpath(abs_path, data_dir)
                    files_rel.append(rel_path)
                    abs_paths.append(abs_path)
        # 排序：顶层文件在前，子文件夹文件在后
        files_rel.sort(key=lambda x: (x.count(os.sep), x))
        abs_paths.sort(key=lambda x: (x.count(os.sep), x))
        return {
            "success": True,
            "message": f"Found {len(files_rel)} raster file(s).",
            "data": {"files": files_rel, "abs_paths": abs_paths, "count": len(files_rel)}
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def read_raster_metadata(file_path: str = None) -> dict:
    """
    Read metadata and basic properties of a raster file.

    Args:
        file_path: Path to raster file. If None, auto-discovers from data/.

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {
                "width", "height", "crs", "bounds",
                "nodata", "dtype", "count" (band count),
                "transform": dict representation,
                "band_descriptions": list[str],
                "file_path", "file_name"
            }
        }
    """
    if not HAS_RASTERIO:
        return {"success": False, "message": "rasterio 未安装，无法读取详细元数据", "data": None}
    try:
        abs_path = _resolve_data_path(file_path)
        with rasterio.open(abs_path) as src:
            meta = {
                "width": src.width,
                "height": src.height,
                "crs": str(src.crs) if src.crs else None,
                "bounds": src.bounds._asdict() if src.bounds else None,
                "nodata": src.nodata,
                "dtype": src.dtypes[0],
                "count": src.count,
                "transform": str(src.transform),
                "res": src.res,
                "band_descriptions": [
                    (str(d) if d else f"Band {i+1}")
                    for i, d in enumerate(src.descriptions or [])
                ] or [f"Band {i+1}" for i in range(src.count)],
                "file_path": abs_path,
                "file_name": os.path.basename(abs_path),
            }
        return {
            "success": True,
            "message": f"Metadata read successfully: {meta['file_name']} "
                       f"({meta['width']}x{meta['height']}, {meta['count']} bands, {meta['dtype']})",
            "data": meta
        }
    except FileNotFoundError as e:
        return {"success": False, "message": str(e), "data": None}
    except Exception as e:
        return {"success": False, "message": f"Failed to read raster: {e}", "data": None}


def read_raster_band(file_path: str = None, band_index: int = 1) -> dict:
    """
    Read a single band from a raster file as a numpy array.

    Args:
        file_path: Path to raster file. If None, auto-discovers.
        band_index: 1-based band index (1 = first band). Use 1-based indexing
                    to match physical band numbering that scientists expect.

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {
                "array_shape": tuple,
                "dtype": str,
                "min", "max", "mean" (numpy statistics),
                "nodata_value",
                "band_index_used": int,  # 1-based
                "file_path"
            }
        }
    """
    try:
        abs_path = _resolve_data_path(file_path)
        with rasterio.open(abs_path) as src:
            if band_index < 1 or band_index > src.count:
                return {
                    "success": False,
                    "message": f"Band index {band_index} out of range. "
                               f"File has {src.count} band(s). Use band_index 1 to {src.count}.",
                    "data": None
                }
            arr = src.read(band_index)
            stats = {
                "array_shape": arr.shape,
                "dtype": str(arr.dtype),
                "min": float(np.nanmin(arr)) if arr.size > 0 else None,
                "max": float(np.nanmax(arr)) if arr.size > 0 else None,
                "mean": float(np.nanmean(arr)) if arr.size > 0 else None,
                "nodata_value": src.nodata,
                "band_index_used": band_index,  # 1-based as used by caller
                "file_path": abs_path,
                "file_name": os.path.basename(abs_path),
            }
        return {
            "success": True,
            "message": (f"Band {band_index} read: shape={arr.shape}, "
                        f"dtype={arr.dtype}, range=[{stats['min']:.4f}, {stats['max']:.4f}]"),
            "data": stats
        }
    except FileNotFoundError as e:
        return {"success": False, "message": str(e), "data": None}
    except Exception as e:
        return {"success": False, "message": f"Failed to read band: {e}", "data": None}


def read_raster_bands(file_path: str = None, band_indices: list[int] = None) -> dict:
    """
    Read multiple bands from a raster file.

    Args:
        file_path: Path to raster file. If None, auto-discovers.
        band_indices: List of 1-based band indices. e.g. [4, 8] for Red + NIR (Sentinel-2).
                      If None, reads all bands.

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {
                "band_count": int,
                "shape": tuple,
                "dtype": str,
                "bands_read": list[int],  # 1-based indices
                "file_path"
            }
        }
    """
    try:
        abs_path = _resolve_data_path(file_path)
        with rasterio.open(abs_path) as src:
            if band_indices is None:
                bands = list(range(1, src.count + 1))
            else:
                for b in band_indices:
                    if b < 1 or b > src.count:
                        return {
                            "success": False,
                            "message": f"Band index {b} out of range (1-{src.count}).",
                            "data": None
                        }
                bands = band_indices
            arrays = [src.read(b) for b in bands]
            shape = arrays[0].shape
        return {
            "success": True,
            "message": f"Read {len(bands)} band(s): indices={bands}, shape={shape}",
            "data": {
                "band_count": len(bands),
                "shape": shape,
                "dtype": str(arrays[0].dtype),
                "bands_read": bands,  # 1-based
                "file_path": abs_path,
                "file_name": os.path.basename(abs_path),
            }
        }
    except FileNotFoundError as e:
        return {"success": False, "message": str(e), "data": None}
    except Exception as e:
        return {"success": False, "message": f"Failed to read bands: {e}", "data": None}


def save_raster(data, output_path: str, reference_file: str = None,
                 dtype: str = None, nodata: float = None) -> dict:
    """
    Save a numpy array as a GeoTIFF raster file.

    Args:
        data: 2D numpy array (for single-band) or 3D (H, W, C).
        output_path: Output file path (relative to results/ or absolute).
        reference_file: Path to reference raster for CRS/transform.
                         If None, uses first file in data/ directory.
        dtype: Data type string (e.g. 'float32', 'uint8'). If None, infers from data.
        nodata: No-data value. If None, uses reference file's nodata or -9999 for new files.

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "data": {"output_path": str, "shape": tuple, "dtype": str}
        }
    """
    try:
        import rasterio
        from rasterio.transform import from_bounds

        # Normalize output path
        if not os.path.isabs(output_path):
            output_path = os.path.join(_ensure_results_dir(), output_path)

        data = np.asarray(data)
        if data.ndim == 2:
            data = np.expand_dims(data, axis=0)
        count, height, width = data.shape

        # Get reference metadata
        ref_path = None
        if reference_file:
            try:
                ref_path = _resolve_data_path(reference_file)
            except FileNotFoundError:
                ref_path = None

        if ref_path is None:
            try:
                ref_path = _resolve_data_path(None)
            except FileNotFoundError:
                ref_path = None

        if ref_path and os.path.exists(ref_path):
            with rasterio.open(ref_path) as ref:
                profile = ref.profile.copy()
        else:
            # Fallback: create minimal profile
            profile = {
                "driver": "GTiff",
                "height": height,
                "width": width,
                "count": count,
                "dtype": dtype or str(data.dtype),
                "crs": None,
                "transform": None,
                "nodata": nodata if nodata is not None else -9999,
            }

        # Override with explicit args
        if dtype:
            data = data.astype(dtype)
            profile["dtype"] = dtype
        else:
            profile["dtype"] = str(data.dtype)

        profile["count"] = count
        profile["height"] = height
        profile["width"] = width
        if nodata is not None:
            profile["nodata"] = nodata
        elif "nodata" not in profile:
            profile["nodata"] = -9999

        with rasterio.open(output_path, "w", **profile) as dst:
            for i in range(count):
                dst.write(data[i], i + 1)

        return {
            "success": True,
            "message": f"Saved raster: {output_path} (shape={data.shape}, dtype={profile['dtype']})",
            "data": {
                "output_path": output_path,
                "shape": tuple(data.shape),
                "dtype": profile["dtype"]
            }
        }
    except Exception as e:
        return {"success": False, "message": f"Failed to save raster: {e}", "data": None}
