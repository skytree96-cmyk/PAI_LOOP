FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAI_LOOP_DATABASE_URL=sqlite:////app/data/pai_loop.db

WORKDIR /app

RUN addgroup --system pai && adduser --system --ingroup pai pai

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

RUN mkdir -p /app/data && chown -R pai:pai /app
USER pai

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2)"

CMD ["sh", "-c", "pai-loop-seed-public-notice --create-schema && exec uvicorn pai_loop.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
