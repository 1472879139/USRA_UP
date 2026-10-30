"""生成交接包的合成演示材料；只在打包/测试端需要GIS依赖。"""
import argparse
import json
import os
from pathlib import Path
import sys


def configure_gis():
    prefix = Path(sys.prefix)
    share = prefix / ("Library/share" if os.name == "nt" else "share")
    if (share / "proj/proj.db").is_file():
        os.environ.setdefault("PROJ_DATA", str(share / "proj"))
        os.environ.setdefault("PROJ_LIB", str(share / "proj"))
    if (share / "gdal").is_dir():
        os.environ.setdefault("GDAL_DATA", str(share / "gdal"))


def prepare(destination):
    configure_gis()
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import transform_geom
    from .contract import BANDS, canonical
    from .inference import write_products
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    y, x = np.indices((32, 32))
    raw = np.stack([(1400 + i * 30 + x * 15 + y * 4).astype("uint16") for i in range(12)])
    profile = {"driver": "GTiff", "width": 32, "height": 32, "count": 12, "dtype": "uint16",
               "crs": rasterio.CRS.from_epsg(32632), "transform": from_origin(500000, 1000000, 10, 10)}
    with rasterio.open(root / "input.tif", "w", **profile) as dst:
        dst.write(raw)
        for i, band in enumerate(BANDS, 1):
            dst.set_band_description(i, band)
    metadata = {"sensor": "Sentinel-2", "product_level": "L2A", "encoding": "dn_10000", "bands": BANDS}
    (root / "metadata.json").write_text(canonical(metadata), encoding="utf-8")
    valid = np.ones((32, 32), dtype=bool)
    index = {}
    for name, capability in [("da", "da-v2-field-delineation"), ("terramind", "terramind-small-landcover")]:
        output = root / name
        prediction = np.zeros((32, 32), dtype="uint8")
        prediction[5:24, 7:27] = 1 if name == "da" else 4
        if name == "terramind":
            prediction[prediction == 0] = 9
        boundaries = None
        if name == "da":
            points = [profile["transform"] * point for point in [(7,5),(27,5),(27,24),(7,24),(7,5)]]
            boundaries = {"type": "FeatureCollection", "features": [{"type": "Feature",
                "properties": {"field_id": 1, "vector_area_m2": 38000, "quality_status": "synthetic_demo"},
                "geometry": transform_geom(profile["crs"], "EPSG:4326", {"type": "Polygon", "coordinates": [points]})}]}
        index[name] = write_products(output, prediction, raw, valid, profile, capability,
                                     {"synthetic_demo": True, "accuracy_evaluated": False}, boundaries, mock=True)
    (root / "index.json").write_text(canonical(index), encoding="utf-8")
    (root / "README.txt").write_text("所有文件均为合成演示，不是卫星观测或模型预测，不得用于精度评价。\n", encoding="utf-8")
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    prepare(parser.parse_args().destination)
