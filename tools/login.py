#!/usr/bin/env python3
"""One-time login for reading a group you do not administer.

Run this ONCE on your own machine (not in CI -- it asks for a login code):

  pip install telethon
  export TELEGRAM_API_ID=1234567
  export TELEGRAM_API_HASH=abcdef...
  python3 tools/login.py

Get api_id / api_hash from https://my.telegram.org -> API development tools.

It prints a session string (paste into the TELEGRAM_SESSION GitHub secret) and
lists every group and channel you are in, with the chat_id for each.

The session string is as sensitive as your password -- never commit it.
"""

import os
import sys

try:
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient
except ImportError:
    sys.exit("error: pip install telethon")


def main() -> int:
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        print("error: set TELEGRAM_API_ID and TELEGRAM_API_HASH first", file=sys.stderr)
        print("       get them from https://my.telegram.org > API development tools",
              file=sys.stderr)
        return 1

    existing = os.environ.get("TELEGRAM_SESSION", "").strip()
    session = StringSession(existing) if existing else StringSession()

    with TelegramClient(session, int(api_id), api_hash) as client:
        me = client.get_me()
        print(f"\nlogged in as {me.first_name} (@{me.username})\n")

        saved = client.session.save()
        print("=" * 70)
        print("TELEGRAM_SESSION  (add as a GitHub secret -- treat it like a password)")
        print("=" * 70)
        print(saved)
        print("=" * 70)

        print("\nyour groups and channels:\n")
        rows = []
        for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            entity = dialog.entity
            username = getattr(entity, "username", None)
            kind = "channel" if dialog.is_channel and not dialog.is_group else "group"
            public = f"@{username}" if username else "private"
            rows.append((dialog.id, kind, public, dialog.name or ""))

        if not rows:
            print("  (none found)")
        width = max((len(str(r[0])) for r in rows), default=12)
        for cid, kind, public, name in rows:
            print(f"  {str(cid):>{width}}  [{kind:<7}] {public:<20} {name[:40]}")

        print("\nput the id of the promo group in config.yaml as telegram.chat_id")
        print("tip: a PUBLIC CHANNEL (has an @name and is listed as 'channel') can be")
        print("     read with `source: web` instead -- no session or credentials at all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
