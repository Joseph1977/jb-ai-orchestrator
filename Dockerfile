FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ .
COPY alembic.ini .
COPY alembic/ ./alembic/
RUN mkdir -p ./Logs

# Expose port
ENV PYTHONPATH=/app
ENV PORT=8000

EXPOSE 8000
# Apply the persisted execution schema before accepting requests. With the
# deployment restart policy, a temporarily unavailable database is retried.
CMD ["sh", "-c", "alembic upgrade head && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
