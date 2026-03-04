#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "mcp",
# ]
# ///
from mcp.server.fastmcp import FastMCP
from typing import Optional, Union, Any
import asyncio
import subprocess
import argparse
import json
from pathlib import Path
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import logging

# Set up logging with more detailed format
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize MCP server
mcp = FastMCP(name="signal-cli")
logger.info("Initialized FastMCP server for signal-cli")

# --- Validation constants ---
E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
USERNAME_PATTERN = re.compile(r"^u:[a-zA-Z0-9._-]+$")
MAX_MESSAGE_LENGTH = 10_000
MIN_TIMEOUT = 1
MAX_TIMEOUT = 300


@dataclass
class AllowlistConfig:
    """Allowlist configuration for restricting who the agent can contact AND
    who the agent will accept messages from.

    NOTE: For maximum security, prefer Signal UUIDs (ACI) over phone numbers
    in your allowlist. UUIDs are cryptographically bound to accounts and
    cannot be spoofed, unlike phone numbers which are susceptible to
    SIM-swapping and caller ID spoofing.

    Example config:
    {
        "allowed_recipients": ["abc12345-def6-7890-abcd-ef1234567890"],
        "allowed_groups": ["Engineering Team"],
        "allowed_senders": ["+11234567890", "abc12345-def6-7890-abcd-ef1234567890"],
        "allowed_receive_groups": ["Engineering Team"]
    }
    """

    allowed_recipients: set[str] = field(default_factory=set)
    allowed_groups: set[str] = field(default_factory=set)
    allowed_senders: set[str] = field(default_factory=set)
    allowed_receive_groups: set[str] = field(default_factory=set)


@dataclass
class SignalConfig:
    """Configuration for Signal CLI."""

    user_id: str = ""  # The user's Signal phone number
    transport: str = "sse"
    debug_pii: bool = False
    allowlist: AllowlistConfig = field(default_factory=AllowlistConfig)
    allowed_file_dirs: list[str] = field(default_factory=list)
    default_group: str = ""  # Default group for agent status updates
    db_path: Path | None = None  # SQLite inbox path (set when polling daemon is used)


@dataclass
class MessageResponse:
    """Structured result for received messages."""

    message: Optional[str] = None
    sender_id: Optional[str] = None
    group_name: Optional[str] = None
    error: Optional[str] = None


class SignalError(Exception):
    """Base exception for Signal-related errors."""

    pass


class SignalCLIError(SignalError):
    """Exception raised when signal-cli command fails."""

    pass


class AllowlistError(SignalError):
    """Exception raised when a recipient is not in the allowlist."""

    pass


class ValidationError(SignalError):
    """Exception raised for input validation failures."""

    pass


SuccessResponse = dict[str, str]
ErrorResponse = dict[str, str]

# Global config instance
config = SignalConfig()


# --- PII sanitization ---


def _sanitize(value: str) -> str:
    """Mask PII unless debug_pii is enabled.

    Phone numbers are masked as +1123***7890.
    Other values are truncated with ellipsis.
    """
    if config.debug_pii:
        return value

    if E164_PATTERN.match(value):
        # Mask middle digits of phone number
        if len(value) > 6:
            return value[:4] + "***" + value[-4:]
        return "***"

    # For message bodies and other sensitive strings, truncate
    if len(value) > 20:
        return value[:10] + "...[redacted]"
    return value


# --- Input validation ---


def _validate_recipient(recipient: str) -> str:
    """Validate that a recipient identifier is well-formed.

    Accepts E.164 phone numbers, Signal UUIDs, or u:username format.
    """
    recipient = recipient.strip()
    if not recipient:
        raise ValidationError("Recipient cannot be empty")

    if E164_PATTERN.match(recipient):
        return recipient
    if UUID_PATTERN.match(recipient):
        return recipient
    if USERNAME_PATTERN.match(recipient):
        return recipient

    raise ValidationError(
        f"Invalid recipient format: {_sanitize(recipient)}. "
        "Must be E.164 phone number (+1234567890), "
        "Signal UUID, or username (u:name)."
    )


def _validate_message(message: str) -> str:
    """Validate message content."""
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValidationError(
            f"Message too long: {len(message)} chars (max {MAX_MESSAGE_LENGTH})"
        )
    return message


def _validate_timeout(timeout: float) -> int:
    """Validate and clamp timeout to safe bounds."""
    clamped = max(MIN_TIMEOUT, min(MAX_TIMEOUT, int(timeout)))
    if int(timeout) != clamped:
        logger.info(f"Timeout clamped from {int(timeout)}s to {clamped}s")
    return clamped


def _check_allowlist_recipient(recipient: str) -> None:
    """Check if a recipient is in the allowlist. Raises AllowlistError if not."""
    if not config.allowlist.allowed_recipients:
        raise AllowlistError(
            "No recipients configured in allowlist. "
            "Provide an --allowlist config file with allowed_recipients."
        )
    if recipient not in config.allowlist.allowed_recipients:
        raise AllowlistError(
            f"Recipient {_sanitize(recipient)} is not in the allowlist."
        )


def _check_allowlist_group(group_name: str) -> None:
    """Check if a group is in the allowlist. Raises AllowlistError if not."""
    if not config.allowlist.allowed_groups:
        raise AllowlistError(
            "No groups configured in allowlist. "
            "Provide an --allowlist config file with allowed_groups."
        )
    if group_name not in config.allowlist.allowed_groups:
        raise AllowlistError(f"Group {_sanitize(group_name)} is not in the allowlist.")


def _validate_file_path(file_path: str) -> Path:
    """Validate that a file path exists, is readable, and is within allowed directories."""
    path = Path(file_path).resolve()

    if not path.exists():
        raise ValidationError(f"File does not exist: {file_path}")
    if not path.is_file():
        raise ValidationError(f"Path is not a file: {file_path}")
    if not os.access(path, os.R_OK):
        raise ValidationError(f"File is not readable: {file_path}")

    # Check allowed directories
    if config.allowed_file_dirs:
        allowed = False
        for allowed_dir in config.allowed_file_dirs:
            allowed_path = Path(allowed_dir).resolve()
            try:
                path.relative_to(allowed_path)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise ValidationError(
                f"File {file_path} is not within any allowed directory. "
                f"Allowed: {config.allowed_file_dirs}"
            )

    return path


# --- Core signal-cli execution ---

# Lock-related error patterns in signal-cli stderr
_LOCK_ERROR_PATTERNS = ("locked", "lock", "could not open", "database is locked")


def _is_lock_error(stderr: str) -> bool:
    """Check if a signal-cli failure is due to config directory lock contention."""
    lower = stderr.lower()
    return any(pattern in lower for pattern in _LOCK_ERROR_PATTERNS)


async def _run_signal_cli(
    args: list[str], max_retries: int = 3
) -> tuple[str, str, int | None]:
    """Run a signal-cli command, retrying on config lock contention.

    signal-cli uses an exclusive file lock on its data directory. When the
    polling daemon is mid-receive, concurrent send commands will fail.
    This wrapper detects lock errors and retries with exponential backoff.

    Args:
        args: List of arguments to pass to signal-cli (excluding 'signal-cli' itself).
        max_retries: Maximum number of retries on lock contention (default: 3).
    """
    stdout_str, stderr_str, returncode = "", "", None

    for attempt in range(max_retries + 1):
        stdout_str, stderr_str, returncode = await _run_signal_cli_once(args)

        # Success or non-lock error — return immediately
        if returncode == 0 or not _is_lock_error(stderr_str):
            return stdout_str, stderr_str, returncode

        # Lock error — retry with exponential backoff
        if attempt < max_retries:
            wait = 2 ** attempt  # 1s, 2s, 4s
            logger.warning(
                f"signal-cli lock contention (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {wait}s"
            )
            await asyncio.sleep(wait)

    logger.error(f"signal-cli lock contention persisted after {max_retries} retries")
    return stdout_str, stderr_str, returncode


async def _run_signal_cli_once(args: list[str]) -> tuple[str, str, int | None]:
    """Run a single signal-cli command using exec (no shell interpretation).

    Args:
        args: List of arguments to pass to signal-cli (excluding 'signal-cli' itself).
    """
    logger.debug(f"Executing signal-cli with {len(args)} args")
    try:
        process = await asyncio.create_subprocess_exec(
            "signal-cli",
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()
        stdout_str, stderr_str = stdout.decode(), stderr.decode()

        if process.returncode != 0:
            logger.warning(
                f"signal-cli command failed with return code {process.returncode}"
            )
            logger.warning(f"stderr: {stderr_str}")
        else:
            logger.debug("signal-cli command completed successfully")

        return stdout_str, stderr_str, process.returncode

    except Exception as e:
        logger.error(f"Error running signal-cli command: {str(e)}", exc_info=True)
        raise SignalCLIError(f"Failed to run signal-cli: {str(e)}")


async def _get_group_id(group_name: str) -> Optional[str]:
    """Look up a group by display name and return its base64 group ID.

    Parses signal-cli listGroups output which has lines like:
        Id: ExAmPlEgRoUpIdWhIcHiSbAsE64eNcOdEd00000000000=
        Name: ASC.is
    """
    logger.info(f"Looking up group with name: {_sanitize(group_name)}")

    args = ["-u", config.user_id, "listGroups"]
    stdout, stderr, return_code = await _run_signal_cli(args)

    if return_code != 0:
        logger.error(f"Error listing groups: {stderr}")
        return None

    # Parse Id: and Name: pairs from the output
    for line in stdout.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        current_id = None
        extracted_name = None

        # Line format:
        # Id: ExAmPlEgRoUpIdWhIcHiSbAsE64eNcOdEd00000000000= Name: ASC.is  Active: true Blocked: false
        if "Id: " in stripped and "Name: " in stripped:
            id_start = stripped.index("Id: ") + 4
            name_start = stripped.index("Name: ")
            current_id = stripped[id_start:name_start].strip()

            name_val_start = name_start + 6
            # Find the end of Name: (usually followed by "Active:")
            if "Active:" in stripped:
                active_start = stripped.index("Active:")
                extracted_name = stripped[name_val_start:active_start].strip()
            else:
                extracted_name = stripped[name_val_start:].strip()

            if extracted_name == group_name and current_id:
                logger.info(
                    f"Found group: {_sanitize(group_name)} "
                    f"-> id: {_sanitize(current_id)}"
                )
                return current_id

    logger.error(f"Could not find group with name: {_sanitize(group_name)}")
    return None


async def _send_message(message: str, target: str, is_group: bool = False) -> bool:
    """Send a message to either a user or group."""
    target_type = "group" if is_group else "user"
    logger.info(f"Sending message to {target_type}: {_sanitize(target)}")

    args = ["-u", config.user_id, "send"]
    if is_group:
        args.extend(["-g", target])
    else:
        args.append(target)
    args.extend(["-m", message])

    try:
        _, stderr, return_code = await _run_signal_cli(args)

        if return_code == 0:
            logger.info(
                f"Successfully sent message to {target_type}: {_sanitize(target)}"
            )
            return True
        else:
            logger.error(f"Error sending message to {target_type}: {stderr}")
            return False
    except SignalCLIError as e:
        logger.error(f"Failed to send message to {target_type}: {str(e)}")
        return False


async def _parse_receive_output(
    stdout: str,
) -> list[MessageResponse]:
    """Parse the JSON output of signal-cli receive command.

    Returns all parsed messages.
    """
    logger.debug("Parsing received JSON message output")

    lines = stdout.split("\n")
    messages: list[MessageResponse] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON line: {line}")
            continue

        env = data.get("envelope", {})

        # Extract the most secure sender identifier, preferring UUID over phone number.
        sender_uuid = env.get("sourceUuid")
        sender_number = env.get("sourceNumber") or env.get("source")

        sender = sender_uuid if sender_uuid else sender_number

        # Not all envelopes contain a dataMessage with a body
        data_message = env.get("dataMessage", {})
        message_body = data_message.get("message")

        if not sender or not message_body:
            continue

        # Check for group info
        group_id = data_message.get("groupInfo", {}).get("groupId")

        logger.info(
            f"Parsed message from {_sanitize(sender)}"
            + (f" in group {_sanitize(group_id)}" if group_id else "")
        )

        messages.append(
            MessageResponse(message=message_body, sender_id=sender, group_name=group_id)
        )

    if not messages:
        logger.warning("No messages parsed from output")

    return messages


# --- Inbound message filtering ---


def _filter_by_allowlist(messages: list[MessageResponse]) -> list[MessageResponse]:
    """Filter received messages to only include those from allowed senders/groups.

    If allowed_senders is configured, only messages from those senders pass.
    If allowed_receive_groups is configured, only group messages from those groups pass.
    If neither is configured, all messages pass through (no inbound filtering).
    """
    has_sender_filter = bool(config.allowlist.allowed_senders)
    has_group_filter = bool(config.allowlist.allowed_receive_groups)

    # If no inbound filters configured, pass everything through
    if not has_sender_filter and not has_group_filter:
        return messages

    filtered: list[MessageResponse] = []

    for msg in messages:
        # Check sender allowlist
        if has_sender_filter and msg.sender_id:
            if msg.sender_id not in config.allowlist.allowed_senders:
                logger.warning(
                    f"Dropping message from non-allowlisted sender: "
                    f"{_sanitize(msg.sender_id)}"
                )
                continue

        # Check group allowlist (only for group messages)
        if has_group_filter and msg.group_name:
            if msg.group_name not in config.allowlist.allowed_receive_groups:
                logger.warning(
                    f"Dropping message from non-allowlisted group: "
                    f"{_sanitize(msg.group_name)}"
                )
                continue

        filtered.append(msg)

    return filtered


# --- MCP Resources ---


@mcp.resource("signal://config")
def get_signal_config() -> str:
    """Returns the current Signal MCP server configuration for agent reference."""
    info = {
        "default_group": config.default_group or "(not configured)",
        "allowed_groups": sorted(config.allowlist.allowed_groups),
        "allowed_recipients": sorted(config.allowlist.allowed_recipients),
        "instructions": (
            "Use send_message_to_group with the default_group to post status updates. "
            "Use receive_message to poll for engineer feedback. "
            "Act on feedback by continuing your work as requested."
        ),
    }
    return json.dumps(info, indent=2)


# --- MCP Tools ---


@mcp.tool()
async def send_message_to_user(
    message: str, user_id: str
) -> Union[SuccessResponse, ErrorResponse]:
    """Send a message to a specific user using signal-cli."""
    logger.info(f"Tool called: send_message_to_user for user {_sanitize(user_id)}")

    try:
        # Validate inputs
        user_id = _validate_recipient(user_id)
        message = _validate_message(message)

        # Check allowlist
        _check_allowlist_recipient(user_id)

        success = await _send_message(message, user_id, is_group=False)
        if success:
            logger.info(f"Successfully sent message to user {_sanitize(user_id)}")
            return {"message": "Message sent successfully"}
        logger.error(f"Failed to send message to user {_sanitize(user_id)}")
        return {"error": "Failed to send message"}
    except (AllowlistError, ValidationError) as e:
        logger.warning(f"Blocked send_message_to_user: {str(e)}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_message_to_user: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send_message_to_group(
    message: str, group_id: str = ""
) -> Union[SuccessResponse, ErrorResponse]:
    """Send a message to a Signal group. If group_id is omitted, sends to the default group."""
    # Use default group if none specified
    if not group_id.strip():
        if config.default_group:
            group_id = config.default_group
            logger.info(f"Using default group: {_sanitize(group_id)}")
        else:
            return {"error": "No group_id provided and no default group configured."}

    logger.info(f"Tool called: send_message_to_group for group {_sanitize(group_id)}")

    try:
        # Validate inputs
        message = _validate_message(message)
        group_id = group_id.strip()
        if not group_id:
            return {"error": "Group ID cannot be empty"}

        # Check allowlist
        _check_allowlist_group(group_id)

        # Determine if group_id is a base64 Signal group ID or a display name.
        # Base64 group IDs are typically 44+ chars ending with '='
        # If it looks like a base64 ID, pass it directly to signal-cli's -g flag.
        # Otherwise, look it up by display name.
        if "=" in group_id or (len(group_id) > 20 and "/" in group_id):
            # Looks like a base64 group ID — use directly
            resolved_group_id = group_id
        else:
            # Looks like a display name — look up the group ID
            resolved_group_id = await _get_group_id(group_id)
            if not resolved_group_id:
                logger.error(f"Could not find group: {_sanitize(group_id)}")
                return {"error": f"Could not find group: {group_id}"}

        success = await _send_message(message, resolved_group_id, is_group=True)
        if success:
            logger.info(f"Successfully sent message to group {_sanitize(group_id)}")
            return {"message": "Message sent successfully"}
        logger.error(f"Failed to send message to group {_sanitize(group_id)}")
        return {"error": "Failed to send message"}
    except (AllowlistError, ValidationError) as e:
        logger.warning(f"Blocked send_message_to_group: {str(e)}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_message_to_group: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def send_file(
    file_path: str,
    recipient: str,
    message: str = "",
) -> Union[SuccessResponse, ErrorResponse]:
    """Send a file attachment to a Signal user or group using signal-cli.

    Args:
        file_path: Path to the file to send (must be within allowed directories).
        recipient: The recipient's phone number, UUID, or u:username.
        message: Optional text message to accompany the file.
    """
    logger.info(
        f"Tool called: send_file to {_sanitize(recipient)}, "
        f"file: {_sanitize(file_path)}"
    )

    try:
        # Validate inputs
        recipient = _validate_recipient(recipient)
        if message:
            message = _validate_message(message)
        validated_path = _validate_file_path(file_path)

        # Check allowlist
        _check_allowlist_recipient(recipient)

        # Build signal-cli command args
        args = ["-u", config.user_id, "send", recipient]
        args.extend(["-m", message or ""])
        args.extend(["--attachment", str(validated_path)])

        _, stderr, return_code = await _run_signal_cli(args)

        if return_code == 0:
            logger.info(
                f"Successfully sent file to {_sanitize(recipient)}: "
                f"{_sanitize(str(validated_path))}"
            )
            return {"message": f"File sent successfully: {validated_path.name}"}
        else:
            logger.error(f"Error sending file: {stderr}")
            return {"error": f"Failed to send file: {stderr}"}

    except (AllowlistError, ValidationError) as e:
        logger.warning(f"Blocked send_file: {str(e)}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error in send_file: {str(e)}", exc_info=True)
        return {"error": str(e)}


@mcp.tool()
async def receive_message(timeout: float) -> list[MessageResponse]:
    """Wait for and receive messages using signal-cli.

    Returns all messages received within the timeout period.
    """
    safe_timeout = _validate_timeout(timeout)
    logger.info(f"Tool called: receive_message with timeout {safe_timeout}s")

    # If a polling daemon is configured, read from the SQLite inbox
    if config.db_path:
        return _receive_from_db()

    # Fallback: direct signal-cli call
    return await _receive_from_signal_cli(safe_timeout)


def _receive_from_db() -> list[MessageResponse]:
    """Read unprocessed messages from the SQLite inbox populated by the polling daemon."""
    from lunch_time_mcp.db import get_unprocessed, mark_processed

    try:
        inbox_msgs = get_unprocessed(config.db_path)
        if not inbox_msgs:
            logger.info("No unprocessed messages in inbox")
            return []

        results: list[MessageResponse] = []
        processed_ids: list[int] = []

        for msg in inbox_msgs:
            results.append(
                MessageResponse(
                    message=msg.message,
                    sender_id=msg.sender_uuid,
                    group_name=msg.group_id,
                )
            )
            processed_ids.append(msg.id)

        # Mark as processed so they aren't returned again
        mark_processed(config.db_path, processed_ids)

        logger.info(f"Retrieved {len(results)} message(s) from inbox")
        return results

    except Exception as e:
        logger.error(f"Error reading from inbox DB: {str(e)}", exc_info=True)
        return [MessageResponse(error=str(e))]


async def _receive_from_signal_cli(safe_timeout: int) -> list[MessageResponse]:
    """Direct signal-cli receive fallback (when no polling daemon is configured)."""
    try:
        args = [
            "--output=json",
            "-u",
            config.user_id,
            "receive",
            "--timeout",
            str(safe_timeout),
        ]

        stdout, stderr, return_code = await _run_signal_cli(args)

        if return_code != 0:
            if "timeout" in stderr.lower():
                logger.info("Receive timeout reached with no messages")
                return []
            else:
                logger.error(f"Error receiving message: {stderr}")
                return [MessageResponse(error=f"Failed to receive message: {stderr}")]

        if not stdout.strip():
            logger.info("No message received within timeout")
            return []

        results = await _parse_receive_output(stdout)
        if results:
            filtered = _filter_by_allowlist(results)
            logger.info(
                f"Received {len(results)} message(s), "
                f"{len(filtered)} passed inbound filter"
            )
            return filtered
        else:
            logger.info("Received output but no parseable messages")
            return []

    except Exception as e:
        logger.error(f"Error in receive_message: {str(e)}", exc_info=True)
        return [MessageResponse(error=str(e))]


# --- Server initialization ---


def _load_allowlist(path: str) -> AllowlistConfig:
    """Load allowlist configuration from a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)

        allowlist = AllowlistConfig(
            allowed_recipients=set(data.get("allowed_recipients", [])),
            allowed_groups=set(data.get("allowed_groups", [])),
            allowed_senders=set(data.get("allowed_senders", [])),
            allowed_receive_groups=set(data.get("allowed_receive_groups", [])),
        )

        logger.info(
            f"Loaded allowlist: {len(allowlist.allowed_recipients)} recipients, "
            f"{len(allowlist.allowed_groups)} groups, "
            f"{len(allowlist.allowed_senders)} allowed senders, "
            f"{len(allowlist.allowed_receive_groups)} allowed receive groups"
        )
        return allowlist

    except FileNotFoundError:
        logger.error(f"Allowlist file not found: {path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in allowlist file: {e}")
        raise


def initialize_server() -> SignalConfig:
    """Initialize the Signal server with configuration."""
    logger.info("Initializing Signal server")

    parser = argparse.ArgumentParser(description="Run the Signal MCP server")
    parser.add_argument(
        "--user-id", required=True, help="Signal phone number for the user"
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio"],
        default="sse",
        help="Transport to use for communication with the client. (default: sse)",
    )
    parser.add_argument(
        "--allowlist",
        required=False,
        help=(
            "Path to JSON allowlist config file. "
            "Defines which recipients and groups the agent can contact. "
            "TIP: Use Signal UUIDs instead of phone numbers for spoof-proof identity."
        ),
    )
    parser.add_argument(
        "--debug-pii",
        action="store_true",
        default=False,
        help="Enable logging of PII (phone numbers, message bodies). Off by default.",
    )
    parser.add_argument(
        "--allowed-file-dirs",
        nargs="*",
        default=[],
        help="Directories from which the agent is allowed to send files.",
    )
    parser.add_argument(
        "--default-group",
        required=False,
        default="",
        help=(
            "Default Signal group for agent status updates. "
            "When set, send_message_to_group uses this group if no group_id is specified."
        ),
    )
    parser.add_argument(
        "--db-path",
        required=False,
        default="",
        help=(
            "Path to SQLite inbox database populated by the polling daemon. "
            "When set, receive_message reads from this DB instead of calling signal-cli directly."
        ),
    )

    args = parser.parse_args()

    # Set global config
    config.user_id = args.user_id
    config.transport = args.transport
    config.debug_pii = args.debug_pii
    config.allowed_file_dirs = args.allowed_file_dirs or []
    config.default_group = args.default_group or ""

    # Set up inbox database path for polling daemon
    if args.db_path:
        from lunch_time_mcp.db import init_db

        config.db_path = init_db(args.db_path)
        logger.info(f"Inbox DB configured at: {config.db_path}")

    # Load allowlist
    if args.allowlist:
        config.allowlist = _load_allowlist(args.allowlist)
    else:
        logger.warning(
            "No --allowlist provided. All send operations will be denied (fail-closed). "
            "Create a JSON file with allowed_recipients and allowed_groups."
        )

    logger.info(
        f"Initialized Signal server for user: {_sanitize(config.user_id)}, "
        f"transport: {config.transport}, "
        f"debug_pii: {config.debug_pii}"
    )
    return config


def run_mcp_server() -> str:
    """Run the MCP server in the current event loop."""
    cfg = initialize_server()

    transport = cfg.transport
    logger.info(f"Starting MCP server with transport: {transport}")

    return transport


def main() -> None:
    """Main function to run the Signal MCP server."""
    logger.info("Starting Signal MCP server")
    try:
        transport = run_mcp_server()
        mcp.run(transport)
    except Exception as e:
        logger.error(f"Error running Signal MCP server: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Signal MCP server shutting down")


if __name__ == "__main__":
    main()
