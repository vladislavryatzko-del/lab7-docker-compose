# ---------- stage 1: собираем wheel-пакеты зависимостей ----------
FROM python:3.12-slim AS builder
WORKDIR /build
COPY app/requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- stage 2: минимальный runtime-образ ----------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# не запускаем приложение от root
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl && rm -rf /wheels

COPY app/main.py .
USER appuser
EXPOSE 8000

# Проверка работоспособности: /health отвечает 200 только если доступны и PostgreSQL, и Redis
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
