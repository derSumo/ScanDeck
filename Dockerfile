FROM python:3.12-slim

ARG APP_VERSION=1.0.0

LABEL org.opencontainers.image.title="ScanDeck" \
      org.opencontainers.image.description="Weboberflaeche fuer eSCL-Netzwerkscanner mit Paperless-ngx-Upload und Home-Assistant-Schnittstelle." \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/derSumo/ScanDeck" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    SCAN_OUTPUT_DIR=/scans

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION app.py ./
COPY templates ./templates
COPY static ./static

RUN useradd --create-home --uid 10001 scanner \
    && mkdir -p /data /scans \
    && chown -R scanner:scanner /app /data /scans

USER scanner
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "0", "--bind", "0.0.0.0:8080", "app:app"]
