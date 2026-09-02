# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
from dotenv import load_dotenv
from pathlib import Path
from app.utils.logger import logger
from app.utils.config_logging import log_safe_configuration


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean-ish environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_running_in_kubernetes():
    """Check if we're running inside a Kubernetes pod"""
    return os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount')

# Check if we're running in Kubernetes
IS_K8S = is_running_in_kubernetes()

if IS_K8S:
    logger.info("Running in Kubernetes, using environment variables")
else:
    try:
        # Only load .env file if we're not in Kubernetes
        env_name = os.getenv('ENV', 'localhost')
        env_file = Path(__file__).parent.parent / '.env' / env_name / '.env'

        if env_file.exists():
            logger.info(f"Running locally, loading environment from: {env_file}")
            load_dotenv(env_file)
        else:
            logger.warning(f"Environment file not found: {env_file}")
    except Exception as e:
        logger.warning(f"Failed to load .env file: {str(e)}")

class Config:
    # Service Configuration
    REGION = os.getenv('Region', 'USC1')
    ENVIRONMENT = os.getenv('Environment', 'DEV')
    SERVICE_NAME = os.getenv('ServiceName', 'jb-ai-orchestrator-service')

    # MCP Configuration - Support multiple servers
    MCP_SERVER_URLS = None  # Will be set in _parse_mcp_servers()

    @classmethod
    def _parse_mcp_servers(cls):
        """Parse MCP server URLs from environment variables"""
        import json  # Import json here to avoid issues

        # Option 1: Check for array format first (MCP_SERVER_URLS as JSON array with named objects)
        mcp_urls_str = os.getenv('MCP_SERVER_URLS')
        if mcp_urls_str:
            try:
                parsed_urls = json.loads(mcp_urls_str)

                # Only an array of {name, url} objects is honoured. Anything else
                # (including an array of bare URL strings) is ignored here and
                # falls through to the numbered MCP_SERVER_URL_{n} variables.
                if isinstance(parsed_urls, list) and len(parsed_urls) > 0:
                    if isinstance(parsed_urls[0], dict):
                        # New format: [{"name": "general", "url": "..."}, {"name": "search", "url": "..."}]
                        # Handle empty names by converting to "default"
                        for server in parsed_urls:
                            if not server.get('name') or server['name'].strip() == '':
                                server['name'] = 'default'
                        cls.MCP_SERVER_URLS = parsed_urls
                        return
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON format for MCP_SERVER_URLS: {mcp_urls_str}")

        # Option 2: Check for individual numbered URLs (MCP_SERVER_URL_1, MCP_SERVER_URL_2, etc.)
        # These should be JSON objects: {"name": "...", "url": "..."}
        numbered_urls = []
        i = 1
        while True:
            url_config = os.getenv(f'MCP_SERVER_URL_{i}')
            if url_config:
                try:
                    # Parse as JSON object
                    server_obj = json.loads(url_config)
                    if isinstance(server_obj, dict) and 'name' in server_obj and 'url' in server_obj:
                        # Handle empty names by converting to "default"
                        if not server_obj['name'] or server_obj['name'].strip() == '':
                            server_obj['name'] = 'default'
                        numbered_urls.append(server_obj)
                    else:
                        logger.warning(f"MCP_SERVER_URL_{i} must be a JSON object with 'name' and 'url' properties")
                    i += 1
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON format for MCP_SERVER_URL_{i}: {url_config}")
                    i += 1
            else:
                break

        if numbered_urls:
            cls.MCP_SERVER_URLS = numbered_urls
            return

        # If no valid configuration found, set empty list (will trigger validation error)
        cls.MCP_SERVER_URLS = []

    # LiteLLM Configuration
    LITELLM_BASE_URL = os.getenv('LITELLM_BASE_URL', 'http://localhost:4000')
    LITELLM_API_KEY = os.getenv('LITELLM_API_KEY', 'sk-1234')
    LITELLM_REQUEST_TIMEOUT_IN_SEC = int(os.getenv('LITELLM_REQUEST_TIMEOUT_IN_SEC', 300))
    LITELLM_DROP_PARAMS = os.getenv('LITELLM_DROP_PARAMS', 'True')
    # Absolute wall-clock budget for one LiteLLM call (streaming or not).
    LITELLM_MODEL_DEADLINE_SEC = int(os.getenv('LITELLM_MODEL_DEADLINE_SEC', '240'))
    # Completion-token cap forwarded to LiteLLM (0 disables).
    LITELLM_MAX_COMPLETION_TOKENS = int(os.getenv('LITELLM_MAX_COMPLETION_TOKENS', '4096'))

    # Database Configuration
    DATABASE_URL = os.getenv('DATABASE_URL')

    # Logging Configuration
    LOG_FOLDER = os.getenv('General_LogFolder', './Logs')
    LOG_LEVEL = os.getenv('Logging_LogLevel_Default', 'Information')

    # Swagger Configuration
    SWAGGER_BASE_PATH = os.getenv('SwaggerBasePath', '')

    # Agent Configuration
    MAX_TOOL_CALLS = int(os.getenv('MAX_TOOL_CALLS', '10'))
    # Seconds before an in-progress resume claim (PENDING) is treated as stale
    # and restored to AWAITING_RESPONSE for retry.
    RESUME_CLAIM_TIMEOUT_SEC = int(os.getenv('RESUME_CLAIM_TIMEOUT_SEC', '300'))
    # Phase 3: run heartbeat interval while a request is active (seconds).
    RUN_HEARTBEAT_INTERVAL_SEC = int(os.getenv('RUN_HEARTBEAT_INTERVAL_SEC', '5'))
    # Phase 3: heartbeat age after which an active/closing run is treated as stale.
    RUN_HEARTBEAT_STALE_SEC = int(os.getenv('RUN_HEARTBEAT_STALE_SEC', '300'))
    # Phase 3: wait/poll budget before returning closing when runs remain active.
    CLOSE_WAIT_TIMEOUT_SEC = int(os.getenv('CLOSE_WAIT_TIMEOUT_SEC', '10'))
    # Hard wall-clock budget for one executable segment (tool loop + model calls).
    RUN_SEGMENT_DEADLINE_SEC = int(os.getenv('RUN_SEGMENT_DEADLINE_SEC', '270'))
    # Log a structured warning when worker cancellation exceeds this threshold.
    RUN_CANCELLATION_WARN_SEC = int(os.getenv('RUN_CANCELLATION_WARN_SEC', '5'))

    # Orchestrator / Workspace Configuration
    # Root directory under which each orchestration run gets an isolated
    # per-execution sandbox (WORKSPACES_ROOT/{execution_id}/...).
    WORKSPACES_ROOT = os.getenv(
        'WORKSPACES_ROOT',
        os.path.join(tempfile.gettempdir(), 'jb-agent-workspaces'),
    )
    # Allow operating directly on a local folder path without copying it into
    # a sandbox. Required for AG-UI `workspacePath` + `inPlace=true` (web sessions)
    # and orchestrator initiate with inPlace=true.
    ALLOW_INPLACE_WORKSPACE = _env_bool('ALLOW_INPLACE_WORKSPACE', False)
    # Timeout (seconds) for `git clone` when provisioning a repo workspace.
    GIT_CLONE_TIMEOUT_SEC = int(os.getenv('GIT_CLONE_TIMEOUT_SEC', '120'))
    # Soft cap on how large a provisioned workspace may be (MB). 0 disables.
    MAX_WORKSPACE_MB = int(os.getenv('MAX_WORKSPACE_MB', '0'))
    # Phase 2+ : enable non-null output bindings and output tools.
    OUTPUT_BINDINGS_ENABLED = _env_bool('OUTPUT_BINDINGS_ENABLED', False)
    OUTPUT_READ_MAX_BYTES = int(os.getenv('OUTPUT_READ_MAX_BYTES', str(5 * 1024 * 1024)))
    OUTPUT_WRITE_MAX_BYTES = int(os.getenv('OUTPUT_WRITE_MAX_BYTES', str(5 * 1024 * 1024)))
    OUTPUT_LIST_MAX_ENTRIES = int(os.getenv('OUTPUT_LIST_MAX_ENTRIES', '1000'))
    AZURE_BLOB_TIMEOUT_SEC = int(os.getenv('AZURE_BLOB_TIMEOUT_SEC', '60'))
    # YAML prompt templates (output-binding instructions, offload notes).
    PROMPTS_DIR = os.getenv('PROMPTS_DIR', '')

    # Built-in local tools (filesystem / git / ask_user) exposed in-process.
    LOCAL_TOOLS_ENABLED = _env_bool('LOCAL_TOOLS_ENABLED', True)
    # Namespace/suffix used to mark built-in local tools (e.g. read_file_local).
    LOCAL_TOOLS_NAMESPACE = os.getenv('LOCAL_TOOLS_NAMESPACE', 'local')
    # When a remote MCP tool's base name collides with a built-in local tool,
    # drop the remote one so the LLM only sees the local version.
    FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL = _env_bool(
        'FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL', True
    )

    # Local shell tool (execute_local). Runs with cwd=workspace; not a full OS jail.
    LOCAL_SHELL_ENABLED = _env_bool('LOCAL_SHELL_ENABLED', True)
    LOCAL_SHELL_TIMEOUT_SEC = int(os.getenv('LOCAL_SHELL_TIMEOUT_SEC', '60'))
    LOCAL_SHELL_MAX_OUTPUT_BYTES = int(os.getenv('LOCAL_SHELL_MAX_OUTPUT_BYTES', '100000'))

    # Context management: offload oversized tool results and compact history.
    TOOL_RESULT_OFFLOAD_CHARS = int(os.getenv('TOOL_RESULT_OFFLOAD_CHARS', '8000'))
    CONTEXT_COMPACTION_CHARS = int(os.getenv('CONTEXT_COMPACTION_CHARS', '120000'))
    # When > 0, compaction triggers on estimated/reported prompt tokens instead of chars.
    CONTEXT_COMPACTION_TOKENS = int(os.getenv('CONTEXT_COMPACTION_TOKENS', '0'))
    CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS = int(
        os.getenv('CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS', '8')
    )
    CONTEXT_COMPACTION_KEEP_RECENT_TURNS = int(
        os.getenv('CONTEXT_COMPACTION_KEEP_RECENT_TURNS', '4')
    )
    CONTEXT_COMPACTION_ENABLED = _env_bool('CONTEXT_COMPACTION_ENABLED', True)
    CONTEXT_SUMMARIZATION_ENABLED = _env_bool('CONTEXT_SUMMARIZATION_ENABLED', True)
    # Empty = use the active run model.
    CONTEXT_SUMMARIZATION_MODEL = os.getenv('CONTEXT_SUMMARIZATION_MODEL', '')

    # Subagents (task_local): nested isolated runs that return a summary.
    SUBAGENT_ENABLED = _env_bool('SUBAGENT_ENABLED', True)
    SUBAGENT_MAX_TOOL_CALLS = int(os.getenv('SUBAGENT_MAX_TOOL_CALLS', '8'))
    SUBAGENT_MAX_DEPTH = int(os.getenv('SUBAGENT_MAX_DEPTH', '2'))

    # Stream LiteLLM tokens into AG-UI TEXT_MESSAGE events when a UI channel exists.
    LLM_STREAMING_ENABLED = _env_bool('LLM_STREAMING_ENABLED', True)

    # Cursor / Claude project hooks (.cursor/hooks.json, .claude/hooks|settings).
    HOOKS_ENABLED = _env_bool('HOOKS_ENABLED', True)
    HOOKS_FAIL_CLOSED = _env_bool('HOOKS_FAIL_CLOSED', False)
    HOOKS_TIMEOUT_SEC = int(os.getenv('HOOKS_TIMEOUT_SEC', '30'))
    HOOKS_MAX_OUTPUT_BYTES = int(os.getenv('HOOKS_MAX_OUTPUT_BYTES', '100000'))

    # Path / shell allow-deny policy (multi-tenant hardening).
    # When enabled with empty lists, behavior matches resolve_within-only sandboxing.
    PATH_POLICY_ENABLED = _env_bool('PATH_POLICY_ENABLED', True)
    # Comma-separated globs relative to the workspace (e.g. ".env,**/*.pem,**/.ssh/**").
    PATH_DENYLIST = os.getenv('PATH_DENYLIST', '')
    # If non-empty, only matching relative paths are allowed (deny still wins).
    PATH_ALLOWLIST = os.getenv('PATH_ALLOWLIST', '')
    # Comma-separated absolute roots for in-place workspacePath binding
    # (e.g. "/app/sessions"). Empty = any path allowed when ALLOW_INPLACE_WORKSPACE.
    WORKSPACE_ALLOWED_ROOTS = os.getenv('WORKSPACE_ALLOWED_ROOTS', '')
    # Comma-separated regexes; command is blocked if any match.
    SHELL_COMMAND_DENYLIST = os.getenv('SHELL_COMMAND_DENYLIST', '')
    # If non-empty, command must match at least one regex.
    SHELL_COMMAND_ALLOWLIST = os.getenv('SHELL_COMMAND_ALLOWLIST', '')

    # Multimodal file reads (images / PDFs via LiteLLM content parts).
    MULTIMODAL_ENABLED = _env_bool('MULTIMODAL_ENABLED', True)
    MULTIMODAL_MAX_BYTES = int(os.getenv('MULTIMODAL_MAX_BYTES', str(5 * 1024 * 1024)))
    # Comma-separated substrings; empty = no allowlist filter (use VISION_MARKERS).
    MULTIMODAL_MODEL_ALLOWLIST = os.getenv('MULTIMODAL_MODEL_ALLOWLIST', '')
    # Comma-separated substrings that never get attachments (text-only / embedding models).
    MULTIMODAL_MODEL_DENYLIST = os.getenv(
        'MULTIMODAL_MODEL_DENYLIST',
        'gpt-3.5,gpt-3.5-turbo,text-embedding,embedding,whisper,tts,davinci,babbage,curie',
    )
    # Model-id substrings treated as vision/document-capable when allowlist is empty.
    MULTIMODAL_VISION_MARKERS = os.getenv(
        'MULTIMODAL_VISION_MARKERS',
        'gpt-4o,gpt-4.1,gpt-4-turbo,gpt-5,o1,o3,o4,'
        'claude-3,claude-4,claude-sonnet,claude-opus,claude-haiku,'
        'gemini,gemini-1.5,gemini-2,vision',
    )

    @classmethod
    def validate_config(cls):
        """Validate required configuration values"""
        # Parse MCP servers first
        cls._parse_mcp_servers()

        if cls.LITELLM_MODEL_DEADLINE_SEC <= 0:
            raise ValueError("LITELLM_MODEL_DEADLINE_SEC must be greater than zero")
        if cls.RUN_SEGMENT_DEADLINE_SEC <= cls.LITELLM_MODEL_DEADLINE_SEC:
            raise ValueError(
                "RUN_SEGMENT_DEADLINE_SEC must exceed LITELLM_MODEL_DEADLINE_SEC"
            )
        if cls.LITELLM_MAX_COMPLETION_TOKENS < 0:
            raise ValueError("LITELLM_MAX_COMPLETION_TOKENS must not be negative")
        if cls.RUN_CANCELLATION_WARN_SEC <= 0:
            raise ValueError("RUN_CANCELLATION_WARN_SEC must be greater than zero")

        required_configs = ['DATABASE_URL']
        missing_configs = []

        for config in required_configs:
            if not getattr(cls, config):
                missing_configs.append(config)

        if missing_configs:
            raise ValueError(f"Missing required configuration: {', '.join(missing_configs)}")

        # Validate MCP servers
        if not cls.MCP_SERVER_URLS or len(cls.MCP_SERVER_URLS) == 0:
            raise ValueError("At least one MCP server URL must be configured")

        # Validate server objects format
        for i, server in enumerate(cls.MCP_SERVER_URLS):
            if not isinstance(server, dict) or 'name' not in server or 'url' not in server:
                raise ValueError(f"MCP server at index {i} must be an object with 'name' and 'url' properties")

        server_names = [server['name'] for server in cls.MCP_SERVER_URLS]
        server_urls = [server['url'] for server in cls.MCP_SERVER_URLS]

        logger.info("Configuration validation passed")
        logger.info(f"Configured {len(cls.MCP_SERVER_URLS)} MCP servers:")
        for server in cls.MCP_SERVER_URLS:
            logger.info(f"  - {server['name']}: {server['url']}")

# Dynamically load environment variables into Config (never log raw env).
for key, value in os.environ.items():
    if key not in Config.__dict__ or isinstance(Config.__dict__.get(key), str):
        setattr(Config, key, value)

log_safe_configuration(Config, logger)
