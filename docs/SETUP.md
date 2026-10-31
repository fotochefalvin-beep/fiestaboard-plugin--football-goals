# Football Goal Notifier -- Setup Guide

You need a FiestaBoard (v9.13.0 or later) and a free
[sportapi.ai](https://sportapi.ai) account. No card is needed for the free
plan (2,000 requests a day, 14,000 a month), which is plenty: this plugin
uses about 130–150 requests on a match day (most of them for the live match
minute, one request a minute while the match is played).

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
   `data/external_plugins/football_goals/` on the Pi and restart).
2. Open **Football Goal Notifier** settings:
   - **sportapi.ai API Key**: paste your key.
   - **Team**: one team slug, e.g. `mechelen` (no commas). Every goal in its
     matches is shown, goals against them too.
   - **Trigger Duration**: how long a goal stays up (default 60 s).
3. Enable the plugin and save. A team sportapi.ai does not know is reported
   on the next save and in the logs.

**Upgrading from 0.1.x?** The plugin was renamed; its id is now
`football_goals`. Remove the old plugin (or delete its old folder under
`data/external_plugins/` on the Pi), install this one, enter the key and team
again, and change templates to `{{football_goals.*}}`.

### Template variables

`{{football_goals.home_team}}`, `{{football_goals.away_team}}`,
`{{football_goals.home_goals}}`, `{{football_goals.away_goals}}`,
`{{football_goals.minute}}`, `{{football_goals.scoring_team}}`,
`{{football_goals.scorer}}`, `{{football_goals.goal_minute}}`: before
kick-off the next match's teams; during the match the live goals per team,
the live match minute (`67'`, `HT`, `FT`), the team of the latest goal, and
that goal's scorer and minute (empty until sportapi.ai lists the goal); after
full time the final state for two hours. A trigger page can show
`GOAL! {{football_goals.scoring_team}}`.
See the README for details.

**Upgrading from 0.3.x?** `minute` is now the live match minute; the latest
goal's minute is `goal_minute`. Change templates that showed the goal minute.

A page that uses the plugin shows the team's match live while it is played:

```
LIVE 67'
MECHELEN 1-0 GENK
S. MICHEZ 22'
```

`FT` and the final score for two hours after full time, otherwise the next
match (`NEXT MATCH / BARCELONA - ALAVES / SAT 31 OCT 21:00`). A goal is shown
as soon as it is pushed:

```
GOAL! MECHELEN
MECHELEN 1-0 GENK
S. MICHEZ 22'
```

## 4. Check it live

During a match of your team, follow FiestaBoard's log:

```bash
docker compose logs -f | grep -i football
```

**Copy as-is.** You should see `Football Goals listener started`, and after a
goal `Goal trigger football_goals_<fixture>_<home>_<away> fired (with
scorer)`. The key never appears in the log.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `sportapi.ai WebSocket rejected the API key` | Wrong or revoked key; paste it again. The plugin retries with backoff (5 s up to 5 min). |
| `quota hit (HTTP 429)` | Daily/monthly limit reached; goals still show, without the scorer, until the limit resets (00:00 UTC). |
| `WEBSOCKETS PACKAGE NOT INSTALLED` on the board | The FiestaBoard image lacks `websockets`; use the official image (it ships with it). |
| No goal for a match | Only matches of the configured team, played today, are watched. |
| `Live minute for ... unavailable` | The once-a-minute minute lookup failed; the board keeps the last minute and tries again a minute later (after a `429`, once `Retry-After` has passed). |
