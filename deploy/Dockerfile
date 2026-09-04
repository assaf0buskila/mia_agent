FROM python:3.12-alpine
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY migrations ./migrations
RUN apk upgrade --no-cache \
 && chmod 644 /etc/ssl/certs/rds-global-bundle.pem \
 && uv sync --frozen --no-dev --no-editable \
 && addgroup -S -g 10001 mia \
 && adduser -S -D -H -h /app -u 10001 -G mia mia \
 && chown -R mia:mia /app
# The tested commit, passed by the build: docker build --build-arg MIA_BUILD_SHA=$(git rev-parse HEAD)
# Reported on /health as deployment.commit_sha so a stale image is visible from prod.
ARG MIA_BUILD_SHA=""
ENV MIA_BUILD_SHA=${MIA_BUILD_SHA}
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
USER mia
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "130"]
