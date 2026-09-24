FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY rec ./rec
RUN uv pip install --system --no-cache .
COPY db ./db
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "rec.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
