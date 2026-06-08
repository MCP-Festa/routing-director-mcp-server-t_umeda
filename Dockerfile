# Python 3.11 (project requires >=3.10,<3.12 — see pyproject.toml)
FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies first so this layer is cached across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source. The default OpenAPI spec must sit at the app root, since
# server.py resolves it relative to the repo root (see DEFAULT_OPEN_API_SPEC).
COPY RoutingDirectorMCP.py jrd-2.8.0-mcp-spec.json ./
COPY utils ./utils

EXPOSE 8012

# config.json is provided at runtime via a bind mount (see docker-compose.yml).
# --host 0.0.0.0 makes the server reachable from outside the container; stdio
# transport is not used here as the container is a long-running HTTP service.
ENTRYPOINT ["python", "RoutingDirectorMCP.py"]
CMD ["--config", "/app/config.json", "--host", "0.0.0.0", "--port", "8012", "--transport", "streamable-http"]
