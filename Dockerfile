FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAI_LOOP_DATABASE_URL=sqlite:////app/data/pai_loop.db

WORKDIR /app

RUN addgroup --system pai && adduser --system --ingroup pai pai

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R pai:pai /app
USER pai

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["uvicorn", "pai_loop.main:app", "--host", "0.0.0.0", "--port", "8000"]

