#!/usr/bin/env python3
"""Integration checks for the polling loop: state, dedupe, and the age guard.

Runs fully offline by stubbing the Telegram fetch. Exercised in CI.

  python3 tests/test_dedupe.py
"""

import json
import sys
import tempfile
import time
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

CONFIG = {
    "source": "bot",
    "telegram": {"bot_token": "x", "notify_chat_id": "1"},
    "max_age_hours": 24,
    "retain_days": 14,
    "rules": [
        {"name": "iPhone", "any": ["iphone"], "none": ["capinha"], "max_price": 5000},
        {"name": "Free", "any": ["gratis"]},
    ],
}

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def poll(state_path, messages, **overrides):
    """Run one polling cycle against a canned message list."""
    monitor.fetch_via_bot = lambda cfg, st: (messages, dict(st))
    args = Namespace(config=None, state=str(state_path), notify=False, dry_run=False,
                     format="json", replay=None, self_test=False, github_output=False,
                     **overrides)
    buf = StringIO()
    with redirect_stdout(buf):
        monitor.run_poll(dict(CONFIG), args)
    return json.loads(buf.getvalue())


def msg(mid, text, ts=None):
    return {"id": mid, "text": text, "ts": ts if ts is not None else int(time.time()),
            "chat": "Promos", "link": f"https://t.me/promos/{mid}"}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "seen.json"

        batch = [
            msg(1, "iPhone 15 por R$ 4.299,00"),
            msg(2, "Capinha de iPhone R$ 29,90"),
            msg(3, "Ebook GRÁTIS hoje"),
            msg(4, "Bom dia pessoal"),
        ]

        first = poll(state, batch)
        check("first poll matches", first["match_count"], 2)
        check("state file written", state.exists(), True)

        # The whole point: polling again must not re-alert the same promos.
        second = poll(state, batch)
        check("repeat poll is silent", second["match_count"], 0)
        check("repeat poll still reads", second["messages_seen"], 4)

        # A genuinely new promo still comes through.
        third = poll(state, batch + [msg(5, "iPhone 14 por R$ 3.100,00")])
        check("new promo alerts", third["match_count"], 1)

        # An edit that only changes case/spacing is not a new alert.
        fourth = poll(state, batch + [msg(6, "IPHONE 14  POR  R$ 3.100,00")])
        check("edited repost is silent", fourth["match_count"], 0)

        # Losing state must not replay the backlog to the phone.
        fresh = Path(tmp) / "fresh.json"
        old = int(time.time()) - 72 * 3600
        recovered = poll(fresh, [msg(7, "iPhone 13 por R$ 2.000,00", ts=old),
                                 msg(8, "iPhone 12 por R$ 1.500,00", ts=old)])
        check("stale backlog suppressed", recovered["match_count"], 0)
        check("stale backlog counted", recovered["skipped_as_old"], 2)

        # Pruning: entries older than retain_days should not accumulate forever.
        blob = json.loads(state.read_text())
        blob["seen"]["ancient"] = time.time() - 90 * 86400
        state.write_text(json.dumps(blob))
        poll(state, batch)
        check("old fingerprints pruned",
              "ancient" in json.loads(state.read_text())["seen"], False)

        # dry-run must not persist anything.
        dry = Path(tmp) / "dry.json"
        monitor.fetch_via_bot = lambda cfg, st: ([msg(9, "iPhone SE R$ 1.200,00")], dict(st))
        args = Namespace(config=None, state=str(dry), notify=True, dry_run=True,
                         format="json", replay=None, self_test=False, github_output=False)
        with redirect_stdout(StringIO()):
            monitor.run_poll(dict(CONFIG), args)
        check("dry-run writes no state", dry.exists(), False)

    check("brl format", monitor.brl(4299.9), "R$ 4.299,90")
    check("brl thousands", monitor.brl(1234567.5), "R$ 1.234.567,50")

    if FAILURES:
        print("INTEGRATION TEST FAILED")
        for f in FAILURES:
            print("  x " + f)
        return 1
    print("integration test: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
