# Soccer Goal Notifier -- Setup Guide

You need a FiestaBoard (v9.13.0 or later) and a free
[sportapi.ai](https://sportapi.ai) account. No card is needed for the free
plan (2,000 requests a day, 14,000 a month), which is plenty: this plugin
uses about 20–30 requests on a match day.

## How to read the commands

**Replace every word that starts with `YOUR_`; copy everything else exactly
as written.** Paste only the value itself: no `< >`, no quotes.

## 1. Get an API key

1. Sign up at <https://sportapi.ai/dashboard>.
2. Create a key. It starts with `sk_live_`.

Keep the key out of chat, commits and screenshots. If you want a local copy
(e.g. to test from your Mac), keep it in this repo's git-ignored `secrets/`
folder, typed in through a hidden prompt so it never lands in your shell
history:

```bash
mkdir -p secrets && chmod 700 secrets
read -s -p "sportapi.ai key: " KEY && printf '%s' "$KEY" > secrets/sportsapi-key.txt && unset KEY
chmod 600 secrets/sportsapi-key.txt
```

**Copy as-is.** Paste the key at the prompt (nothing is shown) and press
Enter.

## 2. Check a team slug (optional)

Teams are given as sportapi.ai *slugs*: `mechelen`, `barcelona`,
`racing-genk`, `bayern-munich`. To check one with the key from step 1:

```bash
curl -s -H "X-Api-Key: $(cat secrets/sportsapi-key.txt)" \
  https://sportapi.ai/api/teams/slug/YOUR_TEAM_SLUG | head -c 300; echo
```

**Replace:** `YOUR_TEAM_SLUG`, e.g. `mechelen`. A known team answers with
`"success":true` and the team's name; an unknown one with
`{"success":false}`.

## 3. Install the plugin in FiestaBoard

1. In FiestaBoard, open **Integrations** and add the plugin from this
   repository's URL (or copy the repo to
   `data/external_plugins/soccer_goals/` on the Pi and restart).
2. Open **Soccer Goal Notifier** settings:
   - **sportapi.ai API Key**: paste your key.
   - **Teams**: e.g. `mechelen, barcelona`.
   - **Goals Shown**: `all` (also goals against your teams) or `selected`
     (only your teams' goals).
   - **Trigger Duration**: how long a goal stays up (default 60 s).
3. Enable the plugin and save. A team sportapi.ai does not know is reported
   on the next save and in the logs.

The board shows the next watched match on a page that uses the plugin
(`NEXT MATCH / BARCELONA - ALAVES / SAT 31 OCT 21:00`), and a goal as soon
as it is pushed:

```
GOAL!
MECHELEN 1-0 GENK
S. MICHEZ 22'
```

## 4. Check it live

During a match of one of your teams, follow FiestaBoard's log:

```bash
docker compose logs -f | grep -i soccer
```

**Copy as-is.** You should see `Soccer Goals listener started`, and after a
goal `Goal trigger soccer_goals_<fixture>_<home>_<away> fired (with
scorer)`. The key never appears in the log.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `sportapi.ai WebSocket rejected the API key` | Wrong or revoked key; paste it again. The plugin retries with backoff (5 s up to 5 min). |
| `quota hit (HTTP 429)` | Daily/monthly limit reached; goals still show, without the scorer, until the limit resets (00:00 UTC). |
| `WEBSOCKETS PACKAGE NOT INSTALLED` on the board | The FiestaBoard image lacks `websockets`; use the official image (it ships with it). |
| No goal for a match | Only matches of the configured teams, played today, are watched. With `selected`, goals against your team are not shown. |
