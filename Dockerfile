# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Auszugsbote" \
      org.opencontainers.image.description="Holt Kontoauszüge per FinTS ab und legt sie in einem freigegebenen Ordner ab" \
      org.opencontainers.image.source="https://github.com/boberbeck/auszugsbote" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUSZUGSBOTE_DATA=/data \
    AUSZUGSBOTE_VAULT_KEYFILE=/run/secrets/vault_key \
    TZ=Europe/Berlin
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels \
 && useradd --uid 1000 --user-group --no-create-home --home-dir /data auszugsbote \
 && mkdir -p /data/ablage && chown -R 1000:1000 /data
USER 1000:1000
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1
ENTRYPOINT ["auszugsbote"]
CMD ["serve"]
