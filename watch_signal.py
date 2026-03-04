import sqlite3
import time
import sys
import json
from pathlib import Path

DB_PATH = Path.home() / ".lunch-time-mcp" / "inbox.db"
TRIGGER_FILE = Path("/tmp/signal_prompt.txt")

def watch_inbox():
    print(f"Watching {DB_PATH} for new Signal messages...")
    print(f"Will write prompts to {TRIGGER_FILE}")
    
    # Ensure DB exists
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)

    while True:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            # Get the oldest unprocessed message
            cursor = conn.execute(
                "SELECT id, sender_uuid, message, group_id FROM inbox WHERE processed = 0 ORDER BY created_at ASC LIMIT 1"
            )
            row = cursor.fetchone()
            
            if row:
                msg_id, sender, text, group_id = row
                
                # Format the trigger content
                trigger_data = {
                    "sender": sender,
                    "group_id": group_id,
                    "message": text
                }
                
                # Write to the trigger file
                with open(TRIGGER_FILE, "w") as f:
                    f.write(json.dumps(trigger_data, indent=2))
                    
                print(f"[{time.strftime('%H:%M:%S')}] Wrote message {msg_id} to trigger file.")
                
                # Mark as processed so we don't read it again
                conn.execute("UPDATE inbox SET processed = 1 WHERE id = ?", (msg_id,))
                conn.commit()
                
                # Wait for the agent to delete the trigger file before we process the next message.
                # This prevents us from overwriting the file if the agent is busy.
                print("Waiting for agent to consume trigger file...")
                while TRIGGER_FILE.exists():
                    time.sleep(1)
                print("Trigger file consumed. Resuming watch.")
                
            conn.close()
            time.sleep(2)  # Poll every 2 seconds
            
        except Exception as e:
            print(f"Error checking DB: {e}")
            time.sleep(5)

if __name__ == '__main__':
    watch_inbox()
