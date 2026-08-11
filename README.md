# promo monitor

Watches a Telegram promo group and pushes a notification to your phone only
when a message matches keywords you care about.

```
Telegram group ──▶ monitor.py ──▶ keyword rules ──▶ your phone
                   (every 10 min)   (+ price gates)   (Telegram push)
```

---

## Why Telegram

The same group exists on WhatsApp, Telegram, and Discord. Telegram is by a wide
margin the easiest to read programmatically:

| | effort | notes |
|---|---|---|
| **Telegram** | **low** | Public channels readable with no auth at all. For a group you are only a member of, your own account can read it. |
| Discord | medium | Needs a bot application **and a server admin to invite it**. Not an option unless you run the server. |
| WhatsApp | high | No official API reads group messages. The Business API does not cover groups. Unofficial libraries drive a real logged-in session, risk a ban, and need a phone paired 24/7. |

---

## Pick your path

**You cannot add a bot to a group you do not administer** — that needs admin
rights. So there are two ways in, and which one applies depends on the group:

### Path A — public channel (easiest by far)

If the group has an `@name` and its link opens a readable history without
joining, it is a public channel. Then you need **no credentials at all** — the
monitor just reads `https://t.me/s/<name>`.

```yaml
source: web
telegram:
  channel: "nomedocanal"     # no @
```

Nothing else to set up. Skip to [step 3](#3-choose-how-alerts-reach-you).

### Path B — private or members-only group

Read it through **your own account**, which already receives these messages.
This uses [Telethon](https://docs.telethon.dev) and needs a one-time login.

> **Worth knowing:** automating a user account is a grey area in Telegram's
> terms. Read-only polling on a 10-minute interval is gentle and low-risk, but
> the account is yours and the risk is not literally zero. If Path A is
> available, prefer it.

#### B1. Get API credentials

<https://my.telegram.org> → *API development tools* → note `api_id` and `api_hash`.

#### B2. Log in once, on your own machine

Not in CI — it asks for the code Telegram sends you.

```bash
pip install telethon
export TELEGRAM_API_ID=1234567
export TELEGRAM_API_HASH=abcdef...
python3 tools/login.py
```

It prints a **session string** and lists every group you are in with its id:

```
  -1001234567890  [group  ] private              Promoções BR
   -1009876543210  [channel] @ofertasbr          Ofertas BR
```

Put the promo group's id in `config.yaml` as `telegram.chat_id`. If the group
you want shows up as a `channel` with an `@name`, use Path A instead.

> The session string is **as sensitive as your password**. It goes in a GitHub
> secret, never in a file you commit.

---

## 3. Choose how alerts reach you

| `notify_via` | what happens | needs |
|---|---|---|
| `saved` | the alert lands in your own Telegram **Saved Messages** | nothing extra (Path B only) |
| `bot` | your own bot DMs you — a separate chat, so alerts stay out of your notes | a bot from @BotFather, and you send it one message. **You do not need to add it to any group.** |

Either way your phone buzzes through Telegram itself, so there is no extra app.

For `bot`, create it with **@BotFather** → `/newbot`, send it any DM, then run
`python3 tools/whoami.py` to get your `notify_chat_id`.

---

## 4. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml          # source, chat_id/channel, notify_via, and your rules
```

## 5. Add the secrets

Repo → *Settings* → *Secrets and variables* → *Actions*:

| secret | needed for |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Path B |
| `TELEGRAM_SESSION` | Path B (from `tools/login.py`) |
| `TELEGRAM_BOT_TOKEN` | only if `notify_via: bot` |
| `TELEGRAM_NOTIFY_CHAT_ID` | only if `notify_via: bot` |

Path A with `notify_via: saved` needs none of these.

## 6. Turn it on

The workflow runs every 10 minutes — **but GitHub only runs scheduled workflows
on the default branch**, so merge this to `main` first. Before merging, test it
from the *Actions* tab → *promo monitor* → *Run workflow* (tick *dry run* to see
matches without sending anything).

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

A Claude Routine can poll on a schedule and push to the Claude mobile app.
One prerequisite: **Claude's sandbox blocks `api.telegram.org` and `t.me`** under
this environment's network egress policy, so a Routine cannot reach Telegram
until those hosts are allowlisted in the environment's network settings (see
<https://code.claude.com/docs/en/claude-code-on-the-web>).

| | GitHub Actions | Claude Routine |
|---|---|---|
| fastest cadence | ~10 min | 1 hour |
| network setup | none | must allowlist Telegram hosts |
| notification | Telegram push | Claude app push |

The engine is identical either way — only the caller changes. Do not run both
against the same group: they keep separate state and would each alert you.

---

## Files

| path | what it is |
|---|---|
| `monitor.py` | the whole engine: fetch, match, dedupe, notify |
| `config.yaml` | your group and your rules |
| `tools/login.py` | one-time account login (Path B); lists your groups and ids |
| `tools/whoami.py` | bot setup helper; prints your `notify_chat_id` |
| `tests/test_dedupe.py` | offline checks for state, dedupe, and the age guard |
| `fixtures/` | sample messages for `--replay` |
| `.github/workflows/` | the 10-minute poller |
