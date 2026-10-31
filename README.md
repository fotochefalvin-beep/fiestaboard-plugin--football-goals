# Soccer Goal Notifier

A [FiestaBoard](https://github.com/Fiestaboard/FiestaBoard) plugin that puts a
goal on the board as soon as it is scored in a match of a team you follow:

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
  plugin looks up each team's fixtures (`/teams/slug/{slug}`), and subscribes
  over `wss://sportapi.ai/ws` to the matches played **today**.
- When a `score_update` shows a higher total than before, it fetches the
  scorer from `/fixtures/{id}/events` and fires a trigger (priority
  `NOTABLE`). A newer goal replaces the previous goal on the board.
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
A match day with two teams costs about 20–30 requests: ≤ 9 team lookups per
team, one `/fixtures/{id}` per match (short team names), one
`/fixtures/date` call per start, and per goal one `/events` call plus at most
two retries. A `429` is respected (`Retry-After`): the goal is shown without
the scorer.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| sportapi.ai API Key | – | Your key (password field). Never logged. |
| Teams | `mechelen, barcelona` | Comma-separated sportapi.ai team slugs. Names are turned into slugs (`Racing Genk` → `racing-genk`). |
| Goals Shown | `all` | `all`: every goal in a watched team's match, including goals against them. `selected`: only goals scored by one of your teams. |
| Trigger Duration (seconds) | `60` | How long a goal stays on the board (min 10). |

A team sportapi.ai does not know is logged and reported when you save the
settings (it never stops the plugin). Find a slug in the team's URL on
sportapi.ai, or see [docs/SETUP.md](docs/SETUP.md).

### Template variables

`{{soccer_goals.match}}`, `{{soccer_goals.score}}`,
`{{soccer_goals.scorer}}`, `{{soccer_goals.minute}}` (each ≤ 22 characters)
hold the last goal for two hours. Without a recent goal, `match` holds the
next watched match (`BARCELONA - ALAVES`). Set the plugin's *trigger page*
to render a goal with your own template.

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
with this repo linked as `plugins/soccer_goals`. They never touch the network
(HTTP and the WebSocket are faked). `.github/workflows/ci.yml` is the
canonical recipe; locally, from the folder that *contains* this repo:

```bash
git clone --branch v9.13.0 https://github.com/Fiestaboard/FiestaBoard.git fiestaboard-core
python3.11 -m venv .venv-fb && source .venv-fb/bin/activate
pip install -r fiestaboard-core/requirements.txt pytest pytest-cov

mkdir -p plugins && touch plugins/__init__.py
ln -s ../fiestaboard-plugin--soccer-goals plugins/soccer_goals

cd fiestaboard-plugin--soccer-goals
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
