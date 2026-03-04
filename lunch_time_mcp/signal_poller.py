"""Persistent polling daemon for Signal MCP.

Continuously polls signal-cli for incoming messages and stores them
in a local SQLite inbox. The MCP server's receive_message tool reads
from this inbox instead of calling signal-cli directly.

Usage:
    python -m lunch_time_mcp.signal_poller \
        --user-id +1YOURNUMBER \
        --allowlist ~/signalAllowList.json \
        --db-path ~/.lunch-time-mcp/inbox.db \
        --poll-interval 30
"""

import asyncio
import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from lunch_time_mcp.db import init_db, insert_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("lunch-time-mcp.poller")


class Poller:
    """Signal polling daemon that ingests messages into SQLite."""

    def __init__(
        self,
        user_id: str,
        db_path: Path,
        poll_interval: int = 10,
        receive_timeout: int = 5,
        allowlist_path: str | None = None,
    ):
        self.user_id = user_id
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.receive_timeout = receive_timeout
        self._running = True
        self._allowed_senders: set[str] = set()
        self._allowed_groups: set[str] = set()

        if allowlist_path:
            self._load_allowlist(allowlist_path)

    def _load_allowlist(self, path: str) -> None:
        """Load the allowlist for inbound filtering."""
        try:
            with open(path) as f:
                data = json.load(f)
            self._allowed_senders = set(data.get("allowed_senders", []))
            self._allowed_groups = set(data.get("allowed_receive_groups", []))
            logger.info(
                f"Loaded allowlist: {len(self._allowed_senders)} senders, "
                f"{len(self._allowed_groups)} groups"
            )
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load allowlist: {e}")
            sys.exit(1)

    def _is_allowed(self, sender_uuid: str, group_id: str | None) -> bool:
        """Check if a message passes the allowlist filter."""
        # If sender filter is configured, sender must be in the list
        if self._allowed_senders and sender_uuid not in self._allowed_senders:
            logger.debug(f"Sender {sender_uuid[:8]}... not in allowlist, dropping")
            return False

        # If group filter is configured and this is a group message, group must be allowed
        if group_id and self._allowed_groups and group_id not in self._allowed_groups:
            logger.debug(f"Group {group_id[:12]}... not in allowlist, dropping")
            return False

        return True

    async def _poll_once(self) -> int:
        """Run one poll cycle. Returns number of messages ingested."""
        logger.debug("Polling signal-cli for new messages...")

        try:
            proc = await asyncio.create_subprocess_exec(
                "signal-cli",
                "--output=json",
                "-u",
                self.user_id,
                "receive",
                "--timeout",
                str(self.receive_timeout),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                stderr_text = stderr.decode().strip() if stderr else ""
                # During shutdown, signal-cli gets killed (rc=-9) — that's expected
                if not self._running:
                    logger.debug(
                        f"signal-cli terminated during shutdown (rc={proc.returncode})"
                    )
                elif "timeout" not in stderr_text.lower():
                    logger.error(
                        f"signal-cli error (rc={proc.returncode}): {stderr_text}"
                    )
                return 0

            if not stdout:
                return 0

            output = stdout.decode()
            count = 0

            for line in output.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping non-JSON line")
                    continue

                env = data.get("envelope", {})
                sender_uuid = env.get("sourceUuid")
                sender_number = env.get("sourceNumber") or env.get("source")

                data_message = env.get("dataMessage", {})
                message_body = data_message.get("message")

                if not message_body:
                    continue

                # Prefer UUID for identity
                sender = sender_uuid if sender_uuid else sender_number
                if not sender:
                    continue

                group_id = data_message.get("groupInfo", {}).get("groupId")
                timestamp = env.get("timestamp", time.time() * 1000)

                # Allowlist filter
                if not self._is_allowed(sender, group_id):
                    continue

                insert_message(
                    db_path=self.db_path,
                    timestamp=timestamp,
                    sender_uuid=sender,
                    message=message_body,
                    group_id=group_id,
                )
                count += 1
                logger.info(
                    f"Ingested message from {sender[:8]}..."
                    + (f" in group {group_id[:12]}..." if group_id else "")
                )

            return count

        except FileNotFoundError:
            logger.error("signal-cli not found in PATH")
            return 0
        except Exception as e:
            logger.error(f"Poll error: {e}", exc_info=True)
            return 0

    async def run(self) -> None:
        """Main polling loop."""
        logger.info(
            f"Starting poller: user={self.user_id}, "
            f"db={self.db_path}, receive_timeout={self.receive_timeout}s, "
            f"sleep={self.poll_interval}s"
        )

        while self._running:
            count = await self._poll_once()
            if count > 0:
                logger.info(f"Ingested {count} message(s) this cycle")

            # Sleep between cycles — this is when the lock is NOT held,
            # giving the MCP server a window to send messages.
            if self._running:
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Request graceful shutdown."""
        logger.info("Shutdown requested")
        self._running = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal MCP Polling Daemon")
    parser.add_argument("--user-id", required=True, help="Signal phone number")
    parser.add_argument("--allowlist", required=False, help="Path to allowlist JSON")
    parser.add_argument(
        "--db-path",
        default=str(Path.home() / ".lunch-time-mcp" / "inbox.db"),
        help="Path to SQLite inbox database",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds to sleep between poll cycles (default: 10)",
    )
    parser.add_argument(
        "--receive-timeout",
        type=int,
        default=5,
        help=(
            "Seconds for each signal-cli receive call (default: 5). "
            "Keep this short to minimize lock contention with the MCP server."
        ),
    )

    args = parser.parse_args()

    db_path = init_db(args.db_path)

    poller = Poller(
        user_id=args.user_id,
        db_path=db_path,
        poll_interval=args.poll_interval,
        receive_timeout=args.receive_timeout,
        allowlist_path=args.allowlist,
    )

    loop = asyncio.new_event_loop()

    # Graceful shutdown on SIGINT/SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, poller.stop)

    try:
        loop.run_until_complete(poller.run())
    finally:
        loop.close()
        logger.info("Poller exited")


if __name__ == "__main__":
    main()
