# Gunicorn configuration for ping-server (production).
# Run with: gunicorn -c deploy/gunicorn.conf.py app.app:app
#
# Uses Uvicorn workers so the ASGI app (and its WebSocket /ws endpoint) works.
import multiprocessing
import os

# Bind to localhost only; Nginx sits in front and proxies to this.
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")

# ASGI worker class. Required for FastAPI + WebSockets.
worker_class = "uvicorn.workers.UvicornWorker"

# WebSocket connections are long-lived, so keep worker count modest and rely on
# async concurrency within each worker rather than many processes.
workers = int(os.getenv("GUNICORN_WORKERS", (multiprocessing.cpu_count() * 2) + 1))

# Restart workers periodically to bound memory growth.
max_requests = 1000
max_requests_jitter = 100

# WebSockets are long-lived; don't let gunicorn time out active workers.
# Uvicorn manages the connections; this is the worker boot/health timeout.
timeout = 120
graceful_timeout = 30
keepalive = 5

# Log to stdout/stderr so journald captures everything.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")

proc_name = "ping-server"
