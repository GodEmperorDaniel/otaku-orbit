FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ANIMECLUB_DB=/data/animeclub.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --system --uid 1000 animeclub && mkdir /data && chown animeclub /data
USER animeclub
VOLUME /data
EXPOSE 8000

# One worker on purpose: SQLite + the background metadata refresher assume a single process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
