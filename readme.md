# Tildes Colossal Game Adventure — Vote Tally Script

Scrape and tally votes from a Tildes discussion thread where people vote in **top-level** comments using lines like:


This script:
- Downloads the page and finds **top-level** comments (not replies)  
- Extracts every `Title (N)` pair from each ballot (robust to titles with extra parentheses)  
- Matches titles against a **tab-delimited** `games_population.csv` with aliases (`alias1`, `alias2`, …)  
- Enforces rules:
  - **≤ 20** total points per user
  - **≤ 5** points for any single game per user
- Adds helpful normalization:
  - Accepts titles missing a leading **“The ”** for canonicals that start with it (e.g., `Secret of Monkey Island` → `The Secret of Monkey Island`)
  - Accepts trailing punctuation variants (`!?.`) in titles
- Produces:
  - `tally.csv` — total points per game (descending)
  - `invalid_votes.txt` — users whose ballots violated rules (not counted)
  - `ignored_votes.txt` — per-user lines that didn’t match any canonical/alias

> **Works great for** the thread format used in Tildes’ **Colossal Game Adventure** voting topics.

---

## Quick Start

### 1) Install
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2) Prepare the game list (tab-delimited)
Create a games_population.csv in the repo root. Must be tab-delimited and include a game column and optional alias* columns. Sample file provided.

### 3) Run
python tally_votes.py \
  --url https://tildes.net/~games/1pvz/colossal_game_adventure_voting_topic \
  --games-file games_population.csv \
  --print-summary
or
python tally_votes.py --debug --print-summary


### Command Options
--url           Page to scrape (default points at CGA voting topic)
--games-file    Path to tab-delimited games CSV (default: games_population.csv)
--tally-out     Output CSV for totals (default: tally.csv)
--invalid-out   Output text file for invalid ballots (default: invalid_votes.txt)
--ignored-out   Output text file for unmatched titles (default: ignored_votes.txt)
--min-pairs     Minimum Title(N) pairs to treat a comment as a ballot (default: 1)
--debug         Verbose progress and parsing logs
--author-debug  Regex of author names to focus debug logs (e.g., "user_id")
--print-summary Print top results at the end
--dump-html     Save the fetched HTML to a file for inspection
