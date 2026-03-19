"""Centralized constants — timeouts, process limits, truncation lengths."""

# --- Subprocess timeouts (seconds) ---

TIMEOUT_GIT_CLONE = 300
TIMEOUT_GIT_PUSH = 120
TIMEOUT_GIT_FETCH = 30
TIMEOUT_GH_COMMAND = 30  # default gh CLI timeout (also in github.py)
TIMEOUT_GH_DIFF = 60
TIMEOUT_NAVIGATION_MODEL = 60
TIMEOUT_REVIEW_AGENT = 300
TIMEOUT_VERIFY_AGENT = 300
TIMEOUT_TRIAGE_MODEL = 60
TIMEOUT_BATCH_ORCHESTRATION = 90
TIMEOUT_CONFLICT_CHECK = 30
TIMEOUT_PR_CREATE = 60
TIMEOUT_COMMIT_PUSH = 120

# --- Process limits (prlimit) ---

PRLIMIT_MAX_PROCESSES = 500
PRLIMIT_MAX_FILE_SIZE = 2_147_483_648  # 2 GB

# --- Output and truncation limits ---

MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB cap on in-memory agent output
READLINE_LIMIT = 1024 * 1024  # 1 MB per line (Claude streams large JSON events)

# Summary/display truncation
TRUNCATE_SUMMARY = 500
TRUNCATE_OUTPUT_TAIL = 2000
TRUNCATE_REVIEW_OUTPUT = 4000
TRUNCATE_VERIFY_OUTPUT = 3000
TRUNCATE_PR_TITLE = 60
TRUNCATE_COMMIT_MSG = 72
TRUNCATE_PROMPT_FOR_REVIEW = 2000
TRUNCATE_TRIAGE_BODY = 3000
TRUNCATE_BATCH_ISSUE_BODY = 1000
TRUNCATE_NAV_CONTEXT = 4000
TRUNCATE_ERROR_MESSAGE = 2000
TRUNCATE_LOG_LINE = 500
TRUNCATE_REASON = 200
TRUNCATE_BRANCH_SLUG = 40

# Navigation context
NAV_MAX_FILES = 15
NAV_MAX_SYMBOLS_PER_FILE = 5
NAV_MAX_EDGES = 20

# --- Agent sandbox ---

# Environment variables stripped from agent subprocess (secrets that must not leak)
SENSITIVE_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "BACKPORCHER_DASHBOARD_PASSWORD",
        "BACKPORCHER_WEBHOOK_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "OPENAI_API_KEY",
    }
)

# Credential file permissions (octal)
CREDENTIAL_FILE_MODE = 0o600

# --- Diff limits ---

MAX_PR_DIFF_CHARS = 15_000


def prlimit_args() -> list[str]:
    """Return the standard prlimit arguments for sandboxed agent subprocesses."""
    return [
        "prlimit",
        f"--nproc={PRLIMIT_MAX_PROCESSES}",
        f"--fsize={PRLIMIT_MAX_FILE_SIZE}",
        "--",
    ]
