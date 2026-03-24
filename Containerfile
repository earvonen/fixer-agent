FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

USER 1000

ENTRYPOINT ["python", "-m", "fixer_agent"]
