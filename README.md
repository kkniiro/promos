# promo monitor

Watches a Telegram promo group and pushes a notification to your phone only
when a message matches keywords you care about.

```
Telegram group ──▶ monitor.py ──▶ keyword rules ──▶ your phone
                   (every 10 min)   (+ price gates)   (Telegram DM)
```

---

## Why Telegram

The same group exists on WhatsApp, Telegram, and Discord. Telegram is by a wide
margin the easiest to read programmatically:

| | effort | notes |
|---|---|---|
| **Telegram** | **low** | Free Bot API, no review process. Public channels readable with no auth at all. Private groups need one bot added to them. |
| Discord | medium | Needs a bot application, a server admin to invite it, and a gateway connection or REST polling. Only workable if you administer the server. |
| WhatsApp | high | No official API reads group messages. The Business API does not cover groups. Unofficial libraries drive a real logged-in session, risk a ban, and need a phone paired 24/7. |

Telegram also doubles as the delivery channel: the same bot DMs you the alert,
so your phone buzzes natively with no extra app to install.

---

## Setup (~10 minutes)

### 1. Make a bot

In Telegram, message **@BotFather** → `/newbot` → follow the prompts. Copy the
token it gives you (`123456789:AAF...`).

### 2. Let the bot read the group

- **Private group:** add the bot as a member, then turn group privacy **off** —
  @BotFather → `/mybots` → your bot → *Bot Settings* → *Group Privacy* → *Turn off*.
  Without this the bot only sees messages that @mention it.
- **Public channel:** nothing to do. Set `source: web` and `telegram.channel` in
  the config and skip the bot entirely for reading (you still want the bot for
  delivering alerts to your phone).

### 3. Find your chat ids

DM your bot anything, post any message in the group, then:

```bash
export TELEGRAM_BOT_TOKEN=123456789:AAF...
python3 tools/whoami.py
```

It prints the group's `chat_id` and your personal `notify_chat_id`.

### 4. Fill in the config

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml          # set chat_id, and your rules
```

### 5. Add the secrets

Repo → *Settings* → *Secrets and variables* → *Actions* → *New repository secret*:

| secret | value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the @BotFather token |
| `TELEGRAM_NOTIFY_CHAT_ID` | your personal chat id from step 3 |

### 6. Turn it on

The workflow runs every 10 minutes — **but GitHub only runs scheduled workflows
on the default branch**, so merge this to `main` first. Before merging you can
test it any time from the *Actions* tab → *promo monitor* → *Run workflow*
(tick *dry run* to check matches without sending anything).

---

## Writing rules

A message alerts if it satisfies **any** rule. Within a rule:

| key | meaning |
|---|---|
| `any` | at least one of these terms must appear |
| `all` | every one of these terms must appear |
| `none` | if any of these appear, skip the message |
| `max_price` | only alert if the cheapest price found is at or below this |
| `min_price` | only alert if the cheapest price found is at or above this |

```yaml
rules:
  - name: "iPhone"
    any: ["iphone", "apple"]
    none: ["capinha", "pelicula", "cabo"]
    max_price: 5000
```

Matching details worth knowing:

- **Accents and case are ignored** — `promocao` matches `Promoção`.
- **Terms will not match mid-word** — `tv` matches `TV` and `TVs`, but not
  `netvibes`. `ps5` matches `ps5!` but not `wps5000`.
- **Prices** are read in Brazilian or plain format: `R$ 1.234,56`, `R$1234.56`,
  `2500`, `149,90 reais`. When several appear, the **cheapest** is used, so
  "de R$ 5.999 por R$ 4.299" is judged on the 4.299.
- **A price gate skips messages with no price at all.** A rule with `max_price`
  will not fire on "iPhone 15 lacrado, chama no pv". Drop the gate if you want
  those.

Check your rules against the bundled sample messages without touching the
network or your state:

```bash
python3 monitor.py --replay fixtures/sample_messages.json --format text
```

---

## Not getting alerted twice

Every message is fingerprinted by its normalised text, so a repost or an edit
that only changes capitalisation is not a second alert. Fingerprints live in
`state/seen.json`, kept in the Actions cache between runs (not committed, so the
repo does not collect a commit every 10 minutes).

If that cache is ever evicted, `max_age_hours: 24` stops an empty state from
replaying the entire backlog to your phone — worst case you miss nothing newer
than a day and get no flood.

---

## Running it

```bash
python3 monitor.py                  # poll, print JSON, update state
python3 monitor.py --notify         # ...and push matches to your phone
python3 monitor.py --format text    # human-readable
python3 monitor.py --dry-run        # touch nothing, send nothing
python3 monitor.py --self-test      # offline checks, no config needed
python3 tests/test_dedupe.py        # state/dedupe integration checks
```

---

## Running it from Claude instead of GitHub Actions

A Claude Routine can poll on a schedule and push to the Claude mobile app
instead. One prerequisite: **Claude's sandbox blocks `api.telegram.org` and
`t.me` by default** under this environment's network egress policy, so a Routine
cannot reach Telegram until those hosts are allowlisted in the environment's
network settings (see
<https://code.claude.com/docs/en/claude-code-on-the-web>).

Trade-offs versus the GitHub Actions setup:

| | GitHub Actions | Claude Routine |
|---|---|---|
| fastest cadence | ~10 min | 1 hour |
| network setup | none | must allowlist Telegram hosts |
| notification | Telegram DM (native push) | Claude app push |
| runs when | always | always |

The engine is identical either way — only the thing calling it changes.

---

## Files

| path | what it is |
|---|---|
| `monitor.py` | the whole engine: fetch, match, dedupe, notify. Stdlib only. |
| `config.yaml` | your group and your rules |
| `tools/whoami.py` | prints the chat ids you need during setup |
| `tests/test_dedupe.py` | offline checks for state, dedupe, and the age guard |
| `fixtures/` | sample messages for `--replay` |
| `.github/workflows/` | the 10-minute poller |
