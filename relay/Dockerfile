FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROJECT_Q_RELAY_DB=/data/project_q_relay.db

RUN groupadd --gid 10001 relay \
    && useradd --uid 10001 --gid relay --create-home relay

WORKDIR /app
COPY pyproject.toml README.md ./
COPY project_q_relay ./project_q_relay
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown -R relay:relay /data /app
USER relay

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["uvicorn", "project_q_relay.app:create_relay_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
