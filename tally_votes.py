#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tildes CGA Voting Tally — FIXED top-level detection + safe vote parsing

Key fixes:
- Top-level detection now uses the presence/absence of a **'Parent'** link:
  replies have a "Parent" control; top-level ballots do not.
- Vote extraction no longer splits lines; it scans the entire comment body with a
  regex that matches every `Title (N)` pair, so titles that contain parentheses
  (e.g., "... (The Frog for Whom the Bell Tolls) (2)") are handled correctly.

Still included:
- TAB-delimited games_population.csv with alias1/alias2/... columns
- Auto alias for missing leading "The " + trailing punctuation variants
- Outputs: tally.csv, invalid_votes.txt, ignored_votes.txt
- Deep debug + author-focused debug

Usage example:
  python tally_votes.py --url <page> --games-file games_population.csv \
    --debug --author-debug "user_name" --print-summary
"""

import argparse
import csv
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from typing import Dict, Set, Tuple, List, Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag

# ---------------- Config / Regex ----------------

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vote-tally/1.5)"}

# Find EVERY "Title (digits)" pair anywhere in the body.
# Non-greedy title; digits parens; next must be boundary-ish (end/whitespace/newline)
VOTE_PAIR_REGEX = re.compile(r"(?P<title>.+?)\s*\((?P<points>\d+)\)(?=(?:\s+|$))")

# ---------------- Normalization ----------------

def normalize_unicode(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()

def fold_punct(s: str) -> str:
    repl = {
        "\u2019": "'", "\u2018": "'", "\u2032": "'",
        "\u201C": '"', "\u201D": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u00A0": " ",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def norm_key(s: str) -> str:
    return fold_punct(normalize_unicode(s)).lower()

# ---------------- Games TSV w/ aliases ----------------

def _add_key_with_variants(mapper: Dict[str, str], key: str, canonical: str, warnset: set, debug: bool):
    for k in {key, key.rstrip("!?.")}:
        if not k:
            continue
        prev = mapper.get(k)
        if prev and prev != canonical:
            if debug and (k, prev, canonical) not in warnset:
                print(f"[warn] key collision: '{k}' -> '{prev}' vs '{canonical}' (keeping first)")
                warnset.add((k, prev, canonical))
        else:
            mapper.setdefault(k, canonical)

def parse_games_file(path: str, debug: bool = False):
    """
    TAB CSV with columns:
      - game (canonical)
      - alias* (alias1, alias2, ... any header starting with 'alias', case-insensitive)
      - roll_over_points (optional; default 0). Pre-existing points to seed into the tally.

    Returns:
      allowed_set, key_to_canonical, rollovers_dict
        - rollovers_dict maps canonical_title -> int points (>= 0)
    """
    allowed: Set[str] = set()
    key_to_canonical: Dict[str, str] = {}
    rollovers: Dict[str, int] = defaultdict(int)
    warnset = set()

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader)
        if not header:
            raise ValueError("games_population.csv has no header row")

        # Identify columns
        idx_game = None
        idx_rollover = None
        alias_cols: List[int] = []

        for i, h in enumerate(header):
            h_clean = (h or "").strip()
            h_low = h_clean.lower()
            if h_low == "game":
                idx_game = i
            elif h_low.startswith("alias"):
                alias_cols.append(i)
            elif h_low in {"roll_over_points", "rollover_points", "roll-over-points"}:
                idx_rollover = i

        if idx_game is None:
            raise ValueError("games_population.csv must contain a 'game' column")

        for row in reader:
            if not row or len(row) <= idx_game:
                continue

            canonical = normalize_unicode(row[idx_game])
            if not canonical or canonical.startswith("#"):
                continue

            # Record canonical
            allowed.add(canonical)
            ck = norm_key(canonical)
            _add_key_with_variants(key_to_canonical, ck, canonical, warnset, debug)

            # Aliases
            for ai in alias_cols:
                if ai < len(row):
                    alias_raw = normalize_unicode(row[ai])
                    if alias_raw and not alias_raw.startswith("#"):
                        ak = norm_key(alias_raw)
                        _add_key_with_variants(key_to_canonical, ak, canonical, warnset, debug)

            # Auto "missing The "
            if ck.startswith("the "):
                ak = ck[4:].strip()
                if ak:
                    _add_key_with_variants(key_to_canonical, ak, canonical, warnset, debug)

            # Rollover points
            pts = 0
            if idx_rollover is not None and idx_rollover < len(row):
                raw = (row[idx_rollover] or "").strip()
                if raw:
                    try:
                        pts = int(raw)
                    except ValueError:
                        if debug:
                            print(f"[warn] Non-integer roll_over_points '{raw}' for '{canonical}' -> treating as 0")
                        pts = 0
            if pts:
                rollovers[canonical] += pts  # accumulate if duplicates appear

    if debug:
        canon_keys = {norm_key(t) for t in allowed}
        alias_count = sum(1 for k in key_to_canonical if k not in canon_keys)
        total_roll = sum(rollovers.values())
        print(f"[games] Canonical: {len(allowed)}  total match-keys: {len(key_to_canonical)} (~{alias_count} aliases)")
        print(f"[games] Rollover points: {total_roll} across {len(rollovers)} games")

    return allowed, key_to_canonical, dict(rollovers)


# ---------------- Fetch + DOM helpers ----------------

def fetch_soup(url: str, debug=False) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    if debug:
        print(f"[fetch] {url} -> {r.status_code}, {len(r.text)} bytes")
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def has_parent_link(tag: Tag) -> bool:
    """Replies on Tildes show a 'Parent' control; top-level ballots do not."""
    a = tag.find("a", string=lambda s: s and s.strip().lower() == "parent")
    return a is not None

def looks_like_comment_container(tag: Tag) -> bool:
    """Heuristic: container that certainly holds a single comment."""
    if not isinstance(tag, Tag):
        return False
    # Must contain an author link and a body/text block somewhere inside.
    has_author = bool(tag.select_one('a[rel="author"], .link-user'))
    has_body = bool(tag.select_one(".topic-comment-text, .comment-text, .markdown, .md, .content, .text"))
    return has_author and has_body

def extract_author(tag: Tag) -> str:
    a = tag.select_one('a[rel="author"]') or tag.select_one(".link-user")
    if a and a.get_text(strip=True):
        return a.get_text(strip=True)
    return "UNKNOWN_USER"

def extract_body_text(tag: Tag) -> str:
    el = (tag.select_one(".topic-comment-text")
          or tag.select_one(".comment-text")
          or tag.select_one(".markdown, .md, .content, .text"))
    if el:
        return el.get_text("\n", strip=True)
    return tag.get_text("\n", strip=True)

def find_top_level_containers(soup: BeautifulSoup, debug=False) -> List[Tag]:
    """
    Robust search that does NOT depend on exact class names:
      1) Start from each author anchor and climb to a reasonable container.
      2) Keep containers that:
          - look like comments, and
          - do NOT contain a 'Parent' link (=> treat as top-level)
    """
    containers: List[Tag] = []
    seen = set()
    for a in soup.select('a[rel="author"], .link-user'):
        # Climb to nearest block-ish container
        t = a
        parent = None
        for _ in range(6):  # climb a few levels only
            if t is None: break
            if isinstance(t, Tag) and t.name in {"li", "article", "div"} and looks_like_comment_container(t):
                parent = t
                break
            t = t.parent
        if not parent:
            continue
        if id(parent) in seen:
            continue
        seen.add(id(parent))
        # Must still look like a comment container
        if not looks_like_comment_container(parent):
            continue
        # Top-level if there is NO 'Parent' link inside this container
        if not has_parent_link(parent):
            containers.append(parent)

    if debug:
        print(f"[scan] top-level containers found (via author-anchor): {len(containers)}")
    return containers

# ---------------- Vote extraction / Ballot validation ----------------

def find_vote_pairs(text: str) -> List[Tuple[str, int]]:
    pairs = []
    for m in VOTE_PAIR_REGEX.finditer(text):
        title = normalize_unicode(m.group("title"))
        pts = int(m.group("points"))
        pairs.append((title, pts))
    return pairs

def parse_ballot_pairs(pairs: Iterable[Tuple[str, int]], key_to_canonical: Dict[str, str], debug=False, author=""):
    per_game = Counter()
    ignored = []
    for title_raw, pts in pairs:
        key = norm_key(title_raw)
        canonical = key_to_canonical.get(key)
        if canonical:
            per_game[canonical] += pts
        else:
            ignored.append((title_raw, pts))
            if debug:
                print(f"  [ignored] {author}: '{title_raw}' ({pts}) not in allowed/alias list")
    return dict(per_game), ignored

def validate_ballot(per_game: dict) -> Tuple[bool, List[str]]:
    reasons = []
    total = sum(per_game.values())
    if total > 20:
        reasons.append(f"Total points {total} > 20")
    over5 = [f"{t} ({p})" for t, p in per_game.items() if p > 5]
    if over5:
        reasons.append("Per-game limit exceeded: " + ", ".join(over5))
    return (len(reasons) == 0), reasons

# ---------------- Iteration ----------------

def iter_top_level_ballots(soup: BeautifulSoup, debug=False, min_pairs=1, author_debug: Optional[re.Pattern]=None):
    """
    Yield dicts: {author, text, pairs}
    """
    containers = find_top_level_containers(soup, debug=debug)
    ballots = 0
    for c in containers:
        author = extract_author(c)
        text = extract_body_text(c)
        pairs = find_vote_pairs(text)

        if debug and (author_debug is None or author_debug.search(author)):
            print(f"\n[top-level] by {author} | votes-found={len(pairs)}")
            for t, p in pairs[:10]:
                print(f"  {t} ({p})")

        if len(pairs) >= min_pairs:
            ballots += 1
            yield {"author": author, "text": text, "pairs": pairs}

    if debug:
        print(f"\n[summary] ballots detected at top level: {ballots}")

# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://tildes.net/~games/1pvz/colossal_game_adventure_voting_topic")
    ap.add_argument("--games-file", default="games_population.csv", help="TAB CSV with 'game' + alias* columns")
    ap.add_argument("--tally-out", default="tally.csv")
    ap.add_argument("--invalid-out", default="invalid_votes.txt")
    ap.add_argument("--ignored-out", default="ignored_votes.txt")
    ap.add_argument("--min-pairs", type=int, default=1, help="Minimum Title(N) pairs to treat a comment as a ballot")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--author-debug", default="", help="Regex of authors for extra debug")
    ap.add_argument("--print-summary", action="store_true")
    ap.add_argument("--dump-html", default="", help="If set, write fetched HTML to this path")
    args = ap.parse_args()

    author_debug_re = re.compile(args.author_debug) if args.author_debug else None

    # Load games + aliases
    try:
        allowed, key_to_canonical, rollovers = parse_games_file(args.games_file, debug=args.debug)
    except Exception as e:
        print(f"ERROR loading games file: {e}", file=sys.stderr)
        sys.exit(1)

    # Fetch page
    soup = fetch_soup(args.url, debug=args.debug)
    if args.dump_html:
        try:
            with open(args.dump_html, "w", encoding="utf-8") as fh:
                fh.write(str(soup))
            if args.debug:
                print(f"[dump] Saved HTML to {args.dump_html}")
        except Exception as e:
            print(f"[dump] Failed to save HTML: {e}")

    # Tally
    game_totals = Counter(rollovers)   # <-- seed with pre-existing points
    invalid_log = []
    ignored_by_user = defaultdict(list)

    authors_seen = set()
    ballots_seen = 0
    valid_ballots = 0
    invalid_ballots = 0
    ignored_votes_total = 0

    for com in iter_top_level_ballots(soup, debug=args.debug, min_pairs=args.min_pairs, author_debug=author_debug_re):
        ballots_seen += 1
        user = com["author"]
        authors_seen.add(user)

        per_game, ignored = parse_ballot_pairs(com["pairs"], key_to_canonical, debug=args.debug, author=user)
        if ignored:
            ignored_by_user[user].extend(ignored)
        ignored_votes_total += len(ignored)

        if args.debug and (author_debug_re is None or author_debug_re.search(user)):
            if per_game:
                print(f"  [parsed] {user}: " + ", ".join(f"{k} ({v})" for k, v in per_game.items()))
            else:
                print(f"  [parsed] {user}: no allowed titles matched")

        if not per_game and not ignored:
            continue

        ok, reasons = validate_ballot(per_game)
        if not ok:
            invalid_ballots += 1
            invalid_log.append(f"{user}: " + "; ".join(reasons))
            if args.debug and (author_debug_re is None or author_debug_re.search(user)):
                print(f"  [INVALID] {invalid_log[-1]}")
            continue

        valid_ballots += 1
        for title, pts in per_game.items():
            game_totals[title] += pts
        if args.debug and (author_debug_re is None or author_debug_re.search(user)):
            print(f"  [OK] Counted {user}'s ballot")


    # Write outputs
    all_rows = [(title, game_totals.get(title, 0)) for title in allowed]

    # Sort: primary = points desc, secondary = title asc (case-insensitive)
    all_rows.sort(key=lambda x: (-x[1], x[0].lower()))

    with open(args.tally_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game", "total_points"])
        for title, pts in all_rows:
            w.writerow([title, pts])

    invalid_log_sorted = sorted(set(invalid_log))
    with open(args.invalid_out, "w", encoding="utf-8") as f:
        if not invalid_log_sorted:
            f.write("No invalid ballots detected.\n")
        else:
            f.write("Invalid ballots (entire ballot not counted):\n")
            for line in invalid_log_sorted:
                f.write(f"- {line}\n")

    with open(args.ignored_out, "w", encoding="utf-8") as f:
        if not ignored_by_user:
            f.write("No ignored votes detected.\n")
        else:
            f.write("Ignored votes (titles not in allowed/alias list after normalization):\n")
            for user in sorted(ignored_by_user.keys()):
                f.write(f"\n[{user}]\n")
                for title_raw, pts in ignored_by_user[user]:
                    f.write(f"  - {title_raw} ({pts})\n")

    # Console summary
    total_rollover_pts = sum(rollovers.values())
    nonzero_rollover_games = sum(1 for v in rollovers.values() if v > 0)

    print("\n=== Scrape Summary ===")
    print(f"Authors seen:            {len(authors_seen)}")
    print(f"Ballots detected:        {ballots_seen}")
    print(f"Valid ballots:           {valid_ballots}")
    print(f"Invalid ballots:         {invalid_ballots}")
    print(f"Ignored vote pairs:      {ignored_votes_total}")
    print(f"Rollover points applied: {total_rollover_pts} across {nonzero_rollover_games} games")
    print(f"Games tallied:           {len(game_totals)}")
    print(f"Games tallied:           {len(allowed)}  (including zero-point games)")
    print(f"Wrote tally to:          {args.tally_out}")
    print(f"Wrote invalids to:       {args.invalid_out}")
    print(f"Wrote ignored votes to:  {args.ignored_out}")

    if args.print_summary:
        if game_totals:
            print("\nTop results:")
            for title, pts in game_totals.most_common(20):
                print(f"  {title}: {pts}")
        else:
            print("\nNo tallies produced. Use --debug, --author-debug and --dump-html for visibility.")

if __name__ == "__main__":
    main()
