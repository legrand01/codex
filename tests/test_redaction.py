"""
Tests for the secrets redaction service.

Covers: password formats, PostgreSQL connection strings, API keys,
Base64 tokens, bearer tokens, mixed content, no-secret content,
edge cases, and failure handling.

Requirements: 10.3, 10.6
"""

import re
from unittest.mock import patch

from backend.services.redaction import (
    REDACTED_PLACEHOLDER,
    redact_secrets,
    redact_with_retry,
    safe_redact,
)


class TestRedactPasswords:
    """Test redaction of various password formats."""

    def test_password_equals_sign(self):
        content = "config: password=secret123 done"
        result = redact_secrets(content)
        assert "secret123" not in result
        assert REDACTED_PLACEHOLDER in result
        assert "config:" in result
        assert "done" in result

    def test_password_with_single_quotes(self):
        content = "PASSWORD='mypass123'"
        result = redact_secrets(content)
        assert "mypass123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_password_with_double_quotes(self):
        content = 'password="super_secret_pw"'
        result = redact_secrets(content)
        assert "super_secret_pw" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_password_with_colon(self):
        content = "password: mysecretvalue"
        result = redact_secrets(content)
        assert "mysecretvalue" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_password_case_insensitive(self):
        content = "Password=CaseSensitive123"
        result = redact_secrets(content)
        assert "CaseSensitive123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_password_with_spaces_around_equals(self):
        content = "password = myvalue123"
        result = redact_secrets(content)
        assert "myvalue123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_multiple_passwords(self):
        content = "password=first password=second"
        result = redact_secrets(content)
        assert "first" not in result
        assert "second" not in result
        assert result.count(REDACTED_PLACEHOLDER) >= 2


class TestRedactConnectionStrings:
    """Test redaction of PostgreSQL and generic connection strings."""

    def test_postgresql_connection_string(self):
        content = "connecting to postgresql://admin:p4ssw0rd@db.example.com:5432/mydb"
        result = redact_secrets(content)
        assert "admin" not in result
        assert "p4ssw0rd" not in result
        assert REDACTED_PLACEHOLDER in result
        # Host portion after @ should still be visible
        assert "db.example.com" in result

    def test_postgresql_connection_string_simple(self):
        content = "postgresql://user:pass@localhost/db"
        result = redact_secrets(content)
        assert "user:pass" not in result
        assert REDACTED_PLACEHOLDER in result
        assert "localhost" in result

    def test_postgresql_uppercase(self):
        content = "POSTGRESQL://dbuser:secret@host:5432/testdb"
        result = redact_secrets(content)
        assert "dbuser" not in result
        assert "secret" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_generic_connection_string(self):
        content = "mysql://root:password@mysql-server:3306/app"
        result = redact_secrets(content)
        assert "root:password" not in result
        assert REDACTED_PLACEHOLDER in result


class TestRedactAPIKeys:
    """Test redaction of API keys with various prefixes."""

    def test_sk_prefix_api_key(self):
        content = "using key sk-abcdefghijklmnopqrstuvwxyz1234"
        result = redact_secrets(content)
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in result
        assert REDACTED_PLACEHOLDER in result
        assert "using key" in result

    def test_pk_prefix_api_key(self):
        content = "api-keyabcdefghijklmnopqrstuvwxyz"
        result = redact_secrets(content)
        assert "api-keyabcdefghijklmnopqrstuvwxyz" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_api_prefix_key(self):
        content = "api-key123456789012345678901"
        result = redact_secrets(content)
        assert "api-key123456789012345678901" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_api_underscore_key(self):
        content = "api_test_abcdefghijklmnopqrst"
        result = redact_secrets(content)
        assert "api_test_abcdefghijklmnopqrst" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_api_key_header(self):
        content = "x-api-key: example-api-token-12345678901234567890"
        result = redact_secrets(content)
        assert "example-api-token-12345678901234567890" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_api_key_header_equals(self):
        content = "api_key=my-secret-api-key-value"
        result = redact_secrets(content)
        assert "my-secret-api-key-value" not in result
        assert REDACTED_PLACEHOLDER in result


class TestRedactBase64Tokens:
    """Test redaction of Base64-encoded tokens (40+ characters)."""

    def test_base64_token(self):
        token = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODk="
        content = f"token: {token}"
        result = redact_secrets(content)
        assert token not in result
        assert REDACTED_PLACEHOLDER in result

    def test_long_base64_token(self):
        token = "A" * 60
        content = f"Authorization: {token}"
        result = redact_secrets(content)
        assert token not in result
        assert REDACTED_PLACEHOLDER in result

    def test_base64_with_padding(self):
        token = "dGhpcyBpcyBhIHRlc3QgdG9rZW4gdGhhdCBpcyBsb25nIGVub3VnaA=="
        content = f"data={token}"
        result = redact_secrets(content)
        assert token not in result
        assert REDACTED_PLACEHOLDER in result

    def test_short_base64_not_redacted(self):
        """Strings shorter than 40 chars of base64 should not be redacted."""
        short_token = "YWJj"  # Only 4 chars
        content = f"short: {short_token}"
        result = redact_secrets(content)
        assert short_token in result


class TestRedactBearerTokens:
    """Test redaction of Bearer tokens."""

    def test_bearer_token(self):
        content = (
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc"
        )
        result = redact_secrets(content)
        assert "eyJhbGciOiJ" not in result
        assert REDACTED_PLACEHOLDER in result
        assert "Bearer" in result

    def test_bearer_token_lowercase(self):
        content = "bearer mytoken12345"
        result = redact_secrets(content)
        assert "mytoken12345" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_bearer_token_in_header(self):
        content = "Header: Bearer sk-proj-abcdefghijklmnop"
        result = redact_secrets(content)
        assert "sk-proj-abcdefghijklmnop" not in result
        assert REDACTED_PLACEHOLDER in result


class TestMixedContent:
    """Test redaction in content with secrets embedded in normal text."""

    def test_mixed_content_preserves_structure(self):
        content = (
            "Connecting to database...\n"
            "Host: postgresql://admin:secret@db.prod.com:5432/main\n"
            "Status: connected\n"
            "API key: sk-prod_abcdefghijklmnopqrstuvwx\n"
            "Ready to process."
        )
        result = redact_secrets(content)
        # Non-secret structure preserved
        assert "Connecting to database..." in result
        assert "Status: connected" in result
        assert "Ready to process." in result
        # Secrets removed
        assert "admin:secret" not in result
        assert "sk-prod_abcdefghijklmnopqrstuvwx" not in result

    def test_multiple_secrets_in_one_line(self):
        content = "password=abc123 postgresql://u:p@h/d"
        result = redact_secrets(content)
        assert "abc123" not in result
        assert "u:p" not in result

    def test_log_entry_with_embedded_secret(self):
        content = (
            "[2024-01-15T10:30:00Z] INFO: Executing query on host-001. "
            "Config: password=db_secret_pass123. Status: success."
        )
        result = redact_secrets(content)
        assert "db_secret_pass123" not in result
        assert "[2024-01-15T10:30:00Z]" in result
        assert "Status: success." in result


class TestNoSecrets:
    """Test that content without secrets passes through unchanged."""

    def test_plain_text(self):
        content = "This is a normal log entry with no secrets."
        result = redact_secrets(content)
        assert result == content

    def test_sql_query_without_secrets(self):
        content = "SELECT * FROM hosts WHERE health_status = 'healthy'"
        result = redact_secrets(content)
        assert result == content

    def test_json_without_secrets(self):
        content = '{"host": "db-001", "status": "connected", "version": "15.4"}'
        result = redact_secrets(content)
        assert result == content

    def test_numbers_and_metrics(self):
        content = "CPU: 45.2%, Memory: 78.1%, Disk IO: 1234 ops/s"
        result = redact_secrets(content)
        assert result == content


class TestEdgeCases:
    """Test edge cases for the redaction function."""

    def test_empty_string(self):
        result = redact_secrets("")
        assert result == ""

    def test_very_long_string(self):
        """Very long strings without secret patterns pass through unchanged."""
        # Use characters outside the base64 alphabet to avoid triggering base64 detection
        content = "normal log entry. " * 5000
        result = redact_secrets(content)
        assert result == content

    def test_string_with_only_whitespace(self):
        content = "   \n\t\n   "
        result = redact_secrets(content)
        assert result == content

    def test_special_characters(self):
        content = "!@#$%^&*()_+-=[]{}|;:',.<>?/"
        result = redact_secrets(content)
        assert result == content

    def test_unicode_content(self):
        content = "Database status: OK. Host: prod-db-01."
        result = redact_secrets(content)
        assert result == content


class TestSafeRedact:
    """Test the safe_redact wrapper function."""

    def test_successful_redaction(self):
        content = "password=secret123"
        result, success = safe_redact(content)
        assert success is True
        assert "secret123" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_no_secrets_success(self):
        content = "normal text"
        result, success = safe_redact(content)
        assert success is True
        assert result == content

    def test_empty_string_success(self):
        result, success = safe_redact("")
        assert success is True
        assert result == ""

    def test_handles_type_error(self):
        """Test that TypeError in regex processing is handled gracefully."""
        with patch(
            "backend.services.redaction.redact_secrets",
            side_effect=TypeError("invalid type"),
        ):
            result, success = safe_redact("password=test")
            assert success is False
            assert result == "password=test"

    def test_handles_regex_error(self):
        """Test that re.error is handled gracefully."""
        with patch(
            "backend.services.redaction.redact_secrets",
            side_effect=re.error("regex failure"),
        ):
            result, success = safe_redact("some content")
            assert success is False
            assert result == "some content"


class TestRedactWithRetry:
    """Test the retry logic for redaction failures."""

    def test_success_on_first_attempt(self):
        content = "password=mysecret"
        result, success = redact_with_retry(content)
        assert success is True
        assert "mysecret" not in result

    def test_success_on_retry(self):
        """Test that retry succeeds after initial failure."""
        call_count = {"n": 0}
        original_safe_redact = safe_redact

        def mock_safe_redact(content):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First attempt fails
                return (content, False)
            # Second attempt succeeds
            return original_safe_redact(content)

        with patch(
            "backend.services.redaction.safe_redact",
            side_effect=mock_safe_redact,
        ):
            result, success = redact_with_retry("password=secret")
            assert success is True

    def test_failure_after_all_retries(self):
        """Test that all retries exhausted returns failure."""
        with patch(
            "backend.services.redaction.safe_redact",
            return_value=("password=secret", False),
        ):
            result, success = redact_with_retry("password=secret", max_retries=2)
            assert success is False
            assert result == "password=secret"

    def test_custom_max_retries(self):
        """Test that max_retries parameter is respected."""
        call_count = {"n": 0}

        def mock_safe_redact(content):
            call_count["n"] += 1
            return (content, False)

        with patch(
            "backend.services.redaction.safe_redact",
            side_effect=mock_safe_redact,
        ):
            redact_with_retry("content", max_retries=3)
            assert call_count["n"] == 3
