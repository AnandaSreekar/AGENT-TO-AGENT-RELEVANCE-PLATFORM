# Stage 1: build the React UI
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: FastAPI serves the API and the built UI from ONE process (required: the SSE hub is in-process)
FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=ui /ui/dist frontend/dist
# Ephemeral by design on Render's free plan: the DB is created + seeded on every boot (see lifespan in app/main.py).
ENV DATABASE_PATH=/tmp/a2a.db PYTHONUNBUFFERED=1
WORKDIR /app/backend
EXPOSE 10000
# --proxy-headers: behind Render's proxy, per-IP limits and registration limits must see the real client IP.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
