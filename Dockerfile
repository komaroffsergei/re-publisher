FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN pip install --no-cache-dir .

CMD ["python", "-m", "app.main", "run", "--folder", "MAX"]
