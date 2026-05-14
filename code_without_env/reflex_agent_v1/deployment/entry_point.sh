#!/bin/sh

export PYTHONPATH="$PYTHONPATH:/agent/runner"

cd /agent/runner

exec gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --workers 8 server:app
