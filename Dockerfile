FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && install -d -o app -g app /data

COPY --chown=app:app src /app/src
COPY --chown=app:app config /app/config
COPY --chown=app:app fixtures /app/fixtures
COPY --chown=app:app pyproject.toml README.md LICENSE /app/

USER app

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "-m", "arsenal_alert", "run"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "arsenal_alert", "healthcheck"]
