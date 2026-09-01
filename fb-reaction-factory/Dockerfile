FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p output sources/approved_inbox reactions data/reaction_uploads data/cloud_upload_cache

EXPOSE 8080

CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 1800 --bind 0.0.0.0:${PORT:-8080} cloud_app:app"]
