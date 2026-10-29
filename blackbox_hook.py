"""Best-effort SessionStart hook: authenticated registration, no output or prompt access."""
import json
import os
from pathlib import Path
import sys

import blackbox


def main():
    try:
        token = os.environ.get("CLAUDE_CHEAT_BLACKBOX_TOKEN", "")
        raw = sys.stdin.buffer.read(65537)
        if not token or len(raw) > 65536:
            return
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart":
            return
        blackbox.Blackbox(Path(__file__).resolve().parent).register_launch(token, event)
    except (OSError, ValueError, TypeError, UnicodeError):
        # Recording is optional and must never block or affect the Claude session.
        return


if __name__ == "__main__":
    main()
