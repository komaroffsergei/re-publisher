FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY scripts/install_channel_routes.py ./scripts/install_channel_routes.py
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN cp /app/config/certs/*.crt /usr/local/share/ca-certificates/ \
    && update-ca-certificates

RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 .

CMD ["python", "-m", "app.main", "run", "--folder", "MAX"]
