# Superset for the analytics dashboards (.scratch/superset-personalization-analytics).
# The official image ships neither a Postgres driver nor authlib (Keycloak SSO).
FROM apache/superset:5.0.0
USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache \
      psycopg2-binary==2.9.10 authlib==1.3.2
COPY superset_config.py /app/pythonpath/superset_config.py
COPY provision.py /app/provision.py
USER superset
