import os

if os.environ.get("RENDER") and not os.environ.get("APP_ENV"):
    os.environ["APP_ENV"] = "production"

try:
    from backend.app import create_app
except ModuleNotFoundError:
    from app import create_app


app = create_app()
application = app
