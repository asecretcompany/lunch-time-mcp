---
description: Starts the Signal Break-Time Agent to allow bi-directional communication via Signal indefinitely.
---
This workflow puts the agent into a continuous listening loop for Signal messages when the user is away from their desk, such as during "lunch time".

6. Ensure the signal watch script is running:
// turbo
`nohup python3 ./watch_signal.py > /tmp/watch_signal.log 2>&1 &`

2. Block and wait for a new Signal message by monitoring the trigger file:
// turbo
`rm -f /tmp/signal_prompt.txt && while [ ! -f /tmp/signal_prompt.txt ]; do sleep 1; done && cat /tmp/signal_prompt.txt`
*(Use the `command_status` tool with `WaitDurationSeconds: 300` to hang on this command indefinitely. Re-run `command_status` if the 5 minutes elapse without output).*

3. Once the command completes and outputs the JSON from the trigger file, parse the message content, execute the user's requested tasks in the codebase, and formulate a concise summary of the results.

4. Send the response back to the user via Signal. Replace the placeholder message with your summary result.
// turbo
`signal-cli -u +1YOURNUMBER send -g YOUR_GROUP_ID -m "<RESPONSE_SUMMARY>"`

5. **Loop to maintain conversation:** IMMEDIATELY repeat Step 2 to wait for new messages or follow-up feedback. Do not prompt the user in the IDE chat unless explicit feedback is required via `notify_user` — assume listening mode.

6. If the user initiates a new chat *in the IDE window*, or explicitly cancels the mode, gracefully exit the loop and resume normal IDE operations.
