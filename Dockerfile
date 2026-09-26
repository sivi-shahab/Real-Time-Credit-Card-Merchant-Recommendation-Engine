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
# Workers come from $WEB_CONCURRENCY. With more than one, set PROMETHEUS_MULTIPROC_DIR so
# /metrics sums every worker; it is emptied here because files left by an earlier
# container would be counted again.
CMD ["sh", "-c", "if [ -n \"$PROMETHEUS_MULTIPROC_DIR\" ]; then rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\"; fi; exec uvicorn rec.api.app:app --host 0.0.0.0 --port 8000"]
