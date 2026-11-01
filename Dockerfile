FROM python:3.12-slim-bookworm AS controller
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY luminext ./luminext
COPY web ./web
RUN useradd --uid 10001 --create-home luminext && mkdir /data && chown luminext:luminext /data
USER luminext
ENV LUMINEXT_DATA=/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "luminext", "--host", "0.0.0.0"]

FROM controller AS detection
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg intel-opencl-icd intel-media-va-driver vainfo libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements-detection.txt .
RUN pip install --no-cache-dir -r requirements-detection.txt
USER luminext
HEALTHCHECK NONE
CMD ["python", "-m", "luminext.worker", "--api", "http://controller:8000", "--data", "/data", "--model", "/models/person-detection-retail-0013.xml"]
