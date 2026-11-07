# The web UI. It starts the runs through the Docker socket of the host, so it needs the Docker CLI but no daemon.
FROM docker:29.9.2-cli AS docker

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt .
# The same environment ./start creates otherwise on the first run.
RUN python3 -m venv .venv && .venv/bin/pip install --no-cache-dir --quiet -r requirements.txt
COPY start .
COPY app app

# The container runs with the UID of the server's user; there is no entry in /etc/passwd and no home for it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    COMPANY_HOST=0.0.0.0

EXPOSE 8700
CMD ["./start"]
