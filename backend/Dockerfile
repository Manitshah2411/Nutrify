FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home appuser \
 && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["gunicorn", "-c", "backend/gunicorn.conf.py", "backend.wsgi:application"]
