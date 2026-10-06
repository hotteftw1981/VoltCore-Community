FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data WEB_PORT=8000 OCPP_PORT=9000 HOME=/tmp TMPDIR=/tmp XDG_CACHE_HOME=/tmp/.cache
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY app ./app
RUN mkdir -p /data
EXPOSE 8000 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
CMD ["python", "-m", "app.main"]
