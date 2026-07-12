FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY pyproject.toml ./

ENV PORT=8080
EXPOSE 8080

# Single worker, on purpose (#142). Two of this app's core services are
# per-process state that breaks with N>1 workers:
#   - SecretVault is in-process memory: stash lands in worker A, the
#     import that consumes the vault_token hits worker B -> "vault_token
#     is unknown or has expired" ~50% of the time.
#   - Each worker starts its own APScheduler against the SAME SQLite
#     jobstore (max_instances=1 is per-scheduler, not cross-process), so
#     strategy cron ticks can double-fire -> duplicate live trades.
# This is a local single-user agent; one worker is the correct shape.
CMD ["sh", "-c", "cp -n src/config/local-example-config.json src/config/local-config.json 2>/dev/null; exec uvicorn src.app:app --host 0.0.0.0 --port 8080 --workers 1 --timeout-keep-alive 120"]
