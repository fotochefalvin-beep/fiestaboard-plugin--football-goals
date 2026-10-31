# Football Goal Notifier

A [FiestaBoard](https://github.com/Fiestaboard/FiestaBoard) plugin that puts a
goal on the board as soon as it is scored in a match of the team you follow:

```
GOAL!
MECHELEN 1-0 GENK
S. MICHEZ 22'
```

Scores are **pushed** by [sportapi.ai](https://sportapi.ai) over a WebSocket
(no polling). sportapi.ai pushes a score about 30–60 s after the goal; the
board shows it on FiestaBoard's next engine pass (`polling_interval`,
15 s by default), so expect the goal on the board 45–75 s after it was
scored.

## How it works

- Once an hour at most (normally every 3 hours, and once after midnight) the
  plugin looks up the team's fixtures (`/teams/slug/{slug}`), and subscribes
  over `wss://sportapi.ai/ws` to the matches played **today**.
- When a `score_update` shows a higher total than before, it fetches the
  scorer from `/fixtures/{id}/events` and fires a trigger (priority
  `NOTABLE`). Every goal in the team's match is announced, goals against
  them too. A newer goal replaces the previous goal on the board.
- If sportapi.ai does not list the scorer yet, the goal is shown without the
  third line straight away, and `/events` is retried after 20 s and 60 s; the
  same trigger is shown again with the scorer once it appears.
- Own goals and penalties get `(OG)` / `(PEN)` when that still fits the row.
- The first score after every (re)connect is only remembered, never
  announced, and a score that goes *down* (disallowed goal) is stored
  silently. Two goals in one update give one trigger with the latest goal.
- The WebSocket reconnects after a drop with a backoff of 5 s doubling to
  5 minutes. Disabling the plugin or changing its settings closes it.
- After a restart in the middle of a match, one `/fixtures/date/{today}` call
  finds the running match (running matches are missing from the team lists).

### Request budget

The free sportapi.ai plan allows 2,000 requests a day and 14,000 a month.
A match day costs about 15–25 requests: ≤ 9 team lookups, one `/fixtures/{id}` per match (short team names), one
`/fixtures/date` call per start, and per goal one `/events` call plus at most
two retries. A `429` is respected (`Retry-After`): the goal is shown without
the scorer.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| sportapi.ai API Key | – | Your key (password field). Never logged. |
| Team | `mechelen` | **One** sportapi.ai team slug. A name is turned into a slug (`Racing Genk` → `racing-genk`). A value with a comma is rejected (`Enter one team`). |
| Trigger Duration (seconds) | `60` | How long a goal stays on the board (min 10). |

A team sportapi.ai does not know is logged and reported when you save the
settings (it never stops the plugin). Find a slug in the team's URL on
sportapi.ai, or see [docs/SETUP.md](docs/SETUP.md).

### Template variables

Each variable is upper-case ASCII, at most 22 characters, and follows your
team's match, so a template can lay the pieces out freely:

| Variable | Example | Holds |
|---|---|---|
| `{{football_goals.home_team}}` | `MECHELEN` | Home team (short name when known) |
| `{{football_goals.away_team}}` | `GENK` | Away team (short name when known) |
| `{{football_goals.home_goals}}` | `1` | Home team's live goals: follow every score push, also a disallowed goal going back down |
| `{{football_goals.away_goals}}` | `0` | Away team's live goals, the same way |
| `{{football_goals.scorer}}` | `S. MICHEZ` | Scorer of the latest goal |
| `{{football_goals.minute}}` | `22'` | Minute of the latest goal |

- **Before kick-off:** `home_team`/`away_team` are the next match, the rest
  is empty (all empty when no match is known).
- **During the match:** the live teams and goals. `scorer`/`minute` are
  empty until sportapi.ai lists the latest goal in `/events` (checked right
  away, then after 20 s and 60 s), and empty before the first goal.
- **After full time:** the final state stays for two hours, then the next
  match.

The plugin's own page keeps showing `GOAL! / MECHELEN 1-0 GENK / S. MICHEZ
22'` for two hours after a goal, else `NEXT MATCH`. Set the plugin's
*trigger page* to render a goal with your own template.

### Upgrading

0.3.0 replaced `score` (`1-0`) by `home_goals` and `away_goals`; a
template that used `{{football_goals.score}}` can use
`{{football_goals.home_goals}}-{{football_goals.away_goals}}`.


0.2.0 renamed the plugin (id `football_goals`): remove the 0.1.x plugin and
install this one again, then re-enter the key and team (templates use
`{{football_goals.*}}` now). A config that still holds the old `teams`
setting keeps working: only its first team is followed (logged once). The `goals_shown` setting is gone; every
goal is shown.

## Requirements

- FiestaBoard v9.13.0 or later (tested against v9.13.0, `519dd1d`).
- The `websockets` package, which the FiestaBoard image already has through
  `uvicorn[standard]`. If it is missing the plugin says so on the board
  (`WEBSOCKETS PACKAGE NOT INSTALLED`) instead of failing.
- A free sportapi.ai API key: see [docs/SETUP.md](docs/SETUP.md).

## Development

### Plugin tests

The tests run against a checkout of
[FiestaBoard core](https://github.com/Fiestaboard/FiestaBoard) at `v9.13.0`,
with this repo linked as `plugins/football_goals`. They never touch the network
(HTTP and the WebSocket are faked). `.github/workflows/ci.yml` is the
canonical recipe; locally, from the folder that *contains* this repo:

```bash
git clone --branch v9.13.0 https://github.com/Fiestaboard/FiestaBoard.git fiestaboard-core
python3.11 -m venv .venv-fb && source .venv-fb/bin/activate
pip install -r fiestaboard-core/requirements.txt pytest pytest-cov

mkdir -p plugins && touch plugins/__init__.py
ln -s ../fiestaboard-plugin--football-goals plugins/football_goals

cd fiestaboard-plugin--football-goals
PYTHONPATH="$(cd .. && pwd):$(cd ../fiestaboard-core && pwd)" \
BOARD_READ_WRITE_KEY=test_key \
python -m pytest tests/ -v --cov --cov-report=term-missing
```

Coverage is measured on the plugin package (`__init__.py`, `sportapi.py`)
and must be at least 80% (`.coveragerc`). CI also runs the host's
`validate_manifest` and install check. There is deliberately no
`requirements.txt` in this repo: the host's install check scans the tree for
one, and every dependency (`requests`, `websockets`) comes with FiestaBoard.

## License

MIT, see [LICENSE](LICENSE).
