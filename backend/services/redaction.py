"""
Secrets redaction service for audit logging.

Detects and replaces sensitive content (passwords, connection strings,
API keys, tokens, certificates) with a fixed placeholder to ensure
audit log entries never contain plaintext secrets.

Requirements: 10.3, 10.6
"""

import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Fixed placeholder used to replace all detected secrets
REDACTED_PLACEHOLDER = "[REDACTED]"

# Regex patterns for detecting secrets in content.
# Order matters: more specific patterns should come before general ones
# to avoid partial matches.
SECRET_PATTERNS: List[re.Pattern[str]] = [
    # Bearer tokens (e.g., "Bearer eyJhbGciOiJ...")
    re.compile(r"(Bearer\s+)\S+", re.IGNORECASE),
    # PostgreSQL connection strings with credentials (e.g., postgresql://user:pass@host/db)
    re.compile(r"postgresql://[^\s@]+@", re.IGNORECASE),
    # Generic connection strings with credentials (e.g., mysql://user:pass@host)
    re.compile(r"\w+://[^\s@]+@", re.IGNORECASE),
    # Password assignments in various formats:
    #   password=secret, PASSWORD='mypass', password: "value", etc.
    re.compile(r"(password\s*[=:]\s*)[\"']?[^\s\"',;]+[\"']?", re.IGNORECASE),
    # API keys with common prefixes (sk-, pk_, api-, api_)
    # Allows underscores/hyphens within the key body (e.g., sk-proj_abc123...)
    re.compile(r"(sk|pk|api)[-_][A-Za-z0-9_-]{20,}"),
    # API key header values (e.g., "api-key: some-value", "x-api-key: value")
    re.compile(r"((?:x-)?api[-_]key\s*[:=]\s*)\S+", re.IGNORECASE),
    # Base64-encoded tokens (40+ chars of base64 alphabet)
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
]


def redact_secrets(content: str) -> str:
    """
    Detect and replace secrets in the given content string.

    Scans the content for patterns matching known secret formats
    (passwords, connection strings, Base64 tokens, API keys, bearer tokens)
    and replaces detected secrets with the fixed placeholder '[REDACTED]'.

    Args:
        content: The string content to scan and redact.

    Returns:
        The content with all detected secrets replaced by '[REDACTED]'.
    """
    if not content:
        return content

    result = content

    # Bearer tokens: keep "Bearer " prefix, redact the token value
    result = SECRET_PATTERNS[0].sub(r"\1" + REDACTED_PLACEHOLDER, result)

    # PostgreSQL connection strings: redact credentials portion
    result = SECRET_PATTERNS[1].sub(REDACTED_PLACEHOLDER + "@", result)

    # Generic connection strings with credentials
    result = SECRET_PATTERNS[2].sub(REDACTED_PLACEHOLDER + "@", result)

    # Password assignments: keep the "password=" prefix, redact the value
    result = SECRET_PATTERNS[3].sub(r"\1" + REDACTED_PLACEHOLDER, result)

    # API keys (sk-xxx, pk_xxx, api-xxx)
    result = SECRET_PATTERNS[4].sub(REDACTED_PLACEHOLDER, result)

    # API key headers: keep the header name, redact the value
    result = SECRET_PATTERNS[5].sub(r"\1" + REDACTED_PLACEHOLDER, result)

    # Base64 tokens (40+ chars)
    result = SECRET_PATTERNS[6].sub(REDACTED_PLACEHOLDER, result)

    return result


def safe_redact(content: str) -> Tuple[str, bool]:
    """
    Attempt to redact secrets with failure handling.

    Provides a safe wrapper around redact_secrets that catches any
    regex or processing errors. On failure, returns the original content
    with a success=False flag so the caller can implement
    redaction-failure handling (block write, log alert, retry).

    Args:
        content: The string content to scan and redact.

    Returns:
        A tuple of (redacted_content, success).
        On success: (redacted_string, True)
        On failure: (original_content, False) - caller should block write and retry.
    """
    try:
        redacted = redact_secrets(content)
        return (redacted, True)
    except (re.error, TypeError, MemoryError, RecursionError) as e:
        logger.error(
            "Redaction failure: %s. Blocking write and alerting. "
            "Content will not be persisted until redaction succeeds.",
            str(e),
        )
        return (content, False)


def redact_with_retry(content: str, max_retries: int = 2) -> Tuple[str, bool]:
    """
    Attempt redaction with retry logic before persisting.

    Implements the redaction-failure handling specified in Requirement 10.6:
    if redaction fails, block the write, log an alert, and retry before
    persisting the entry.

    Args:
        content: The string content to scan and redact.
        max_retries: Maximum number of retry attempts (default: 2).

    Returns:
        A tuple of (redacted_content, success).
        On success after any attempt: (redacted_string, True)
        On failure after all retries: (original_content, False)
            - Caller MUST block the audit log write.
    """
    for attempt in range(1, max_retries + 1):
        redacted, success = safe_redact(content)
        if success:
            return (redacted, True)
        logger.warning(
            "Redaction attempt %d/%d failed. Retrying...",
            attempt,
            max_retries,
        )

    logger.critical(
        "Redaction failed after %d retries. Blocking audit log write. "
        "Manual intervention required.",
        max_retries,
    )
    return (content, False)
