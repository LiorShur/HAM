FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
RUN pip install --no-cache-dir .
COPY --from=web /web/dist ./web/dist
ENV PYTHONUNBUFFERED=1 WCM_WEB_DIST=/app/web/dist
EXPOSE 8000
CMD ["wcm", "serve", "--host", "0.0.0.0", "--port", "8000"]
