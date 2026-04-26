#!/usr/bin/env bash
set -euo pipefail

cd /app

python3 backend/manage.py ensure-migration-state
python3 -m flask --app backend/wsgi.py db upgrade -d backend/migrations
python3 backend/manage.py prepare-deploy

exec gunicorn -c backend/gunicorn.conf.py backend.wsgi:application
