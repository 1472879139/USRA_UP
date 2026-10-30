FROM python:3.10-slim-bookworm
LABEL org.ursa.runtime="python-v1"
# 容器内独立GIS环境；不会覆盖宿主Conda环境的GDAL/Rasterio。
RUN python -m pip install --no-cache-dir numpy==1.26.4 rasterio==1.3.11 matplotlib==3.8.4 Pillow==10.4.0
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk libexpat1 && rm -rf /var/lib/apt/lists/*
RUN MPLCONFIGDIR=/tmp/mpl python -c "import numpy, rasterio, matplotlib, PIL; print(rasterio.__gdal_version__)"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
