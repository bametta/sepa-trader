FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user: a flaw in the app should not mean root inside the container.
# The app only talks to Postgres/Redis and the network; it writes no files, so no volumes need to be writable.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1

COPY --chown=app:app . .
USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
