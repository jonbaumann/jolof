FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user. The deploy script's RUN_AS (default 1000:1000)
# matches this.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin jolof
USER jolof

EXPOSE 8000

# One worker, several threads: background jobs (URL import, CSV import, the
# duplicate finder) run in threads and report progress through Django's
# in-memory cache, so every request has to reach the same process.
CMD ["gunicorn", "jolof.wsgi", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "--no-control-socket"]
