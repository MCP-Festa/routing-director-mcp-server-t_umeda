import json
import os
import logging
import pathlib
import tempfile
from typing import Optional
from fastmcp import FastMCP

from utils.mcp.auth_manager import TokenManager
from utils.mcp.utils import update_openapi_specs_with_tags
from utils.mcp.tracing import configure_mlflow, install_tool_tracing
from utils.mcp.constants import SERVER_NAME, DEFAULT_OPEN_API_SPEC

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


mcp: Optional[FastMCP] = None
mcp_config: str = ""


def validate_mcp_config_file(config):
    mandatory_keys_keys = ['http_url', 'org_id', 'auth']
    optional_config_keys = ['openapi_spec', 'components', 'mlflow']
    for key in mandatory_keys_keys:
        if key not in config:
            raise ValueError(f"Mandatory key `{key}` is missing in the MCP config file")

    valid_keys = [key for key in (mandatory_keys_keys + optional_config_keys) if key in config]
    logger.info("MCP config file validation successful. Configs used: %s", valid_keys)

    # validating the auth section in the config file
    auth_config = config.get('auth', {})
    auth_type = auth_config.get('type')
    if auth_type == 'basic':
        if auth_config.get('username', None) is None or auth_config.get('password', None) is None:
            raise ValueError("Routing Director's GUI's username and password must be provided for basic authentication in the MCP config file.")
    elif auth_type == 'token':
        if auth_config.get('token') is None:
            raise ValueError("Routing Director's API Token must be provided for token authentication in the MCP config file.")
    else:
        raise ValueError(f"Invalid auth type `{auth_type}` in the MCP config file. Supported types are `token` and `basic`.")

    return True


def load_config(config_path: str) -> dict:
    if not config_path:
        raise ValueError("Config file path is required")
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load config from {config_path}: {e}")


# Environment variables that can supply or override config.json values. Each maps an
# env var name to the nested path it sets in the config dict. This lets the server run
# with no config file (e.g. in containers) — see README "Configuration via environment".
_ENV_CONFIG_MAP = {
    "RD_HTTP_URL": ("http_url",),
    "RD_ORG_ID": ("org_id",),
    "RD_OPENAPI_SPEC": ("openapi_spec",),
    "RD_AUTH_TYPE": ("auth", "type"),
    "RD_AUTH_USERNAME": ("auth", "username"),
    "RD_AUTH_PASSWORD": ("auth", "password"),
    "RD_AUTH_TOKEN": ("auth", "token"),
    "RD_MLFLOW_TRACKING_URI": ("mlflow", "tracking_uri"),
    "RD_MLFLOW_EXPERIMENT": ("mlflow", "experiment"),
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base, returning a new dict. Overlay wins."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _set_nested(config: dict, path: tuple, value) -> None:
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def config_from_env() -> dict:
    """Build a (partial) config dict from RD_* environment variables.

    Returns {} when no relevant variables are set. RD_CONFIG_JSON, if present, is
    parsed as the base config; individual RD_* variables then override it. Empty
    strings are treated as unset, so unset compose variables don't clobber values.
    """
    config: dict = {}

    raw_json = os.getenv("RD_CONFIG_JSON")
    if raw_json:
        try:
            config = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"RD_CONFIG_JSON is not valid JSON: {e}")

    for env_var, path in _ENV_CONFIG_MAP.items():
        value = os.getenv(env_var)
        if value not in (None, ""):
            _set_nested(config, path, value)

    components = os.getenv("RD_COMPONENTS")
    if components not in (None, ""):
        config["components"] = [c.strip() for c in components.split(",") if c.strip()]

    enabled = os.getenv("RD_MLFLOW_ENABLED")
    if enabled not in (None, ""):
        _set_nested(config, ("mlflow", "enabled"), enabled.strip().lower() in ("1", "true", "yes", "on"))

    # Infer auth type when only credentials (and not the type) were provided via env.
    auth = config.get("auth")
    if isinstance(auth, dict) and not auth.get("type"):
        if auth.get("token"):
            auth["type"] = "token"
        elif auth.get("username") or auth.get("password"):
            auth["type"] = "basic"

    return config


def resolve_config(config_path: str):
    """Resolve the effective config from a file and/or RD_* environment variables.

    Precedence (low -> high): config file, then environment variables. Either source
    alone is sufficient. Returns (config, used_env); used_env tells the caller whether
    env vars contributed, so it knows the merged config must be re-materialized for the
    client layer (client_connection.py reads config from the MCP_CONFIG file path).
    """
    file_config = {}
    if config_path and os.path.exists(config_path):
        file_config = load_config(config_path)

    env_config = config_from_env()
    if not file_config and not env_config:
        raise ValueError(
            "No configuration found. Provide --config <file> or set RD_* environment "
            "variables (e.g. RD_HTTP_URL, RD_ORG_ID, and RD_AUTH_TOKEN or "
            "RD_AUTH_USERNAME/RD_AUTH_PASSWORD)."
        )

    return _deep_merge(file_config, env_config), bool(env_config)


def _write_temp_config(config: dict) -> str:
    """Write the merged config to a temp file so the client layer can read it."""
    fd, path = tempfile.mkstemp(prefix="rd_mcp_config_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    logger.info("Merged config written to %s for client use.", path)
    return path

def _load_mcp_plugins() -> None:
    # Import all MCP modules so they can register with FastMCP
    import utils.mcp.ems  # noqa: F401
    import utils.mcp.fh  # noqa: F401
    import utils.mcp.insights  # noqa: F401
    import utils.mcp.network_optimization  # noqa: F401
    import utils.mcp.trust  # noqa: F401
    import utils.mcp.active_assurance  # noqa: F401
    import utils.mcp.routing_intelligence  # noqa: F401


def create_mcp_server(args):
    global mcp_config
    global mcp

    # Config may come from a file (--config), RD_* environment variables, or both
    # (env overrides the file). This allows running without a config file in containers.
    config, used_env = resolve_config(args.config)
    validate_mcp_config_file(config)

    # Setting necessary environment variables for the MCP server based on the config.
    os.environ['EOP_HOST'] = config['http_url']
    # The client layer (client_connection.py) reads the config from the MCP_CONFIG file
    # path. When env vars contributed (or no readable file was given), materialize the
    # merged config to a temp file so the client sees the same effective config.
    if used_env or not (args.config and os.path.exists(args.config)):
        mcp_config = _write_temp_config(config)
    else:
        mcp_config = args.config
    os.environ['MCP_CONFIG'] = mcp_config

    token_manager = TokenManager()

    verifier = None
    if args.transport != 'stdio':
        if token_manager.tokens_file_exists():
            logger.info("Tokens file found. Authentication enabled.")
            verifier = token_manager.get_verifier()
        else:
            logger.warning("No tokens file found. Authentication DISABLED.")
            verifier = None
    else:
        logger.info("Using stdio transport - authentication bypassed")

    openapi_spec_path = config.get('openapi_spec')

    spec = None
    if openapi_spec_path:
        logger.info("User provided OpenAPI spec found at %s. Loading it for MCP server.", openapi_spec_path)
        with open(openapi_spec_path) as fh:
            spec = json.load(fh)
    else:
        try:
            default_openapi_spec_full_path = str(pathlib.Path(__file__).parent.parent.parent / DEFAULT_OPEN_API_SPEC)
            if os.path.exists(default_openapi_spec_full_path):
                logger.info(f"Loading default OpenAPI spec from {default_openapi_spec_full_path}")
                with open(default_openapi_spec_full_path) as fh:
                    spec = json.load(fh)
        except Exception as e:
            logger.error(f"Failed to load default OpenAPI spec: {e}")

    if spec:
        components = config.get('components', [])
        # If user provides a valid component list in the config.json, filtering the openapi spec for the specified
        # components, else filtering the openapi spec with the default component list . Only the endpoints with the
        # openapi extension `x-mcp-server` will be included for  mcp, not all the endpoints in the openapi spec.
        updated_spec, include_tags = update_openapi_specs_with_tags(openapi_spec=spec, components=components)

        from utils.lm_calls.connection.client_connection import create_routing_director_async_client
        # The tool calls generated from the openapi spec doesn't work with sync httpx client, so using
        # async httpx client for the mcp server when openapi spec is provided. The async client is only
        # used for the tool calls, the mcp server itself will run in sync mode.
        async_con = create_routing_director_async_client()

        mcp = FastMCP.from_openapi(name=SERVER_NAME, openapi_spec=updated_spec,
                                   client= async_con, auth=verifier, include_tags=include_tags)
    else:
        logger.info("OpenAPI spec not available. Starting MCP server without it, some of the functionality might be unavailable.")
        mcp = FastMCP(name=SERVER_NAME, log_level="DEBUG", auth=verifier)

    # Configure MLflow tracing (opt-in via the `mlflow` config section) and wrap
    # mcp.tool BEFORE loading the plugins, so every tool they register is traced.
    if configure_mlflow(config):
        install_tool_tracing(mcp)

    _load_mcp_plugins()
    mcp.prompt(f"Organization ID or org id is {config.get('org_id')}")

    # List all registered tools before starting the server
    try:
        tools = mcp._tool_manager._tools
        logger.info("Total registered MCP tools %d: %s", len(tools), sorted(tools.keys()))
    except Exception as e:
        logger.warning("Could not list registered tools: %s", e)

    # Prepare SSL config if provided
    uvicorn_config = None
    if getattr(args, 'ssl_key', None) and getattr(args, 'ssl_cert', None):
        if os.path.exists(args.ssl_key) and os.path.exists(args.ssl_cert):
            logger.info(f"SSL enabled. Cert: {args.ssl_cert}, Key: {args.ssl_key}")
            uvicorn_config = {
                "ssl_keyfile": args.ssl_key,
                "ssl_certfile": args.ssl_cert
            }
        else:
            logger.error(f"SSL files not found: {args.ssl_key} or {args.ssl_cert}")

    if args.transport == 'stdio':
        mcp.run(transport=args.transport)
    elif args.transport == 'streamable-http':
        mcp.run(host=args.host, port=args.port, transport=args.transport, uvicorn_config=uvicorn_config)
    else:
        mcp.run(host=args.host, port=args.port, transport=args.transport)
