# LDTF in a container: the web interface on port 8765, the archives in the volume /data.
# Build: docker build -t ldtf .   Run: see compose.yaml (or docs/GUIDE.md, "Docker").
FROM python:3.14-slim

LABEL org.opencontainers.image.title="LDTF" \
      org.opencontainers.image.description="Локальный архив профилей DTF: посты, комментарии с контекстом, медиа, поиск" \
      org.opencontainers.image.source="https://github.com/Gwynerva/ldtf" \
      org.opencontainers.image.licenses="MIT"

# LDTF_DOCKER: no tray, autostart or self-update (the image is updated instead); TZ: the daily sync time is local time
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    LDTF_DOCKER=1 LDTF_ROOT=/data LDTF_HOST=0.0.0.0 LDTF_PORT=8765 TZ=Europe/Moscow

WORKDIR /app
COPY LICENSE ./
COPY dtf_backup ./dtf_backup
RUN python -m compileall -q dtf_backup \
    && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin ldtf \
    && mkdir /data && chown ldtf:ldtf /data

USER ldtf
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('LDTF_PORT', '8765'), timeout=4)"]
# SIGTERM (docker stop) lets running syncs save their progress before the container goes
CMD ["python", "-m", "dtf_backup", "serve"]
