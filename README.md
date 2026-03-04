# Lunch-Time MCP

A fork of [signal-mcp](https://github.com/rymurr/signal-mcp) integration for [signal-cli](https://github.com/AsamK/signal-cli) that gives your AI assistants (like Antigravity) the ability to send and receive Signal messages. 

The primary use case is the **Break-Time Agent** — stepping away from your desk, dropping your agent into a watch loop, and continuing to collaborate with it via Signal from your phone.

Simple ACL's are in place, recommend UUIDs over phone numbers for spoof-proof identity. Use additional security measures.

## Features

- **Bi-Directional Communication**: Send and receive messages from the agent.
- **Polling Daemon**: A background continuous watcher (`watch_signal.py`) that ingests messages into a SQLite local DB.
- **Zero-Cost Watch Loop**: The agent can block on file changes, costing $0 API usage while waiting for your messages.
- **Secure Allowlisting**: Restrict the agent to only listen and respond to authorized Signal UUIDs/phone numbers.

## Prerequisites

This project requires [signal-cli](https://github.com/AsamK/signal-cli) to be installed and configured on your Mac/Linux system.

### Installing signal-cli

1. **Install signal-cli**: Follow the [official installation instructions](https://github.com/AsamK/signal-cli/blob/master/README.md#installation). If on macOS with Homebrew:
   ```bash
   brew install signal-cli
   ```

2. **Register your Signal account** (or link a secondary device):
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER register
   ```

3. **Verify your account** with the code received via SMS:
   ```bash
   signal-cli -u YOUR_PHONE_NUMBER verify CODE_RECEIVED
   ```

## Installation

Clone this repository and install the dependencies:

```bash
git clone https://github.com/asecretcompany/lunch-time-mcp.git
cd lunch-time-mcp
uv pip install -e .
```

## Security Configuration (Allowlist)

You MUST create a JSON allowlist so the agent knows who is authorized to give it commands and receive data. **Failure to do this will result in the agent failing-closed and rejecting all requests.**

Create `~/signalAllowList.json`:
```json
{
    "allowed_recipients": ["+1234567890", "YOUR_UUID"],
    "allowed_groups": ["Group_Name_or_Base64_ID"],
    "allowed_senders": ["+1234567890", "YOUR_UUID"],
    "allowed_receive_groups": ["Group_Name_or_Base64_ID"]
}
```
*Tip: Use Signal UUIDs instead of phone numbers for spoof-proof identity.*

## Setting up the Polling Daemon

The polling daemon ingests Signal messages into a local SQLite database. The MCP server reads from this DB instead of calling signal-cli directly, which avoids lock contention.

```bash
python -m lunch_time_mcp.signal_poller \
    --user-id +1YOURNUMBER \
    --allowlist ~/signalAllowList.json \
    --db-path ~/.lunch-time-mcp/inbox.db \
    --receive-timeout 5 \
    --poll-interval 10
```

The `--receive-timeout` (default 5s) controls how long each `signal-cli receive` call holds the lock. Keep it short to give the MCP server windows to send.

Also run the watch script to trigger the agent on new messages:

```bash
nohup python3 watch_signal.py > /tmp/watch_signal.log 2>&1 &
```
This polls the SQLite inbox and drops a `/tmp/signal_prompt.txt` trigger file whenever a new message arrives.

## Setting up Antigravity

Add the MCP server to your `mcp_config.json`:

```json
{
  "mcpServers": {
    "lunch-time-mcp": {
      "command": "/path/to/lunch-time-mcp/.venv/bin/python",
      "args": [
        "/path/to/lunch-time-mcp/lunch_time_mcp/main.py",
        "--user-id",
        "+1YOURNUMBER",
        "--allowlist",
        "/path/to/signalAllowList.json",
        "--transport",
        "stdio",
        "--db-path",
        "/path/to/.lunch-time-mcp/inbox.db"
      ]
    }
  }
}
```

### The "Lunch Time" Workflow

To create a reusable skill in Antigravity, save the following to `.agents/workflows/lunch_time.md` in your project root:

```markdown
---
description: Starts the Signal Break-Time Agent to allow bi-directional communication via Signal indefinitely.
---
This workflow puts the agent into a continuous listening loop for Signal messages when the user is away from their desk.

1. Block and wait for a new Signal message by monitoring the trigger file:
// turbo
`rm -f /tmp/signal_prompt.txt && while [ ! -f /tmp/signal_prompt.txt ]; do sleep 1; done && cat /tmp/signal_prompt.txt`

2. Once the command completes, parse the message content, execute the tasks, and formulate a concise summary.

3. Send the response back via the `send_message_to_group` MCP tool. The default group is pre-configured.

4. **Loop:** IMMEDIATELY repeat Step 1.
```

When you step away from your computer, simply type `/lunch_time` into the IDE chat and the agent will wait for your Signal instructions!
