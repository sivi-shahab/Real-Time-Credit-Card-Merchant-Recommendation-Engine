#!/bin/sh
# Serve an ASGI app with uvicorn: `serve.sh <module:app> <port>`. Workers come from
# $WEB_CONCURRENCY. With more than one, set PROMETHEUS_MULTIPROC_DIR so /metrics sums
# every worker; it is emptied first because files left by an earlier container would be
# counted again.
set -e
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
  rm -rf "$PROMETHEUS_MULTIPROC_DIR"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi
exec uvicorn "$1" --host 0.0.0.0 --port "$2"
