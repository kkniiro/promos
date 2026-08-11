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
    """Whole-word match that still tolerates a plural.

    'tv' matches 'tv' and 'tvs' but not 'netvibes'; 'ps5' matches 'ps5!' but
    not 'wps5000'; 'capa' matches 'capa' and 'capas' but NOT 'capacidade' or
    'capacete'. Guarding only the left side would let a short term swallow any
    longer word starting with it, which silently drops real matches when the
    term is used as an exclusion. Both sides are normalised by the caller.
    """
    term = term.strip()
    if not term:
        return False
    pattern = r"(?<![0-9a-z])" + re.escape(term) + r"(?:es|s)?(?![0-9a-z])"
    return re.search(pattern, haystack) is not None


def contains_loose(haystack: str, term: str) -> bool:
    """Raw substring match, anywhere -- 'bug' also hits 'bugado' and 'bugou'."""
    term = term.strip()
    return bool(term) and term in haystack


# A number only counts as a price when a currency marker sits next to it --
# otherwise "iPhone 15" or "50% off" would read as an amount.
# Handles symbol-first (R$ 1.234,56 / €99,90 / $1,234.56) and symbol-last
# (99,90 € / 149,90 reais / 20 euros).
_CUR_BEFORE = r"r\$|us\$|au\$|ca\$|\$|€|£|brl|usd|eur|gbp"
_CUR_AFTER = r"€|£|\$|reais?|real|euros?|dolares|dollars?|pounds?|conto"
_PRICE_RE = re.compile(
    rf"(?:{_CUR_BEFORE})\s*([0-9][0-9.,\s]*[0-9]|[0-9])"
    rf"|([0-9][0-9.,]*)\s*(?:{_CUR_AFTER})(?![a-z])",
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


_PCT_RE = re.compile(r"(\d{1,3})\s*(?:%|por\s*cento|porcento)")
# "de R$ 199 por R$ 99" -- a discount stated as two prices rather than a %.
_FROM_TO_RE = re.compile(r"\bde\b.{0,60}?\bpor\b", re.DOTALL)


def extract_discounts(text: str) -> list[int]:
    """Explicit discount percentages written in the message."""
    out = []
    for m in _PCT_RE.finditer(text or ""):
        pct = int(m.group(1))
        if 0 < pct <= 100:
            out.append(pct)
    return sorted(out)


def implied_discount(text: str) -> int | None:
    """Discount inferred from a 'de X por Y' price pair, if one is present.

    Requires the de/por wording so two unrelated prices in the same message
    are not read as a markdown.
    """
    if not _FROM_TO_RE.search(normalize(text or "")):
        return None
    # Order matters here, not magnitude: "de X por Y" means X came first.
    # Sorting would read "de R$ 50 por R$ 200" (a rise) as a 75% cut.
    ordered = _prices_in_order(text)
    if len(ordered) < 2:
        return None
    was, now = ordered[0], ordered[1]
    if was <= 0 or now >= was:
        return None
    return int(round((was - now) / was * 100))


def _prices_in_order(text: str) -> list[float]:
    """Every currency-looking number, in the order it appears in the text."""
    found: list[float] = []
    for m in _PRICE_RE.finditer(text or ""):
        value = _to_float(m.group(1) or m.group(2) or "")
        if value is not None and 0 < value < 10_000_000:
            found.append(value)
    return found


def extract_prices(text: str) -> list[float]:
    """Every currency-looking number in the message, cheapest first."""
    return sorted(_prices_in_order(text))


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


# What each delivery channel needs before it can send anything.
CHANNEL_REQUIREMENTS = {
    "ntfy": [("ntfy", "topic")],
    "pushover": [("pushover", "token"), ("pushover", "user_key")],
    "discord": [("discord", "webhook_url")],
    "webhook": [("webhook", "url")],
    "bot": [("telegram", "bot_token"), ("telegram", "notify_chat_id")],
    "saved": [("telegram", "api_id"), ("telegram", "api_hash"), ("telegram", "session")],
}


def validate_config(config: dict) -> None:
    """Catch combinations that cannot work before we hit the network."""
    source = (config.get("source") or "user").lower()
    if source == "web" and not web_channels(config):
        sys.exit("error: source 'web' needs telegram.channels (public @names, without the @)")

    channels = config.get("notify_via") or "ntfy"
    if isinstance(channels, str):
        channels = [channels]
    if not channels:
        sys.exit("error: notify_via is empty -- nothing would ever reach you")

    for raw in channels:
        name = str(raw).lower()
        if name not in NOTIFIERS:
            sys.exit(f"error: unknown notify_via '{raw}' (expected one of "
                     f"{', '.join(sorted(NOTIFIERS))})")
        if name == "saved" and source != "user":
            sys.exit("error: notify_via 'saved' reuses the account session from source "
                     f"'user',\n       but source is '{source}'. Pick another channel "
                     "(ntfy needs no account at all).")
        missing = [f"{sect}.{key}" for sect, key in CHANNEL_REQUIREMENTS[name]
                   if not str((config.get(sect) or {}).get(key) or "").strip()]
        if missing:
            sys.exit(f"error: notify_via '{name}' needs {', '.join(missing)} in config.yaml")


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
            "contains": [normalize(t) for t in (raw.get("contains") or []) if str(t).strip()],
            "max_price": raw.get("max_price"),
            "min_price": raw.get("min_price"),
            "min_discount": raw.get("min_discount"),
            "max_discount": raw.get("max_discount"),
        }
        if not (rule["any"] or rule["all"] or rule["contains"]
                or rule["min_discount"] is not None):
            sys.exit(f"error: rule '{rule['name']}' needs at least one of "
                     "'any', 'all', 'contains' or 'min_discount'")
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
    percentages = extract_discounts(text)
    best_discount = max(percentages) if percentages else implied_discount(text)
    hits = []

    for rule in rules:
        if any(contains_term(hay, t) for t in rule["none"]):
            continue
        matched_all = [t for t in rule["all"] if contains_term(hay, t)]
        if len(matched_all) != len(rule["all"]):
            continue
        matched_any = [t for t in rule["any"] if contains_term(hay, t)]
        matched_loose = [t for t in rule["contains"] if contains_loose(hay, t)]
        # `any` and `contains` are two ways of spelling the same "at least one
        # of these" test, so they pool together.
        if (rule["any"] or rule["contains"]) and not (matched_any or matched_loose):
            continue

        # Price gates only apply when the message actually quotes a price.
        if rule["max_price"] is not None:
            if cheapest is None or cheapest > float(rule["max_price"]):
                continue
        if rule["min_price"] is not None:
            if cheapest is None or cheapest < float(rule["min_price"]):
                continue

        # Same for discount gates: no stated discount means no match.
        if rule["min_discount"] is not None:
            if best_discount is None or best_discount < int(rule["min_discount"]):
                continue
        if rule["max_discount"] is not None:
            if best_discount is None or best_discount > int(rule["max_discount"]):
                continue

        hits.append({
            "rule": rule["name"],
            "terms": sorted(set(matched_any + matched_all + matched_loose)),
            "price": cheapest,
            "discount": best_discount,
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

# Blocks worth reading inside a post. The link preview matters as much as the
# caption: channels that post a teaser ("MOLETOM DE BRUXO") put the actual
# product name and price only in the preview of the link they attach.
_VOID_TAGS = frozenset({"br", "img", "hr", "input", "meta", "link", "source",
                        "area", "base", "col", "embed", "param", "track", "wbr"})

_TEXT_BLOCKS = (
    "tgme_widget_message_text",
    "link_preview_title",
    "link_preview_description",
    "link_preview_site_name",
)


class _ChannelParser(HTMLParser):
    """Pulls message text + permalink out of a t.me/s/<channel> page.

    Telegram renders each post as
      <div class="tgme_widget_message" data-post="chan/123">
        <div class="tgme_widget_message_text">caption</div>
        <a class="tgme_widget_message_link_preview">
          <div class="link_preview_title">product name</div> ...
        <time datetime="...">

    Everything after the caption -- preview, timestamp -- belongs to the same
    post, so fragments are accumulated per data-post id and the list is
    materialised at the end. Collecting as we go and emitting at the caption's
    closing tag would drop each of those, since they are parsed later.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._entries: dict[str, dict] = {}
        self._order: list[str] = []
        self._post: str | None = None
        self._depth = 0
        self._buf: list[str] = []

    def _entry(self, post_id):
        if post_id not in self._entries:
            self._entries[post_id] = {"post": post_id, "parts": [], "time": None}
            self._order.append(post_id)
        return self._entries[post_id]

    @property
    def posts(self) -> list[dict]:
        out = []
        for post_id in self._order:
            entry = self._entries[post_id]
            seen, parts = set(), []
            for part in entry["parts"]:
                # A preview often repeats the caption; keep it once.
                if part and part not in seen:
                    seen.add(part)
                    parts.append(part)
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
            if text:
                out.append({"post": post_id, "text": text, "time": entry["time"]})
        return out

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "tgme_widget_message" in classes and attrs.get("data-post"):
            self._post = attrs["data-post"]
            self._entry(self._post)
        if tag == "time" and attrs.get("datetime") and self._post:
            entry = self._entry(self._post)
            entry["time"] = entry["time"] or attrs["datetime"]
        if self._depth:
            # Already inside a text block: keep track of nesting.
            if tag not in _VOID_TAGS:
                self._depth += 1
            if tag == "br":
                self._buf.append("\n")
            return
        if any(c in classes for c in _TEXT_BLOCKS):
            self._depth = 1
            self._buf = []

    def handle_startendtag(self, tag, attrs):
        # "<br/>" would otherwise run starttag AND endtag, and the endtag would
        # close the text block at the first line break -- truncating every
        # multi-line promo to its first line.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return                      # never opened a level, must not close one
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = "".join(self._buf).strip()
                if text and self._post:
                    self._entry(self._post)["parts"].append(text)
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


def web_channels(config: dict) -> list[str]:
    """Public channels to read. Accepts `channels: [...]` or a single `channel`."""
    tg = config.get("telegram") or {}
    raw = tg.get("channels")
    if raw is None:
        raw = [tg.get("channel")] if tg.get("channel") else []
    if isinstance(raw, str):
        raw = [raw]
    return [str(c).strip().lstrip("@") for c in raw if str(c or "").strip()]


def _fetch_one_channel(channel: str) -> list[dict]:
    url = f"https://t.me/s/{urllib.parse.quote(channel)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=40) as resp:
        html_text = resp.read().decode("utf-8", "replace")

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
    return messages


def fetch_via_web(config: dict, state: dict) -> tuple[list[dict], dict]:
    channels = web_channels(config)
    if not channels:
        sys.exit("error: source 'web' needs telegram.channels (public @names, without the @)")

    messages: list[dict] = []
    failures = []
    for channel in channels:
        try:
            got = _fetch_one_channel(channel)
            messages.extend(got)
            print(f"  {channel}: {len(got)} message(s)", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            failures.append(f"{channel} ({exc.code}) -- is it public?")
        except urllib.error.URLError as exc:
            failures.append(f"{channel} ({exc.reason})")

    for f in failures:
        # One dead channel must not stop the others from being checked.
        print(f"warn: could not read {f}", file=sys.stderr)
    if failures and not messages:
        sys.exit(f"error: every channel failed: {'; '.join(failures)}")

    messages.sort(key=lambda m: m.get("ts") or 0)
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


def _http_post(url: str, payload: dict | None = None, data: bytes | None = None,
               headers: dict | None = None, timeout: int = 20) -> bool:
    """POST JSON (or raw bytes) and report whether it was accepted."""
    hdrs = {"User-Agent": USER_AGENT}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data or b"", headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        print(f"warn: POST {urllib.parse.urlparse(url).netloc} -> {exc.code}: {body}",
              file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"warn: POST {urllib.parse.urlparse(url).netloc} failed: {exc.reason}",
              file=sys.stderr)
    return False


def build_alert(hit: dict, symbol: str = "R$", style: str = "eu") -> dict:
    """Channel-neutral view of a match; each notifier renders this its own way."""
    rules = ", ".join(sorted({h["rule"] for h in hit["matches"]}))
    terms = sorted({t for h in hit["matches"] for t in h["terms"]})
    price = next((h["price"] for h in hit["matches"] if h["price"] is not None), None)
    discount = next((h.get("discount") for h in hit["matches"]
                     if h.get("discount") is not None), None)
    body = hit["text"].strip()
    if len(body) > 900:
        body = body[:900].rstrip() + "..."

    bits = [rules]
    if price is not None:
        bits.append(money(price, symbol, style))
    if discount is not None:
        bits.append(f"-{discount}%")
    title = " · ".join(bits)
    return {"title": title, "rules": rules, "terms": terms, "price": price,
            "discount": discount, "body": body, "url": hit.get("link"),
            "chat": hit.get("chat")}


def body_with_source(alert: dict) -> str:
    """Message text plus which channel it came from -- matters once >1 is watched."""
    if not alert.get("chat"):
        return alert["body"]
    return f"{alert['body']}\n\n— @{alert['chat']}"


def notify_ntfy(config: dict, alerts: list[dict]) -> int:
    """ntfy.sh -- free, no account. A dedicated app you can leave unmuted."""
    cfg = config.get("ntfy") or {}
    topic = (cfg.get("topic") or "").strip()
    if not topic:
        print("warn: notify skipped -- set ntfy.topic", file=sys.stderr)
        return 0
    server = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
    priority = int(cfg.get("priority", 4))
    token = (cfg.get("token") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    sent = 0
    for a in alerts:
        payload = {
            "topic": topic,
            "title": a["title"][:200],
            "message": body_with_source(a),
            "priority": priority,
            "tags": ["moneybag"],
        }
        if a["url"]:
            payload["click"] = a["url"]
        if _http_post(server, payload=payload, headers=headers):
            sent += 1
            time.sleep(0.2)
    return sent


def notify_pushover(config: dict, alerts: list[dict]) -> int:
    cfg = config.get("pushover") or {}
    token, user = (cfg.get("token") or "").strip(), (cfg.get("user_key") or "").strip()
    if not token or not user:
        print("warn: notify skipped -- set pushover.token and pushover.user_key",
              file=sys.stderr)
        return 0
    sent = 0
    for a in alerts:
        form = {"token": token, "user": user, "title": a["title"][:250],
                "message": body_with_source(a)[:1024],
                "priority": int(cfg.get("priority", 0))}
        if a["url"]:
            form["url"] = a["url"]
            form["url_title"] = "open in telegram"
        if _http_post("https://api.pushover.net/1/messages.json",
                      data=urllib.parse.urlencode(form).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"}):
            sent += 1
            time.sleep(0.2)
    return sent


def notify_discord(config: dict, alerts: list[dict]) -> int:
    """Post to a Discord webhook -- useful since per-server mute is separate."""
    url = ((config.get("discord") or {}).get("webhook_url") or "").strip()
    if not url:
        print("warn: notify skipped -- set discord.webhook_url", file=sys.stderr)
        return 0
    sent = 0
    for a in alerts:
        embed = {"title": a["title"][:250], "description": body_with_source(a)[:4000],
                 "color": 0x2ECC71}
        if a["url"]:
            embed["url"] = a["url"]
        if _http_post(url, payload={"embeds": [embed]}):
            sent += 1
            time.sleep(0.3)
    return sent


def notify_webhook(config: dict, alerts: list[dict]) -> int:
    """Generic JSON POST -- Slack, Zapier, Home Assistant, your own endpoint."""
    cfg = config.get("webhook") or {}
    url = (cfg.get("url") or "").strip()
    if not url:
        print("warn: notify skipped -- set webhook.url", file=sys.stderr)
        return 0
    headers = cfg.get("headers") or {}
    sent = 0
    for a in alerts:
        if _http_post(url, payload=a, headers=headers):
            sent += 1
            time.sleep(0.2)
    return sent


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


def notify_telegram(config: dict, alerts: list[dict]) -> int:
    sent = 0
    for a in alerts:
        if send_telegram_dm(config, render_telegram(a)):
            sent += 1
            time.sleep(0.4)                       # stay under Telegram's rate cap
    return sent


def notify_saved(config: dict, alerts: list[dict]) -> int:
    return send_saved_messages(config, [render_telegram(a) for a in alerts])


NOTIFIERS = {
    "ntfy": notify_ntfy,
    "pushover": notify_pushover,
    "discord": notify_discord,
    "webhook": notify_webhook,
    "bot": notify_telegram,
    "saved": notify_saved,
}


def deliver(config: dict, hits: list[dict]) -> int:
    """Fan every match out to each configured channel.

    Returns the number of alerts delivered to at least one channel, so two
    channels for one promo counts once. A channel that fails does not stop
    the others.
    """
    alerts = [build_alert(h, config.get("currency_symbol", "R$"),
                          config.get("currency_style", "eu")) for h in hits]
    channels = config.get("notify_via") or "ntfy"
    if isinstance(channels, str):
        channels = [channels]

    best = 0
    for name in channels:
        fn = NOTIFIERS.get(str(name).lower())
        if fn is None:
            print(f"warn: unknown notify_via '{name}' -- expected one of "
                  f"{', '.join(sorted(NOTIFIERS))}", file=sys.stderr)
            continue
        best = max(best, fn(config, alerts))
    return best


def money(value: float, symbol: str = "R$", style: str = "eu") -> str:
    """Format 4299.9 as 'R$ 4.299,90' (eu) or '$ 4,299.90' (us)."""
    formatted = f"{value:,.2f}"
    if style == "eu":   # swap separators: 4,299.90 -> 4.299,90
        formatted = formatted.replace(",", "@").replace(".", ",").replace("@", ".")
    return f"{symbol} {formatted}"


# Kept so existing callers and tests keep working.
def brl(value: float) -> str:
    return money(value)


def render_telegram(alert: dict) -> str:
    """Telegram HTML rendering of a channel-neutral alert."""
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = [f"🔔 <b>{esc(alert['rules'])}</b>"]
    if alert.get("price") is not None:
        lines.append(f"💰 {alert['title'].split(' - ', 1)[-1]}")
    lines.append("")
    lines.append(esc(body_with_source(alert)))
    if alert.get("url"):
        lines.append("")
        lines.append(f'<a href="{esc(alert["url"])}">open in telegram</a>')
    if alert.get("terms"):
        lines.append(f"<i>matched: {esc(', '.join(alert['terms']))}</i>")
    return "\n".join(lines)


def format_alert(hit: dict, symbol: str = "R$", style: str = "eu") -> str:
    """Convenience wrapper: raw match -> Telegram HTML."""
    return render_telegram(build_alert(hit, symbol, style))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_poll(config: dict, args) -> int:
    validate_config(config)
    rules = compile_rules(config)
    symbol = config.get("currency_symbol", "R$")
    style = config.get("currency_style", "eu")
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

    if getattr(args, "dump", False):
        # Diagnostic: tells "nothing matched" apart from "nothing was read".
        print(f"fetched {len(messages)} message(s) from "
              f"{', '.join(web_channels(config)) or config.get('source')}\n")
        for m in messages:
            when = (datetime.fromtimestamp(m["ts"], timezone.utc).isoformat(timespec="minutes")
                    if m.get("ts") else "no timestamp")
            snippet = " ".join((m.get("text") or "").split())[:160]
            hits = match_rules(m.get("text") or "", rules)
            flag = "MATCH " + ",".join(h["rule"] for h in hits) if hits else "-"
            print(f"[{when}] {flag}\n  {snippet}\n")
        return 0

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
             "discount": next((m.get("discount") for m in h["matches"]
                               if m.get("discount") is not None), None),
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
            tags = []
            if m["price"] is not None:
                tags.append(money(m["price"], symbol, style))
            if m.get("discount") is not None:
                tags.append(f"-{m['discount']}%")
            label = f"  [{' · '.join(tags)}]" if tags else ""
            print(f"\n- {m['rule']}{label}")
            if m["terms"]:            # discount-only rules match no keywords
                print(f"  terms: {', '.join(m['terms'])}")
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
    # Guarding only the left side let a short term swallow any longer word
    # starting with it -- which silently killed real matches via exclusions.
    check("word-guard capa/capacidade", contains_term("iphone 256gb capacidade", "capa"), False)
    check("word-guard capa/capacete", contains_term("capacete de moto", "capa"), False)
    check("word-guard capa/capas", contains_term("duas capas novas", "capa"), True)
    check("word-guard free/freezer", contains_term("freezer novo", "free"), False)
    check("word-guard free alone", contains_term("brinde free hoje", "free"), True)
    check("word-guard plural es", contains_term("promocoes do dia", "promocao"), False)
    check("word-guard camera plural", contains_term("cameras gopro", "camera"), True)

    check("loose matches inside word", contains_loose("produto bugado", "bug"), True)
    check("loose matches exact", contains_loose("bug de preco", "bug"), True)
    check("loose absent", contains_loose("promocao boa", "bug"), False)

    check("pct explicit", extract_discounts("50% OFF hoje"), [50])
    check("pct multiple", extract_discounts("de 20% ate 70% off"), [20, 70])
    check("pct words", extract_discounts("80 por cento de desconto"), [80])
    check("pct over 100 ignored", extract_discounts("120% de aumento"), [])
    check("pct implied", implied_discount("de R$ 200,00 por R$ 50,00"), 75)
    check("pct implied needs marker", implied_discount("R$ 200,00 R$ 50,00"), None)
    check("pct implied needs drop", implied_discount("de R$ 50,00 por R$ 200,00"), None)

    check("price br", extract_prices("de R$ 1.234,56 por R$ 899,90"), [899.90, 1234.56])
    check("price us", extract_prices("R$1,234.56"), [1234.56])
    check("price plain", extract_prices("R$ 2500"), [2500.0])
    check("price thousands", extract_prices("R$ 3.499"), [3499.0])
    check("price reais suffix", extract_prices("sai por 149,90 reais"), [149.90])
    check("price none", extract_prices("frete gratis hoje"), [])

    # Other currencies -- the channel may not price in BRL.
    check("price eur symbol", extract_prices("seulement 29,99€"), [29.99])
    check("price eur before", extract_prices("€ 1.499,00"), [1499.0])
    check("price usd", extract_prices("only $1,234.56"), [1234.56])
    check("price gbp", extract_prices("£99.99 today"), [99.99])
    check("price word eur", extract_prices("a 20 euros"), [20.0])
    check("price cheapest wins", extract_prices("de 199€ por 99€"), [99.0, 199.0])
    # A bare number or a percentage must never read as a price.
    check("no bare number", extract_prices("iPhone 15 Pro Max 256"), [])
    check("no percentage", extract_prices("50% de desconto"), [])
    check("no word glued", extract_prices("123 realmente barato"), [])

    check("money eu", money(4299.9, "R$", "eu"), "R$ 4.299,90")
    check("money us", money(4299.9, "$", "us"), "$ 4,299.90")
    check("money eur", money(29.99, "€", "eu"), "€ 29,99")

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

    # contains + discount gates
    dr = compile_rules({"rules": [
        {"name": "bug", "contains": ["bug"]},
        {"name": "big sale", "min_discount": 50},
        {"name": "gopro", "any": ["gopro", "go pro"]},
    ]})
    check("contains fires mid-word",
          [h["rule"] for h in match_rules("Produto bugado na Amazon", dr)], ["bug"])
    check("discount explicit",
          [h["rule"] for h in match_rules("70% OFF em tudo", dr)], ["big sale"])
    check("discount below floor", match_rules("20% OFF em tudo", dr), [])
    check("discount implied",
          [h["rule"] for h in match_rules("de R$ 400,00 por R$ 100,00", dr)], ["big sale"])
    check("discount reported", match_rules("70% OFF", dr)[0]["discount"], 70)
    check("no discount stated", match_rules("Camisa por R$ 50,00", dr), [])
    check("gopro one word", [h["rule"] for h in match_rules("GoPro Hero 12", dr)], ["gopro"])
    check("gopro two words", [h["rule"] for h in match_rules("Go Pro Hero 12", dr)], ["gopro"])
    # A rule may gate purely on discount, with no keywords at all.
    check("discount-only rule compiles", len(dr), 3)

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

    # A real multi-line promo. "<br/>" is self-closing, which HTMLParser turns
    # into starttag+endtag -- if the endtag closes the block, everything after
    # the first line is lost, including the product name and the price.
    multiline = (
        '<div class="tgme_widget_message" data-post="loba/1">'
        '<div class="tgme_widget_message_text">CARGA NOVA PRA N&Atilde;O RASPAR NO SECO'
        '<br/><br/>\U0001f9f4 Gillette Mach3 Aparelho + 01 Carga (3 unidades)'
        '<br/>\U0001f4b0 R$ 37,94 &agrave; vista'
        '<br/>- Adicione 3 unidades ao carrinho'
        '<br/>- Desconto aplicado na finaliza&ccedil;&atilde;o da compra<br/><br/>'
        '<a href="https://amzlink.to/az0HrkBbre3o3">https://amzlink.to/az0HrkBbre3o3</a>'
        '</div><time datetime="2026-08-11T09:08:00+00:00"></time></div>'
    )
    ml = _ChannelParser()
    ml.feed(multiline)
    check("multiline post count", len(ml.posts), 1)
    body = ml.posts[0]["text"]
    check("multiline keeps first line", "CARGA NOVA" in body, True)
    check("multiline keeps product", "Gillette Mach3" in body, True)
    check("multiline keeps price", extract_prices(body), [37.94])
    check("multiline keeps link", "amzlink.to" in body, True)
    check("multiline keeps time", ml.posts[0]["time"], "2026-08-11T09:08:00+00:00")

    # A teaser caption whose product name lives only in the link preview.
    teaser = """
    <div class="tgme_widget_message" data-post="loba/9">
      <div class="tgme_widget_message_text">MOLETOM DE BRUXO E DESCONTO DE TROUXA</div>
      <a class="tgme_widget_message_link_preview" href="https://x">
        <div class="link_preview_site_name">Amazon</div>
        <div class="link_preview_title">Monitor Gamer 27 165Hz</div>
        <div class="link_preview_description">Por R$ 899,00 a vista</div>
      </a>
      <time datetime="2026-08-11T11:00:00+00:00"></time>
    </div>
    """
    tp = _ChannelParser()
    tp.feed(teaser)
    check("preview post count", len(tp.posts), 1)
    check("preview keeps caption", "MOLETOM" in tp.posts[0]["text"], True)
    check("preview adds product", "Monitor Gamer 27" in tp.posts[0]["text"], True)
    check("preview adds price", extract_prices(tp.posts[0]["text"]), [899.0])
    check("preview time", tp.posts[0]["time"], "2026-08-11T11:00:00+00:00")
    # A message with only a preview and no caption must still be produced.
    only = _ChannelParser()
    only.feed('<div class="tgme_widget_message" data-post="loba/10">'
              '<div class="link_preview_title">SSD 1TB</div></div>')
    check("preview-only post", only.posts[0]["text"], "SSD 1TB")
    # Empty messages (media with no text at all) must not appear.
    none_ = _ChannelParser()
    none_.feed('<div class="tgme_widget_message" data-post="loba/11"></div>')
    check("empty message dropped", none_.posts, [])
    # A preview that merely repeats the caption should not double up.
    dup = _ChannelParser()
    dup.feed('<div class="tgme_widget_message" data-post="loba/12">'
             '<div class="tgme_widget_message_text">SSD 1TB</div>'
             '<div class="link_preview_title">SSD 1TB</div></div>')
    check("duplicate preview collapsed", dup.posts[0]["text"], "SSD 1TB")
    check("html post count", len(parser.posts), 2)
    check("html first id", parser.posts[0]["post"], "promos/101")
    # <time> sits after the text block, so this only passes if it is backfilled.
    # Without a timestamp the max_age_hours flood guard silently does nothing.
    check("html timestamp captured", parser.posts[0]["time"], "2026-08-11T10:00:00+00:00")
    check("html missing time stays none", parser.posts[1]["time"], None)
    check("html nested tags", "Smart TV" in parser.posts[0]["text"], True)
    check("html br newline", "\n" in parser.posts[0]["text"], True)
    check("html entities", "&" in parser.posts[1]["text"], True)

    # Channel list accepts a list, a bare string, or the old singular key.
    check("channels list", web_channels({"telegram": {"channels": ["a", "@b"]}}), ["a", "b"])
    check("channels string", web_channels({"telegram": {"channels": "solo"}}), ["solo"])
    check("channels legacy key", web_channels({"telegram": {"channel": "@old"}}), ["old"])
    check("channels blank dropped",
          web_channels({"telegram": {"channels": ["a", "", None]}}), ["a"])
    check("channels none", web_channels({"telegram": {}}), [])

    # Source channel is shown on the alert, so two channels stay tellable apart.
    src = build_alert({"text": "SSD 1TB", "link": None, "chat": "lobaopromo",
                       "matches": [{"rule": "SSD", "terms": ["ssd"], "price": None}]})
    check("body names channel", body_with_source(src).endswith("— @lobaopromo"), True)
    check("body without channel",
          body_with_source({"body": "x", "chat": None}), "x")

    # The new keywords, against text taken from the real channel.
    kw = compile_rules({"rules": [
        {"name": "Monitor", "any": ["monitor"], "none": ["suporte", "braco", "cabo"]},
        {"name": "SSD", "any": ["ssd", "nvme"]},
    ]})
    check("monitor real", [h["rule"] for h in
                           match_rules("🔥 Monitor Gamer Haiz 27\" IPS 240Hz", kw)], ["Monitor"])
    check("monitor plural", len(match_rules("Monitores em promocao", kw)), 1)
    check("monitor excluded", match_rules("Suporte para monitor 27", kw), [])
    check("ssd real", [h["rule"] for h in
                       match_rules("SSD Kingston NV3 1TB PCIe 4.0", kw)], ["SSD"])
    check("ssd plural", len(match_rules("SSDs em oferta", kw)), 1)
    check("ssd not midword", match_rules("wssd generico", kw), [])

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
    ap.add_argument("--test-alert", action="store_true",
                    help="send one fake alert to confirm delivery is wired up")
    ap.add_argument("--dump", action="store_true",
                    help="print what was actually fetched, to check the reader works")
    ap.add_argument("--github-output", action="store_true", help="emit match_count for Actions")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    config = load_config(Path(args.config))

    if args.test_alert:
        validate_config(config)
        fake = {
            "text": "Teste do monitor: Smart TV 55\" 4K por R$ 1.999,00\n"
                    "Se você recebeu isto no celular, está tudo funcionando.",
            "link": f"https://t.me/s/{config.get('telegram', {}).get('channel', '')}",
            "chat": "promo-monitor",
            "matches": [{"rule": "test alert", "terms": ["teste"], "price": 1999.0}],
        }
        channels = config.get("notify_via") or "ntfy"
        channels = [channels] if isinstance(channels, str) else channels
        sent = deliver(config, [fake])
        if sent:
            print(f"sent a test alert via {', '.join(map(str, channels))} -- "
                  "check your phone")
            return 0
        print("test alert was NOT delivered -- see the warnings above", file=sys.stderr)
        return 1

    try:
        return run_poll(config, args)
    except BrokenPipeError:
        # Someone piped us into `head`; that is not an error worth a traceback.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
