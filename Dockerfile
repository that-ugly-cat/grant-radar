FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# The user guide is served from /guide and lives at the repo root.
COPY docs ./docs
COPY scripts ./scripts

# docker compose build --build-arg GIT_COMMIT=$(git rev-parse --short=12 HEAD)
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

ENV GR_DB=/data/grantradar.db
EXPOSE 8015

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8015"]
