FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install -r requirements.txt

FROM python:3.13-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUNICORN_BIND=0.0.0.0:8000 \
    MEDIA_ROOT=/data/media \
    ATTACHMENTS_ROOT=/data/attachments \
    IMPORTS_ROOT=/data/imports \
    LOG_DIR=/data/logs

RUN useradd --system --uid 10001 --home-dir /app ping \
    && mkdir -p /data/media /data/attachments /data/imports /data/logs \
    && chown -R ping /data

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY pyproject.toml ./
COPY migrations ./migrations
COPY deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py
COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY app ./app

USER ping
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=3)"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "app.app:app"]
