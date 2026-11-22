FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ESTIMATOR_CACHE=/var/data/cache \
    ESTIMATOR_DATA=/var/data/app \
    PORT=8000

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN useradd --create-home appuser && mkdir -p /var/data && chown -R appuser /var/data
# Runs as root only long enough to fix volume ownership, then as appuser.
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
