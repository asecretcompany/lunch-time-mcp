import asyncio

import pytest

from lunch_time_mcp.main import (
    MessageResponse,
    AllowlistConfig,
    _parse_receive_output,
    _filter_by_allowlist,
    _validate_recipient,
    _validate_message,
    _validate_timeout,
    _check_allowlist_recipient,
    _check_allowlist_group,
    _sanitize,
    config,
    AllowlistError,
    ValidationError,
    MAX_MESSAGE_LENGTH,
    MIN_TIMEOUT,
    MAX_TIMEOUT,
)


# --- Test data ---

RECV_SINGLE = """
{"envelope":{"source":"+11234567890","sourceNumber":"+11234567890","sourceUuid":"bob-uuid","sourceName":"Bob Sagat","sourceDevice":4,"timestamp":1744185565466,"dataMessage":{"timestamp":1744185565466,"message":"yo","expiresInSeconds":0,"viewOnce":false}},"account":"+15551234567"}
"""

RECV_MULTIPLE = """
{"envelope":{"source":"+11234567890","sourceNumber":"+11234567890","sourceUuid":"bob-uuid","sourceName":"Bob Sagat","sourceDevice":4,"timestamp":1744185564802,"receiptMessage":{"when":1744185564802,"isDelivery":true,"isRead":false,"isViewed":false,"timestamps":[1744185564802]}},"account":"+15551234567"}
{"envelope":{"source":"+11234567890","sourceNumber":"+11234567890","sourceUuid":"bob-uuid","sourceName":"Bob Sagat","sourceDevice":4,"timestamp":1744185565466,"dataMessage":{"timestamp":1744185565466,"message":"first message","expiresInSeconds":0,"viewOnce":false}},"account":"+15551234567"}
{"envelope":{"source":"+19876543210","sourceNumber":"+19876543210","sourceUuid":"alice-uuid","sourceName":"Alice Smith","sourceDevice":1,"timestamp":1744185566000,"dataMessage":{"timestamp":1744185566000,"message":"second message","expiresInSeconds":0,"viewOnce":false}},"account":"+15551234567"}
"""

RECV_GROUP = """
{"envelope":{"source":"+11234567890","sourceNumber":"+11234567890","sourceUuid":"bob-uuid","sourceName":"Bob Sagat","sourceDevice":4,"timestamp":1744185567000,"dataMessage":{"timestamp":1744185567000,"message":"hello group!","expiresInSeconds":0,"viewOnce":false,"groupInfo":{"groupId":"ExAmPlEgRoUpIdWhIcHiSbAsE64eNcOdEd00000000000=","type":"DELIVER"}}},"account":"+15551234567"}
"""

RECV_EMPTY = ""

RECV_NO_BODY = """
{"envelope":{"source":"+11234567890","sourceNumber":"+11234567890","sourceUuid":"bob-uuid","sourceName":"Bob Sagat","sourceDevice":4,"timestamp":1744185564802,"receiptMessage":{"when":1744185564802,"isDelivery":true,"isRead":false,"isViewed":false,"timestamps":[1744185564802]}},"account":"+15551234567"}
"""


# --- Parsing tests ---


class TestParseReceiveOutput:
    def test_parse_direct_message(self):
        result = asyncio.run(_parse_receive_output(RECV_SINGLE))
        assert len(result) == 1
        assert result[0] == MessageResponse(
            message="yo", sender_id="bob-uuid", group_name=None
        )

    def test_parse_multiple_messages(self):
        result = asyncio.run(_parse_receive_output(RECV_MULTIPLE))
        assert len(result) == 2
        assert result[0].message == "first message"
        assert result[0].sender_id == "bob-uuid"
        assert result[1].message == "second message"
        assert result[1].sender_id == "alice-uuid"

    def test_parse_group_message(self):
        result = asyncio.run(_parse_receive_output(RECV_GROUP))
        assert len(result) == 1
        assert result[0].message == "hello group!"
        assert result[0].sender_id == "bob-uuid"
        assert result[0].group_name == "ExAmPlEgRoUpIdWhIcHiSbAsE64eNcOdEd00000000000="

    def test_parse_empty_output(self):
        result = asyncio.run(_parse_receive_output(RECV_EMPTY))
        assert result == []

    def test_parse_no_body_messages(self):
        result = asyncio.run(_parse_receive_output(RECV_NO_BODY))
        assert result == []


# --- Validation tests ---


class TestValidateRecipient:
    def test_valid_e164(self):
        assert _validate_recipient("+11234567890") == "+11234567890"

    def test_valid_e164_short(self):
        assert _validate_recipient("+44123") == "+44123"

    def test_valid_uuid(self):
        uuid = "abcd1234-ef56-7890-abcd-ef1234567890"
        assert _validate_recipient(uuid) == uuid

    def test_valid_username(self):
        assert _validate_recipient("u:alice.smith") == "u:alice.smith"

    def test_invalid_empty(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            _validate_recipient("")

    def test_invalid_no_plus(self):
        with pytest.raises(ValidationError, match="Invalid recipient"):
            _validate_recipient("11234567890")

    def test_invalid_letters(self):
        with pytest.raises(ValidationError, match="Invalid recipient"):
            _validate_recipient("notanumber")

    def test_strips_whitespace(self):
        assert _validate_recipient("  +11234567890  ") == "+11234567890"


class TestValidateMessage:
    def test_valid_message(self):
        assert _validate_message("Hello!") == "Hello!"

    def test_message_at_limit(self):
        msg = "a" * MAX_MESSAGE_LENGTH
        assert _validate_message(msg) == msg

    def test_message_over_limit(self):
        msg = "a" * (MAX_MESSAGE_LENGTH + 1)
        with pytest.raises(ValidationError, match="too long"):
            _validate_message(msg)


class TestValidateTimeout:
    def test_valid_timeout(self):
        assert _validate_timeout(30.0) == 30

    def test_clamp_low(self):
        assert _validate_timeout(0.0) == MIN_TIMEOUT

    def test_clamp_high(self):
        assert _validate_timeout(999.0) == MAX_TIMEOUT

    def test_negative(self):
        assert _validate_timeout(-5.0) == MIN_TIMEOUT


# --- Allowlist tests ---


class TestAllowlist:
    def setup_method(self):
        """Save and restore config state."""
        self._orig_allowlist = config.allowlist

    def teardown_method(self):
        config.allowlist = self._orig_allowlist

    def test_allowlist_blocks_unknown_recipient(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients={"+19999999999"},
            allowed_groups=set(),
        )
        with pytest.raises(AllowlistError, match="not in the allowlist"):
            _check_allowlist_recipient("+10000000000")

    def test_allowlist_allows_known_recipient(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients={"+19999999999"},
            allowed_groups=set(),
        )
        # Should not raise
        _check_allowlist_recipient("+19999999999")

    def test_allowlist_blocks_unknown_group(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients=set(),
            allowed_groups={"Engineering Team"},
        )
        with pytest.raises(AllowlistError, match="not in the allowlist"):
            _check_allowlist_group("Secret Group")

    def test_allowlist_allows_known_group(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients=set(),
            allowed_groups={"Engineering Team"},
        )
        # Should not raise
        _check_allowlist_group("Engineering Team")

    def test_empty_allowlist_denies_all_recipients(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients=set(),
            allowed_groups=set(),
        )
        with pytest.raises(AllowlistError, match="No recipients configured"):
            _check_allowlist_recipient("+11234567890")

    def test_empty_allowlist_denies_all_groups(self):
        config.allowlist = AllowlistConfig(
            allowed_recipients=set(),
            allowed_groups=set(),
        )
        with pytest.raises(AllowlistError, match="No groups configured"):
            _check_allowlist_group("Any Group")

    def test_allowlist_with_uuid(self):
        uuid = "abcd1234-ef56-7890-abcd-ef1234567890"
        config.allowlist = AllowlistConfig(
            allowed_recipients={uuid},
            allowed_groups=set(),
        )
        # Should not raise
        _check_allowlist_recipient(uuid)


# --- Inbound filter tests ---


class TestInboundFilter:
    def setup_method(self):
        self._orig_allowlist = config.allowlist

    def teardown_method(self):
        config.allowlist = self._orig_allowlist

    def test_filter_drops_unknown_sender(self):
        config.allowlist = AllowlistConfig(
            allowed_senders={"+19999999999"},
        )
        messages = [
            MessageResponse(message="hi", sender_id="+10000000000"),
            MessageResponse(message="hello", sender_id="+19999999999"),
        ]
        result = _filter_by_allowlist(messages)
        assert len(result) == 1
        assert result[0].sender_id == "+19999999999"

    def test_filter_drops_unknown_group(self):
        config.allowlist = AllowlistConfig(
            allowed_receive_groups={"Engineering Team"},
        )
        messages = [
            MessageResponse(message="hi", sender_id="+1111", group_name="Secret Group"),
            MessageResponse(
                message="yo", sender_id="+2222", group_name="Engineering Team"
            ),
        ]
        result = _filter_by_allowlist(messages)
        assert len(result) == 1
        assert result[0].group_name == "Engineering Team"

    def test_filter_allows_all_when_no_inbound_config(self):
        config.allowlist = AllowlistConfig()  # No inbound filters
        messages = [
            MessageResponse(message="hi", sender_id="+1111"),
            MessageResponse(message="yo", sender_id="+2222"),
        ]
        result = _filter_by_allowlist(messages)
        assert len(result) == 2

    def test_filter_combined_sender_and_group(self):
        config.allowlist = AllowlistConfig(
            allowed_senders={"+19999999999"},
            allowed_receive_groups={"Engineering Team"},
        )
        messages = [
            # Wrong sender, wrong group -> drop
            MessageResponse(message="a", sender_id="+10000000000", group_name="Random"),
            # Right sender, wrong group -> drop
            MessageResponse(message="b", sender_id="+19999999999", group_name="Random"),
            # Right sender, right group -> keep
            MessageResponse(
                message="c", sender_id="+19999999999", group_name="Engineering Team"
            ),
            # Right sender, no group (DM) -> keep
            MessageResponse(message="d", sender_id="+19999999999"),
        ]
        result = _filter_by_allowlist(messages)
        assert len(result) == 2
        assert result[0].message == "c"
        assert result[1].message == "d"

    def test_filter_with_empty_messages(self):
        config.allowlist = AllowlistConfig(
            allowed_senders={"+19999999999"},
        )
        result = _filter_by_allowlist([])
        assert result == []


# --- PII sanitization tests ---


class TestSanitize:
    def setup_method(self):
        self._orig_debug_pii = config.debug_pii

    def teardown_method(self):
        config.debug_pii = self._orig_debug_pii

    def test_sanitize_phone_masked(self):
        config.debug_pii = False
        result = _sanitize("+11234567890")
        assert "1234567890" not in result
        assert "***" in result

    def test_sanitize_phone_debug_mode(self):
        config.debug_pii = True
        result = _sanitize("+11234567890")
        assert result == "+11234567890"

    def test_sanitize_long_message_masked(self):
        config.debug_pii = False
        msg = "This is a sensitive message with details"
        result = _sanitize(msg)
        assert "[redacted]" in result

    def test_sanitize_long_message_debug_mode(self):
        config.debug_pii = True
        msg = "This is a sensitive message with details"
        result = _sanitize(msg)
        assert result == msg
