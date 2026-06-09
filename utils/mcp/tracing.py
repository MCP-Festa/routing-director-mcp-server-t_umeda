import logging

from utils.mcp.constants import SERVER_NAME

logger = logging.getLogger(__name__)

# Holds the imported mlflow module once tracing has been successfully configured.
# Stays None when tracing is disabled or mlflow is unavailable, which is the
# signal used by install_tool_tracing() to skip wrapping entirely.
_mlflow = None


def configure_mlflow(config: dict) -> bool:
    """Configure MLflow tracing from the optional `mlflow` section of config.json.

    Tracing is opt-in: with no `mlflow` section (or `enabled: false`) nothing is
    imported or initialised and the server behaves exactly as before.

    Supported config keys (all optional except the section itself):
        mlflow.tracking_uri : MLflow tracking server URI. If omitted, MLflow falls
                              back to the MLFLOW_TRACKING_URI env var or local ./mlruns.
        mlflow.experiment   : Experiment name to log traces under. Defaults to SERVER_NAME.
        mlflow.enabled      : Set to false to disable tracing while keeping the section.

    Returns:
        True if tracing was enabled, False otherwise.
    """
    mlflow_config = config.get("mlflow")
    if not mlflow_config:
        logger.info("No `mlflow` section in config; MLflow tracing disabled.")
        return False

    if mlflow_config.get("enabled", True) is False:
        logger.info("MLflow tracing explicitly disabled via config (`mlflow.enabled` is false).")
        return False

    global _mlflow
    try:
        import mlflow
    except ImportError:
        logger.error(
            "An `mlflow` config section was provided but the `mlflow` package is not installed. "
            "Install it with `pip install mlflow`. Tracing disabled."
        )
        return False

    tracking_uri = mlflow_config.get("tracking_uri")
    experiment = mlflow_config.get("experiment", SERVER_NAME)

    # Tracing is an optional observability feature — a misconfigured backend must
    # never crash the server. Note: with MLflow 3.x the local file store (the
    # default when no tracking_uri is given) raises unless MLFLOW_ALLOW_FILE_STORE
    # is set, so point tracking_uri at a tracking server or a DB (e.g. sqlite:///).
    try:
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
            logger.info("MLflow tracking URI set to %s", tracking_uri)
        mlflow.set_experiment(experiment)
    except Exception as e:
        logger.error("Failed to initialise MLflow tracing (%s). Tracing disabled; server continues.", e)
        return False

    _mlflow = mlflow
    logger.info("MLflow tracing enabled. Experiment: %s", experiment)
    return True


def install_tool_tracing(mcp) -> None:
    """Wrap `mcp.tool` so every tool function registered by the plugin modules is
    traced with MLflow.

    This is the single choke point that keeps the registration layer untouched:
    every plugin calls `mcp.tool(fn, tags=...)`, so wrapping the bound method here
    instruments all hand-written tools at once. Must be called AFTER the FastMCP
    instance is built and BEFORE `_load_mcp_plugins()` imports the plugins, so the
    wrapper is in place when they register their tools.
    """
    if _mlflow is None:
        return

    original_tool = mcp.tool

    def traced_tool(fn=None, **kwargs):
        # Direct form used throughout the plugins: mcp.tool(fn, tags={...}).
        if fn is not None and callable(fn):
            traced = _mlflow.trace(fn, name=getattr(fn, "__name__", None), span_type="TOOL")
            return original_tool(traced, **kwargs)

        # Decorator form: @mcp.tool(...) — wrap the function once it arrives.
        def decorator(f):
            traced = _mlflow.trace(f, name=getattr(f, "__name__", None), span_type="TOOL")
            return original_tool(traced, **kwargs)

        return decorator

    mcp.tool = traced_tool
    logger.info("MLflow tool tracing installed on FastMCP instance.")
