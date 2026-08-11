#!/usr/bin/env python3
"""Find the two chat ids you need for config.yaml.

  export TELEGRAM_BOT_TOKEN=123456:ABC...
  python3 tools/whoami.py

Send your bot a DM ("oi") and post any message in the promo group first --
the bot can only report chats it has actually heard from.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import _api_call  # noqa: E402


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("error: set TELEGRAM_BOT_TOKEN first", file=sys.stderr)
        print("       get one from @BotFather in Telegram", file=sys.stderr)
        return 1

    me = _api_call(token, "getMe", {}, timeout=20)
    print(f"bot: @{me.get('username')}  ({me.get('first_name')})")
    print(f"group privacy mode must be OFF for the bot to read group messages.")
    print(f"  -> @BotFather > /mybots > @{me.get('username')} > Bot Settings > Group Privacy > Turn off\n")

    updates = _api_call(token, "getUpdates", {
        "timeout": 0, "limit": 100,
        "allowed_updates": json.dumps(["message", "channel_post"]),
    }, timeout=30)

    if not updates:
        print("no updates yet.")
        print("  1. DM your bot anything (this gives you notify_chat_id)")
        print("  2. post any message in the promo group (this gives you chat_id)")
        print("  3. run this again")
        return 0

    seen = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        cid = chat.get("id")
        if cid is None or cid in seen:
            continue
        seen[cid] = {
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
        }

    print("chats your bot can see:\n")
    for cid, info in seen.items():
        role = ("<- notify_chat_id (your DM)" if info["type"] == "private"
                else "<- chat_id (the group to watch)")
        print(f"  {cid}   [{info['type']}] {info['title']}  {role}")

    private = [c for c, i in seen.items() if i["type"] == "private"]
    groups = [c for c, i in seen.items() if i["type"] != "private"]
    print("\nconfig.yaml / secrets:")
    if groups:
        print(f"  telegram.chat_id:         \"{groups[0]}\"")
    if private:
        print(f"  TELEGRAM_NOTIFY_CHAT_ID:  {private[0]}")
    if not private:
        print("  (DM your bot to get your notify chat id)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
