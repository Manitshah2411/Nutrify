#!/usr/bin/env bash
set -euo pipefail

python3 -m flask --app wsgi:application db upgrade -d migrations
python3 manage.py prepare-deploy
