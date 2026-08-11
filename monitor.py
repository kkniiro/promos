#!/usr/bin/env python3
"""Watch a Telegram promo group and surface only the messages that match your keywords.

Stdlib only -- no pip install, so it runs anywhere Python 3.9+ does.

Two ways to read a group:
  bot  -- getUpdates via a bot you added to the group. Works for private groups.
  web  -- scrapes https://t.me/s/<channel>. Public channels only, no token.

Usage:
  python3 monitor.py                     # poll, print matches as JSON, update state
  python3 monitor.py --notify            # ...and push each match to your phone
  python3 monitor.py --format text       # human-readable output
  python3 monitor.py --self-test         # offline check of matching + parsing
  python3 monitor.py --replay FILE       # run rules against a fixture, ignore state
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_STATE = ROOT / "state" / "seen.json"
API = "https://api.telegram.org"
STATE_VERSION = 1
USER_AGENT = "Mozilla/5.0 (compatible; promo-monitor/1.0)"


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase and strip accents so 'Promoção' and 'promocao' compare equal."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def contains_term(haystack: str, term: str) -> bool:
    """Substring match that will not fire mid-word.

    'tv' matches 'tv' and 'tvs' but not 'netvibes'; 'ps5' matches 'ps5!' but
    not 'wps5000'. Both sides are already normalised by the caller.
    """
    term = term.strip()
    if not term:
        return False
    # A term with its own spaces/punctuation is matched literally.
    pattern = r"(?<![0-9a-z])" + re.escape(term)
    return re.search(pattern, haystack) is not None


# Brazilian and plain formats: R$ 1.234,56 / R$1234.56 / 1.234,56 reais
_PRICE_RE = re.compile(
    r"(?:r\$|rs|brl)\s*([0-9][0-9.,\s]*)|([0-9][0-9.,]*)\s*(?:reais|conto)",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float | None:
    raw = raw.strip().replace(" ", "")
    if not raw:
        return None
    # Decide which separator is the decimal one by looking at the last group.
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")   # 1.234,56
        else:
            raw = raw.replace(",", "")                      # 1,234.56
    elif "," in raw:
        # ',' is decimal only when it separates exactly 2 trailing digits.
        raw = raw.replace(",", ".") if re.search(r",\d{2}$", raw) else raw.replace(",", "")
    elif re.search(r"\.\d{3}(?!\d)", raw):
        raw = raw.replace(".", "")                          # 1.234 -> thousands
    raw = raw.rstrip(".")
    try:
        return float(raw)
    except ValueError:
        return None


def extract_prices(text: str) -> list[float]:
    """Every currency-looking number in the message, cheapest first."""
    found: list[float] = []
    for m in _PRICE_RE.finditer(text or ""):
        value = _to_float(m.group(1) or m.group(2) or "")
        if value is not None and 0 < value < 10_000_000:
            found.append(value)
    return sorted(found)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _expand_env(value):
    """Resolve ${VAR} references so secrets stay out of the config file."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: no config at {path}\n       copy config.example.yaml to config.yaml and edit it.")
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # available on GitHub runners and most systems
        data = yaml.safe_load(text)
    except ImportError:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            sys.exit("error: PyYAML is not installed and the config is not valid JSON.\n"
                     "       run: pip install pyyaml")
    if not isinstance(data, dict):
        sys.exit("error: config must be a mapping")
    return _expand_env(data)


def compile_rules(config: dict) -> list[dict]:
    rules = []
    for i, raw in enumerate(config.get("rules") or []):
        if not isinstance(raw, dict):
            sys.exit(f"error: rule #{i + 1} must be a mapping")
        rule = {
            "name": raw.get("name") or f"rule {i + 1}",
            "any": [normalize(t) for t in (raw.get("any") or []) if str(t).strip()],
            "all": [normalize(t) for t in (raw.get("all") or []) if str(t).strip()],
            "none": [normalize(t) for t in (raw.get("none") or []) if str(t).strip()],
            "max_price": raw.get("max_price"),
            "min_price": raw.get("min_price"),
        }
        if not (rule["any"] or rule["all"]):
            sys.exit(f"error: rule '{rule['name']}' needs at least one 'any' or 'all' keyword")
        rules.append(rule)
    if not rules:
        sys.exit("error: config has no rules -- nothing to watch for")
    return rules


def match_rules(text: str, rules: list[dict]) -> list[dict]:
    """Return every rule that fires on this message, with the reason it fired."""
    hay = normalize(text)
    if not hay:
        return []
    prices = extract_prices(text)
    cheapest = prices[0] if prices else None
    hits = []

    for rule in rules:
        if any(contains_term(hay, t) for t in rule["none"]):
            continue
        matched_all = [t for t in rule["all"] if contains_term(hay, t)]
        if len(matched_all) != len(rule["all"]):
            continue
        matched_any = [t for t in rule["any"] if contains_term(hay, t)]
        if rule["any"] and not matched_any:
            continue

        # Price gates only apply when the message actually quotes a price.
        if rule["max_price"] is not None:
            if cheapest is None or cheapest > float(rule["max_price"]):
                continue
        if rule["min_price"] is not None:
            if cheapest is None or cheapest < float(rule["min_price"]):
                continue

        hits.append({
            "rule": rule["name"],
            "terms": sorted(set(matched_any + matched_all)),
            "price": cheapest,
        })
    return hits


# --------------------------------------------------------------------------
# Source: bot API (private groups)
# --------------------------------------------------------------------------

def _api_call(token: str, method: str, params: dict | None = None, timeout: int = 40) -> dict:
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"error: telegram {method} failed ({exc.code}): {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {API} ({exc.reason}). "
                         "If this is a Claude sandbox, api.telegram.org must be allowlisted.")
    if not payload.get("ok"):
        raise SystemExit(f"error: telegram {method}: {payload.get('description')}")
    return payload["result"]


def fetch_via_bot(config: dict, state: dict) -> tuple[list[dict], dict]:
    token = (config.get("telegram", {}).get("bot_token") or "").strip()
    if not token:
        sys.exit("error: source 'bot' needs telegram.bot_token (set TELEGRAM_BOT_TOKEN)")
    watch = config.get("telegram", {}).get("chat_id")
    watch = str(watch).strip() if watch not in (None, "") else None

    params = {"timeout": 0, "limit": 100,
              "allowed_updates": json.dumps(["message", "channel_post",
                                             "edited_message", "edited_channel_post"])}
    offset = state.get("last_update_id")
    if offset:
        params["offset"] = int(offset) + 1

    updates = _api_call(token, "getUpdates", params)
    messages, highest = [], state.get("last_update_id")

    for upd in updates:
        highest = upd["update_id"] if highest is None else max(highest, upd["update_id"])
        msg = (upd.get("message") or upd.get("channel_post")
               or upd.get("edited_message") or upd.get("edited_channel_post"))
        if not msg:
            continue
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        username = chat.get("username") or ""
        if watch and watch not in (chat_id, username, f"@{username}"):
            continue
        text = msg.get("text") or msg.get("caption") or ""
        if not text.strip():
            continue
        link = None
        if username and msg.get("message_id"):
            link = f"https://t.me/{username}/{msg['message_id']}"
        messages.append({
            "id": f"{chat_id}:{msg.get('message_id')}",
            "text": text,
            "ts": msg.get("date"),
            "chat": chat.get("title") or username or chat_id,
            "link": link,
        })

    new_state = dict(state)
    if highest is not None:
        new_state["last_update_id"] = highest
    return messages, new_state


# --------------------------------------------------------------------------
# Source: public channel web preview
# --------------------------------------------------------------------------

class _ChannelParser(HTMLParser):
    """Pulls message text + permalink out of a t.me/s/<channel> page.

    Telegram renders each post as
      <div class="tgme_widget_message" data-post="chan/123">
        ... <div class="tgme_widget_message_text">body</div>
    We track nesting depth so we stop collecting at the right closing tag.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._post: str | None = None
        self._time: str | None = None
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "tgme_widget_message" in classes and attrs.get("data-post"):
            self._post = attrs["data-post"]
            self._time = None
        if tag == "time" and attrs.get("datetime") and self._post:
            self._time = attrs["datetime"]
        if self._depth:
            # Already inside a text block: keep track of nesting.
            if tag not in ("br", "img", "hr", "input", "meta", "link"):
                self._depth += 1
            if tag == "br":
                self._buf.append("\n")
            return
        if "tgme_widget_message_text" in classes:
            self._depth = 1
            self._buf = []

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = re.sub(r"\n{3,}", "\n\n", "".join(self._buf)).strip()
                if text and self._post:
                    self.posts.append({"post": self._post, "text": text, "time": self._time})
                self._buf = []

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


# --------------------------------------------------------------------------
# Source: your own account (groups you are only a member of)
# --------------------------------------------------------------------------

def fetch_via_user(config: dict, state: dict) -> tuple[list[dict], dict]:
    """Read a group through your own Telegram account.

    This is the only option for a group you do not administer: a bot cannot be
    added by a non-admin, and t.me/s/ previews exist only for public channels.
    Your account already receives these messages -- this just reads them.

    Needs Telethon: pip install telethon
    """
    tg = config.get("telegram", {})
    api_id, api_hash = str(tg.get("api_id") or "").strip(), (tg.get("api_hash") or "").strip()
    session = (tg.get("session") or "").strip()
    target = tg.get("chat_id")

    # Config errors first, so a missing secret reads as a config problem even
    # when Telethon is not installed yet.
    missing = [n for n, v in (("api_id", api_id), ("api_hash", api_hash),
                              ("session", session)) if not v]
    if missing:
        sys.exit(f"error: source 'user' needs telegram.{', telegram.'.join(missing)}\n"
                 "       run tools/login.py once to generate them")
    if target in (None, ""):
        sys.exit("error: source 'user' needs telegram.chat_id -- run tools/login.py to list your groups")

    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("error: source 'user' needs Telethon -- run: pip install telethon")

    try:
        target = int(str(target).strip())
    except ValueError:
        target = str(target).strip().lstrip("@")   # public @username also works

    limit = int(config.get("fetch_limit", 60))
    messages = []
    with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
        entity = client.get_entity(target)
        title = getattr(entity, "title", None) or getattr(entity, "username", "") or str(target)
        username = getattr(entity, "username", None)
        for m in client.iter_messages(entity, limit=limit):
            text = (m.message or "") or (getattr(m, "caption", "") or "")
            if not text.strip():
                continue
            if username:
                link = f"https://t.me/{username}/{m.id}"
            else:
                # Private supergroups are addressed as t.me/c/<id-without-prefix>/<msg>
                internal = str(getattr(entity, "id", "")).lstrip("-")
                link = f"https://t.me/c/{internal}/{m.id}"
            messages.append({
                "id": f"{getattr(entity, 'id', target)}:{m.id}",
                "text": text,
                "ts": int(m.date.timestamp()) if m.date else None,
                "chat": title,
                "link": link,
            })

    messages.reverse()          # oldest first, so alerts arrive in order
    return messages, dict(state)


def fetch_via_web(config: dict, state: dict) -> tuple[list[dict], dict]:
    channel = (config.get("telegram", {}).get("channel") or "").strip().lstrip("@")
    if not channel:
        sys.exit("error: source 'web' needs telegram.channel (the public @name)")
    url = f"https://t.me/s/{urllib.parse.quote(channel)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            html_text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"error: GET {url} failed ({exc.code}). Is the channel public?")
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach t.me ({exc.reason}). "
                         "If this is a Claude sandbox, t.me must be allowlisted.")

    parser = _ChannelParser()
    parser.feed(html_text)
    messages = []
    for post in parser.posts:
        ts = None
        if post["time"]:
            try:
                ts = int(datetime.fromisoformat(post["time"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        messages.append({
            "id": post["post"],
            "text": post["text"],
            "ts": ts,
            "chat": channel,
            "link": f"https://t.me/{post['post']}",
        })
    return messages, dict(state)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "seen": {}, "last_update_id": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "seen": {}, "last_update_id": None}
    data.setdefault("seen", {})
    data.setdefault("last_update_id", None)
    data.setdefault("version", STATE_VERSION)
    return data


def save_state(path: Path, state: dict, retain_days: int = 14) -> None:
    cutoff = time.time() - retain_days * 86400
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if v >= cutoff}
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def fingerprint(msg: dict) -> str:
    """Identify a message by content, so an edit or a repost is still one alert."""
    basis = normalize(msg.get("text", ""))[:500]
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------

def send_telegram_dm(config: dict, text: str) -> bool:
    tg = config.get("telegram", {})
    token = (tg.get("bot_token") or "").strip()
    target = str(tg.get("notify_chat_id") or "").strip()
    if not token or not target:
        print("warn: notify skipped -- set telegram.bot_token and telegram.notify_chat_id",
              file=sys.stderr)
        return False
    try:
        _api_call(token, "sendMessage", {
            "chat_id": target,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }, timeout=20)
        return True
    except SystemExit as exc:
        print(f"warn: notify failed -- {exc}", file=sys.stderr)
        return False


def send_saved_messages(config: dict, texts: list[str]) -> int:
    """Deliver alerts to your own Saved Messages -- no bot involved at all.

    Opening a session is slow, so the whole batch goes out in one connection.
    """
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        print("warn: notify skipped -- pip install telethon", file=sys.stderr)
        return 0
    tg = config.get("telegram", {})
    try:
        with TelegramClient(StringSession(tg.get("session", "")),
                            int(tg.get("api_id")), tg.get("api_hash")) as client:
            sent = 0
            for text in texts:
                client.send_message("me", text[:4096], parse_mode="html")
                sent += 1
                time.sleep(0.4)
            return sent
    except Exception as exc:                      # noqa: BLE001 - never crash the poll
        print(f"warn: saved-messages notify failed -- {exc}", file=sys.stderr)
        return 0


def deliver(config: dict, hits: list[dict]) -> int:
    """Send every match through whichever channel is configured."""
    via = (config.get("notify_via") or "bot").lower()
    bodies = [format_alert(h) for h in hits]
    if via == "saved":
        return send_saved_messages(config, bodies)
    sent = 0
    for body in bodies:
        if send_telegram_dm(config, body):
            sent += 1
            time.sleep(0.4)                       # stay under Telegram's rate cap
    return sent


def brl(value: float) -> str:
    """Format 4299.9 as 'R$ 4.299,90' -- thousands dot, decimal comma."""
    return "R$ " + f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def format_alert(hit: dict) -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rules = ", ".join(sorted({h["rule"] for h in hit["matches"]}))
    terms = ", ".join(sorted({t for h in hit["matches"] for t in h["terms"]}))
    body = hit["text"].strip()
    if len(body) > 900:
        body = body[:900].rstrip() + "..."

    lines = [f"🔔 <b>{esc(rules)}</b>"]
    price = next((h["price"] for h in hit["matches"] if h["price"] is not None), None)
    if price is not None:
        lines.append(f"💰 {brl(price)}")
    lines.append("")
    lines.append(esc(body))
    if hit.get("link"):
        lines.append("")
        lines.append(f'<a href="{esc(hit["link"])}">open in telegram</a>')
    if terms:
        lines.append(f"<i>matched: {esc(terms)}</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_poll(config: dict, args) -> int:
    rules = compile_rules(config)
    state_path = Path(args.state) if args.state else DEFAULT_STATE
    state = {} if args.replay else load_state(state_path)
    seen = state.get("seen", {}) if not args.replay else {}

    if args.replay:
        messages = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        new_state = {}
    else:
        source = (config.get("source") or "user").lower()
        if source == "user":
            messages, new_state = fetch_via_user(config, state)
        elif source == "bot":
            messages, new_state = fetch_via_bot(config, state)
        elif source == "web":
            messages, new_state = fetch_via_web(config, state)
        else:
            sys.exit(f"error: unknown source '{source}' (expected 'user', 'bot', or 'web')")

    # Guard rail: if state was lost, do not replay days of backlog to the phone.
    max_age = int(config.get("max_age_hours", 24))
    floor = time.time() - max_age * 3600 if max_age > 0 else 0

    hits, considered, skipped_old = [], 0, 0
    for msg in messages:
        considered += 1
        if not args.replay:
            if msg.get("ts") and floor and msg["ts"] < floor:
                skipped_old += 1
                continue
            fp = fingerprint(msg)
            if fp in seen:
                continue
        matches = match_rules(msg["text"], rules)
        if not args.replay:
            seen[fingerprint(msg)] = time.time()
        if matches:
            hits.append({**msg, "matches": matches})

    if not args.replay:
        new_state["seen"] = seen
        new_state.setdefault("version", STATE_VERSION)
        if not args.dry_run:
            save_state(state_path, new_state, int(config.get("retain_days", 14)))

    sent = 0
    if args.notify and hits and not args.dry_run:
        sent = deliver(config, hits)

    summary = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "messages_seen": considered,
        "skipped_as_old": skipped_old,
        "match_count": len(hits),
        "notified": sent,
        "matches": [
            {"rule": ", ".join(sorted({m["rule"] for m in h["matches"]})),
             "terms": sorted({t for m in h["matches"] for t in m["terms"]}),
             "price": next((m["price"] for m in h["matches"] if m["price"] is not None), None),
             "text": h["text"][:400],
             "link": h.get("link"),
             "chat": h.get("chat")}
            for h in hits
        ],
    }

    if args.format == "text":
        print(f"checked {considered} message(s) -- {len(hits)} match(es)"
              + (f", {skipped_old} skipped as older than {max_age}h" if skipped_old else ""))
        for m in summary["matches"]:
            price = f"  [{brl(m['price'])}]" if m["price"] is not None else ""
            print(f"\n- {m['rule']}{price}\n  terms: {', '.join(m['terms'])}")
            print("  " + m["text"].replace("\n", "\n  ")[:300])
            if m["link"]:
                print(f"  {m['link']}")
    else:
        print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"match_count={len(hits)}\n")

    return 0


def self_test() -> int:
    """Offline checks -- no network, no config. Exercises the tricky parts."""
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    check("accents", normalize("Promoção ÁGUA"), "promocao agua")
    check("word-guard mid-word", contains_term("netvibes rocks", "tv"), False)
    check("word-guard plural", contains_term("smart tvs on sale", "tv"), True)
    check("word-guard punctuation", contains_term("ps5 slim!", "ps5"), True)
    check("word-guard prefix digits", contains_term("wps5000 router", "ps5"), False)

    check("price br", extract_prices("de R$ 1.234,56 por R$ 899,90"), [899.90, 1234.56])
    check("price us", extract_prices("R$1,234.56"), [1234.56])
    check("price plain", extract_prices("R$ 2500"), [2500.0])
    check("price thousands", extract_prices("R$ 3.499"), [3499.0])
    check("price reais suffix", extract_prices("sai por 149,90 reais"), [149.90])
    check("price none", extract_prices("frete gratis hoje"), [])

    rules = compile_rules({"rules": [
        {"name": "cheap iphone", "any": ["iphone"], "none": ["capinha"], "max_price": 5000},
        {"name": "any tv", "all": ["tv", "55"]},
    ]})
    check("rule fires", [h["rule"] for h in match_rules("iPhone 15 por R$ 4.299,00", rules)],
          ["cheap iphone"])
    check("rule price gate", match_rules("iPhone 15 por R$ 6.000,00", rules), [])
    check("rule exclusion", match_rules("Capinha de iPhone R$ 29,90", rules), [])
    check("rule all-terms", [h["rule"] for h in match_rules("Smart TV 55 polegadas", rules)],
          ["any tv"])
    check("rule all-terms partial", match_rules("Smart TV 43 polegadas", rules), [])
    check("no price = no gate pass", match_rules("iPhone 15 lacrado, chama no pv", rules), [])

    # Accent-insensitive rules
    ac = compile_rules({"rules": [{"name": "fone", "any": ["fone de ouvido"]}]})
    check("accent rule", len(match_rules("FONE DE OUVIDO em promoção", ac)), 1)

    # HTML parsing against a realistic t.me/s/ fragment
    sample = """
    <div class="tgme_widget_message" data-post="promos/101">
      <div class="tgme_widget_message_text js-message_text">
        <b>Smart TV</b> 55" 4K<br>por <i>R$ 2.199,00</i>
      </div>
      <time datetime="2026-08-11T10:00:00+00:00"></time>
    </div>
    <div class="tgme_widget_message" data-post="promos/102">
      <div class="tgme_widget_message_text">Capinha de iPhone &amp; pelicula R$ 19,90</div>
    </div>
    """
    parser = _ChannelParser()
    parser.feed(sample)
    check("html post count", len(parser.posts), 2)
    check("html first id", parser.posts[0]["post"], "promos/101")
    check("html nested tags", "Smart TV" in parser.posts[0]["text"], True)
    check("html br newline", "\n" in parser.posts[0]["text"], True)
    check("html entities", "&" in parser.posts[1]["text"], True)

    # Fingerprint stability: an edit that only changes case/spacing is the same alert
    a = fingerprint({"text": "Smart TV  55  R$ 2.199,00"})
    b = fingerprint({"text": "SMART TV 55 R$ 2.199,00"})
    check("fingerprint normalises", a, b)
    c = fingerprint({"text": "Smart TV 65 R$ 2.199,00"})
    check("fingerprint distinguishes", a != c, True)

    if failures:
        print("SELF-TEST FAILED")
        for f in failures:
            print("  x " + f)
        return 1
    print("self-test: all checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram promo keyword monitor")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--state", default=None)
    ap.add_argument("--notify", action="store_true", help="send matches to your phone")
    ap.add_argument("--dry-run", action="store_true", help="do not write state or notify")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--replay", metavar="FILE", help="run rules against a JSON fixture")
    ap.add_argument("--self-test", action="store_true", help="offline sanity checks")
    ap.add_argument("--github-output", action="store_true", help="emit match_count for Actions")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    config = load_config(Path(args.config))
    return run_poll(config, args)


if __name__ == "__main__":
    sys.exit(main())
