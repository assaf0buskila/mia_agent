FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY migrations ./migrations
RUN chmod 644 /etc/ssl/certs/rds-global-bundle.pem \
 && uv sync --frozen --no-dev --no-editable \
 && useradd --uid 10001 --home-dir /app --shell /usr/sbin/nologin --no-create-home mia \
 && chown -R mia:mia /app
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
USER mia
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "130"]
