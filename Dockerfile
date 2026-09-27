FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY rec ./rec
# the worker image adds `[causal]` for the scheduled uplift report (docker-compose.yml)
ARG EXTRAS=""
RUN uv pip install --system --no-cache ".${EXTRAS}"
COPY db ./db
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
CMD ["sh", "scripts/serve.sh", "rec.api.app:app", "8000"]
