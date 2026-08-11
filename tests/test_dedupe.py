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
    "notify_via": "ntfy",
    "ntfy": {"topic": "test-topic"},
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
                     test_alert=False, dump=False, **overrides)
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
                         format="json", replay=None, self_test=False, github_output=False,
                         test_alert=False, dump=False)
        with redirect_stdout(StringIO()):
            monitor.run_poll(dict(CONFIG), args)
        check("dry-run writes no state", dry.exists(), False)

    # --- multi-channel resilience -------------------------------------------
    import urllib.error

    cfg = {"source": "web", "telegram": {"channels": ["good", "dead"]},
           "notify_via": "ntfy", "ntfy": {"topic": "t"}}

    def one_dead(channel):
        if channel == "dead":
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        return [msg(1, "SSD 1TB por R$ 300,00")]

    monitor._fetch_one_channel = one_dead
    got, _ = monitor.fetch_via_web(cfg, {})
    check("dead channel does not block the live one", len(got), 1)

    def all_dead(channel):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monitor._fetch_one_channel = all_dead
    try:
        monitor.fetch_via_web(cfg, {})
        FAILURES.append("all channels dead: expected failure, got none")
    except SystemExit:
        pass

    # Messages from several channels come back oldest-first, not grouped.
    def two_channels(channel):
        return ([msg(1, "a", ts=300)] if channel == "good"
                else [msg(2, "b", ts=100), msg(3, "c", ts=200)])

    monitor._fetch_one_channel = two_channels
    got, _ = monitor.fetch_via_web(cfg, {})
    check("merged channels sort by time", [m["ts"] for m in got], [100, 200, 300])

    check("brl format", monitor.brl(4299.9), "R$ 4.299,90")
    check("brl thousands", monitor.brl(1234567.5), "R$ 1.234.567,50")

    # Delivery routing: notify_via picks the channel, and one failing send
    # must not stop the rest of the batch.
    hits = [{"text": "iPhone 15 R$ 4.299,00", "link": "https://t.me/p/1",
             "matches": [{"rule": "iPhone", "terms": ["iphone"], "price": 4299.0}]},
            {"text": "Ebook GRÁTIS", "link": None,
             "matches": [{"rule": "Free", "terms": ["gratis"], "price": None}]}]

    calls = []
    monitor.send_telegram_dm = lambda cfg, text: (calls.append(text), True)[1]
    check("via bot delivers all", monitor.deliver({"notify_via": "bot"}, hits), 2)
    check("alert has rule name", "iPhone" in calls[0], True)
    check("alert has BR price", "R$ 4.299,00" in calls[0], True)
    check("alert has link", "https://t.me/p/1" in calls[0], True)

    monitor.send_telegram_dm = lambda cfg, text: "GRÁTIS" in text
    check("partial failure keeps going", monitor.deliver({"notify_via": "bot"}, hits), 1)

    routed = []
    monitor.send_saved_messages = lambda cfg, texts: (routed.extend(texts), len(texts))[1]
    check("via saved delivers batch", monitor.deliver({"notify_via": "saved"}, hits), 2)
    check("saved got both", len(routed), 2)

    # --- non-Telegram channels, with the HTTP layer stubbed -----------------
    posts = []

    def fake_post(url, payload=None, data=None, headers=None, timeout=20):
        posts.append({"url": url, "payload": payload, "data": data,
                      "headers": headers or {}})
        return True

    monitor._http_post = fake_post

    posts.clear()
    n = monitor.deliver({"notify_via": "ntfy",
                         "ntfy": {"topic": "abc123", "priority": 4}}, hits)
    check("ntfy sends each match", n, 2)
    check("ntfy hits ntfy.sh", posts[0]["url"], "https://ntfy.sh")
    check("ntfy carries topic", posts[0]["payload"]["topic"], "abc123")
    check("ntfy title has price", "R$ 4.299,00" in posts[0]["payload"]["title"], True)
    check("ntfy click-through", posts[0]["payload"]["click"], "https://t.me/p/1")
    check("ntfy omits click when no link", "click" in posts[1]["payload"], False)
    check("ntfy priority passed", posts[0]["payload"]["priority"], 4)

    posts.clear()
    n = monitor.deliver({"notify_via": "ntfy",
                         "ntfy": {"topic": "t", "server": "https://push.example.com/"}}, hits)
    check("ntfy honours self-hosted server", posts[0]["url"], "https://push.example.com")

    posts.clear()
    monitor.deliver({"notify_via": "pushover",
                     "pushover": {"token": "tk", "user_key": "uk"}}, hits)
    check("pushover endpoint", posts[0]["url"], "https://api.pushover.net/1/messages.json")
    check("pushover form-encoded", b"token=tk" in posts[0]["data"], True)

    posts.clear()
    monitor.deliver({"notify_via": "discord",
                     "discord": {"webhook_url": "https://discord.com/api/webhooks/x"}}, hits)
    check("discord sends embed", "embeds" in posts[0]["payload"], True)

    posts.clear()
    monitor.deliver({"notify_via": "webhook",
                     "webhook": {"url": "https://example.com/h",
                                 "headers": {"X-Key": "s"}}}, hits)
    check("webhook custom header", posts[0]["headers"].get("X-Key"), "s")

    # Two channels at once: both fire, and the count is per-promo not per-send.
    posts.clear()
    n = monitor.deliver({"notify_via": ["ntfy", "discord"],
                         "ntfy": {"topic": "t"},
                         "discord": {"webhook_url": "https://discord.com/api/webhooks/x"}},
                        hits)
    check("fan-out posts to both", len(posts), 4)
    check("fan-out counts promos once", n, 2)

    # A dead channel must not stop a healthy one.
    def flaky_post(url, payload=None, data=None, headers=None, timeout=20):
        posts.append(url)
        return "discord" not in url

    monitor._http_post = flaky_post
    posts.clear()
    n = monitor.deliver({"notify_via": ["discord", "ntfy"],
                         "ntfy": {"topic": "t"},
                         "discord": {"webhook_url": "https://discord.com/api/webhooks/x"}},
                        hits)
    check("healthy channel survives a dead one", n, 2)

    # Unknown channel warns, does not crash.
    monitor._http_post = fake_post
    check("unknown channel is not fatal", monitor.deliver({"notify_via": "carrier-pigeon"},
                                                          hits), 0)

    # --- config validation --------------------------------------------------
    def expect_exit(label, cfg):
        try:
            monitor.validate_config(cfg)
        except SystemExit:
            return
        FAILURES.append(f"{label}: expected config rejection, got none")

    expect_exit("ntfy without topic", {"source": "web", "telegram": {"channel": "c"},
                                       "notify_via": "ntfy"})
    expect_exit("saved without user source", {"source": "web", "telegram": {"channel": "c"},
                                              "notify_via": "saved"})
    expect_exit("empty notify_via", {"source": "web", "telegram": {"channel": "c"},
                                     "notify_via": []})
    expect_exit("bot without token", {"source": "web", "telegram": {"channel": "c"},
                                      "notify_via": "bot"})
    monitor.validate_config({"source": "web", "telegram": {"channel": "LaPromotion"},
                             "notify_via": "ntfy", "ntfy": {"topic": "x"}})

    # HTML in a promo must not break Telegram's HTML parse_mode.
    esc = monitor.format_alert({"text": "TV <55\"> & more", "link": None,
                                "matches": [{"rule": "TV", "terms": ["tv"], "price": None}]})
    check("escapes angle brackets", "<55" not in esc, True)
    check("escapes ampersand", "&amp;" in esc, True)

    if FAILURES:
        print("INTEGRATION TEST FAILED")
        for f in FAILURES:
            print("  x " + f)
        return 1
    print("integration test: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
