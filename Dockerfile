FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 trader \
    && useradd --uid 10001 --gid trader --no-create-home trader \
    && mkdir /data \
    && chown trader:trader /data

COPY config/default.toml /app/config/default.toml
COPY --chmod=755 docker/entrypoint.sh /app/entrypoint.sh
USER trader
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["paper-start"]
