FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && chgrp -R 0 /app \
    && chmod -R g=u /app

ENV PYTHONUNBUFFERED=1

# No USER: OpenShift restricted-v2 runs the container with an arbitrary UID in the namespace
# range; root group (0) can read/execute installed site-packages via g=u on /app.

ENTRYPOINT ["python", "-m", "fixer_agent"]
