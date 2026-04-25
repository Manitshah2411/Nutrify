# Nutrify

Nutrify is a Flask-based school health monitoring and meal-planning system.

## Development

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in the required values.
4. Apply the database schema:
   ```bash
   python -m flask --app wsgi:application db upgrade -d migrations
   python manage.py init-dev
   ```
5. Run the app:
   ```bash
   python run.py
   ```

## Production

Use Gunicorn with the checked-in config:

```bash
gunicorn -c gunicorn.conf.py wsgi:application
```

This repo now includes:

- `render.yaml` at the repo root for a Render Blueprint
- `backend/migrations/` with the initial Alembic history
- `backend/bin/render_predeploy.sh` to run migrations and seed reference data before each deploy
- `.github/workflows/backend-ci.yml` so Render can wait for CI with `autoDeployTrigger: checksPass`

## Render deployment

1. Push this repo to GitHub.
2. In Render, choose `New > Blueprint`.
3. Select this repo and sync the checked-in `render.yaml`.
4. Provide `GOOGLE_API_KEY` during the initial Blueprint creation flow.
5. After the first deploy, open the web service environment variables and copy `DEFAULT_SCHOOL_PASSWORD`.
   Use that password with the bootstrap school username from `DEFAULT_SCHOOL_USERNAME` to sign in.
6. Change the bootstrap school password after first login.

The Blueprint provisions:

- a Python web service on the `starter` plan
- a managed PostgreSQL database
- automatic zero-downtime health checks using `/health`
- automatic database migrations before startup
- CI-gated deploys via GitHub Actions

## Bootstrap account

For local development, the default bootstrap account remains:

- Username: `BestSchool`
- Password: `school123`

For production, the bootstrap password is expected from `DEFAULT_SCHOOL_PASSWORD`. The Render Blueprint generates this automatically so the production app does not ship with a known default password.

### Database migrations

Create a new migration after model changes:

```bash
python -m flask --app wsgi:application db migrate -d migrations -m "describe change"
python -m flask --app wsgi:application db upgrade -d migrations
```

## Required environment variables

- `SECRET_KEY`
- `DATABASE_URL`
- `GOOGLE_API_KEY` for AI-powered nutrition and recipe features
- `DEFAULT_SCHOOL_PASSWORD` when bootstrapping a production database without an existing school account

Optional variables:

- `APP_ENV`
- `CORS_ORIGINS`
- `LOG_LEVEL`
- `LOG_FILE`
- `RATELIMIT_STORAGE_URL`
- `REDIS_URL`
- `ENABLE_PROXY_FIX`
- `GEMINI_MODEL_NAME`
- `GEMINI_REQUEST_TIMEOUT_SECONDS`
- `SESSION_COOKIE_SAMESITE`
- `REMEMBER_COOKIE_SAMESITE`
- `SESSION_LIFETIME_HOURS`
- `DB_POOL_SIZE`
- `DB_MAX_OVERFLOW`
- `DB_POOL_TIMEOUT`
- `SQLALCHEMY_POOL_RECYCLE`
- `WEB_CONCURRENCY`
- `GUNICORN_TIMEOUT`
