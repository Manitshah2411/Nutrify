# Backend Notes

The main project documentation now lives at the repository root:

- [Project README](../README.md)

This backend directory contains the Flask application, migrations, tests, deployment scripts, and production server configuration used by Nutrify.

## Quick backend commands

```bash
cd backend

python -m flask --app wsgi:application db upgrade -d migrations
python manage.py init-dev
python run.py
```

## Test suite

```bash
cd backend
pytest tests
```

## Production startup

Render uses:

```bash
./bin/render_predeploy.sh
gunicorn -c gunicorn.conf.py wsgi:application
```
