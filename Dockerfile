# Python 3.11 (project requires >=3.10,<3.12 — see pyproject.toml)
FROM python:3.11-slim

# Bring in the uv binary for fast, cached dependency installs. Pin a tag (e.g.
# ghcr.io/astral-sh/uv:0.5.11) instead of :latest for fully reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install Python dependencies first so this layer is cached across source changes.
# --system installs into the image's Python (no venv needed inside a container).
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Application source. The default OpenAPI spec must sit at the app root, since
# server.py resolves it relative to the repo root (see DEFAULT_OPEN_API_SPEC).
COPY RoutingDirectorMCP.py jrd-2.8.0-mcp-spec.json ./
COPY utils ./utils

EXPOSE 8012

# Config is provided at runtime via RD_* env vars or a bind-mounted config.json
# (see compose.yml). --host 0.0.0.0 makes the server reachable from outside the
# container; stdio transport is not used here as this is a long-running service.
ENTRYPOINT ["python", "RoutingDirectorMCP.py"]
CMD ["--config", "/app/config.json", "--host", "0.0.0.0", "--port", "8012", "--transport", "streamable-http"]
