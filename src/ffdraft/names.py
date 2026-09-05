"""Name resolution.

Every data source spells players differently. FantasyPros says "Josh Palmer" where
nflverse says "Joshua Palmer"; Sleeper says "Marquise Brown" where a draft board says
"Hollywood Brown"; PFR writes "Kenneth Walker III" and ESPN writes "Kenneth Walker".
Silent mismatches are the worst failure mode here — a player who doesn't join looks
like he scored zero all year, which quietly manufactures busts in any analysis.

This module centralises matching so every join in the codebase uses the same logic.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import pandas as pd

SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)
PUNCT = re.compile(r"[^a-z0-9 ]")
SPACES = re.compile(r"\s+")

# Formal name <-> the short form ranking sites tend to publish.
NICKNAMES: dict[str, str] = {
    "joshua": "josh", "michael": "mike", "christopher": "chris", "cameron": "cam",
    "robert": "rob", "william": "will", "nicholas": "nick", "anthony": "tony",
    "matthew": "matt", "gregory": "greg", "jeffrey": "jeff", "daniel": "dan",
    "benjamin": "ben", "samuel": "sam", "timothy": "tim", "zachary": "zach",
    "gabriel": "gabe", "kenneth": "ken", "jonathan": "jon", "nathaniel": "nate",
    "nathan": "nate", "alexander": "alex", "andrew": "drew", "steven": "steve",
    "stephen": "steve", "patrick": "pat", "edward": "ed", "richard": "rich",
    "thomas": "tom", "charles": "charlie", "james": "jim", "joseph": "joe",
    "david": "dave", "ronald": "ron", "donald": "don", "kenny": "ken",
    "jaxon": "jax", "elijah": "eli", "isaiah": "izzy", "demarcus": "marcus",
    "calvin": "cal", "frederick": "fred", "theodore": "theo", "vincent": "vince",
    "salvatore": "sal", "raymond": "ray", "lawrence": "larry", "phillip": "phil",
    "philip": "phil", "douglas": "doug", "terrence": "terry", "terrance": "terry",
    "bryson": "bry", "jermaine": "jay",
}

# Players genuinely known by a different name than their given one.
HARD_ALIASES: dict[str, str] = {
    "hollywood brown": "marquise brown",
    "marquise hollywood brown": "marquise brown",
    "bam knight": "zonovan knight",
    "chig okonkwo": "chigoziem okonkwo",
    "gabe davis": "gabriel davis",
    "chris brooks": "christopher brooks",
    "scotty miller": "scott miller",
    "mitch trubisky": "mitchell trubisky",
    "robby anderson": "robbie anderson",
    "willie snead iv": "willie snead",
    "irv smith": "irv smith jr",
    "jeff wilson": "jeffery wilson",
    "ronald jones ii": "ronald jones",
    "benny snell jr": "benny snell",
    "elijah mitchell": "eli mitchell",
    "cam akers": "cameron akers",
    "josh jacobs": "joshua jacobs",
    "nick chubb": "nicholas chubb",
    "kenny walker": "kenneth walker",
    "ken walker iii": "kenneth walker",
    "brian robinson jr": "brian robinson",
    "michael pittman jr": "michael pittman",
    "travis etienne jr": "travis etienne",
    "marvin harrison jr": "marvin harrison",
    "brian thomas jr": "brian thomas",
    "calvin austin iii": "calvin austin",
    "deebo samuel sr": "deebo samuel",
    "odell beckham jr": "odell beckham",
    "allen robinson ii": "allen robinson",
    "laviska shenault jr": "laviska shenault",
    "pierre strong jr": "pierre strong",
    "tyrone tracy jr": "tyrone tracy",
    "audric estime": "audric estime",
    "velus jones jr": "velus jones",
}


def _merge_initials(n: str) -> str:
    """"d j moore" -> "dj moore". Dropping the dots from "D.J." leaves single-letter
    tokens that never match the undotted spelling nflverse and ESPN disagree on;
    "D.J. Moore" recorded as drafted still showed available on a "DJ Moore" board."""
    out: list[str] = []
    run: list[str] = []
    for tok in n.split(" "):
        if len(tok) == 1:
            run.append(tok)
            continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0])
            run = []
        out.append(tok)
    if run:
        out.append("".join(run) if len(run) > 1 else run[0])
    return " ".join(out)


def _base_norm(name: str) -> str:
    """Normalization without alias substitution — keeps the original spelling's tokens."""
    n = str(name).lower().strip()
    n = n.replace("'", "").replace("`", "").replace(".", " ").replace("-", " ")
    n = PUNCT.sub(" ", n)
    n = SUFFIX.sub(" ", n)
    return _merge_initials(SPACES.sub(" ", n).strip())


def normalize(name: str) -> str:
    """Canonical key: lowercase, no punctuation, no generational suffix, dotted
    initials merged ("D.J." and "DJ" agree)."""
    n = str(name).lower().strip()
    n = n.replace("'", "").replace("`", "").replace(".", " ").replace("-", " ")
    n = PUNCT.sub(" ", n)
    n = SUFFIX.sub(" ", n)
    n = _merge_initials(SPACES.sub(" ", n).strip())
    n = SHORTHAND.get(n.replace(" ", ""), n)
    return HARD_ALIASES.get(n, n)


def _swap_first(key: str) -> str | None:
    """Swap a formal first name for its short form, or the reverse."""
    parts = key.split()
    if len(parts) < 2:
        return None
    first = parts[0]
    if first in NICKNAMES:
        return " ".join([NICKNAMES[first]] + parts[1:])
    for formal, short in NICKNAMES.items():
        if short == first:
            return " ".join([formal] + parts[1:])
    return None


# Shorthand that the automatic generator can't derive, because it comes from capitals
# inside a surname (McCaffrey -> CMC) rather than from separate name tokens.
SHORTHAND: dict[str, str] = {
    "cmc": "christian mccaffrey",
    "obj": "odell beckham",
    "ceh": "clyde edwards helaire",
    "jsn": "jaxon smith njigba",
    "arsb": "amon ra st brown",
    "mhj": "marvin harrison",
    "btj": "brian thomas",
    "jam": "jonathan taylor",
    "dhop": "deandre hopkins",
    "cd": "ceedee lamb",
}


def _initialisms(key: str) -> list[str]:
    """Shorthand people actually type: JSN, CMC, OBJ, ARSB, CEH.

    Generated rather than hand-listed, so next year's rookies work without an edit.
    Only for names of three or more tokens, plus the first-initial-plus-surname form
    for two-token names. Two-letter initialisms are far too collision-prone to trust,
    and any collision is caught by the ambiguity check before it can pick wrongly.
    """
    parts = key.split()
    out = []
    if len(parts) >= 3:
        out.append("".join(p[0] for p in parts))
    if len(parts) >= 2:
        out.append(parts[0][0] + parts[-1])  # "jjefferson"
    return out


def alias_keys(name: str) -> list[str]:
    """Every key a name might plausibly be stored under, best first."""
    base = normalize(name)
    keys = [base, _base_norm(name)]
    # Collapsed initials: "d j moore" should also answer to "dj moore".
    collapsed = re.sub(r"\b([a-z]) ([a-z]) ", r"\1\2 ", base)
    if collapsed != base:
        keys.append(collapsed)
    swapped = _swap_first(base)
    if swapped:
        keys.append(swapped)
    parts = base.split()
    if len(parts) >= 2:
        # Initialised first name: "dj moore" <-> "d j moore"
        collapsed = parts[0].replace(" ", "")
        if len(collapsed) <= 2:
            keys.append(" ".join([" ".join(collapsed)] + parts[1:]))
        if len(parts) > 2:
            # Drop middle names: "amon ra st brown" keeps first+last too
            keys.append(f"{parts[0]} {parts[-1]}")
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class PlayerIndex:
    """Resolves free-text names against a reference table.

    Resolution runs as a cascade, strongest signal first: exact key, then alias
    variants, then last name plus first initial, then fuzzy similarity. Position is
    used to disambiguate whenever it's known, which is what keeps Josh Allen the
    quarterback apart from Josh Allen the linebacker.
    """

    def __init__(self, table: pd.DataFrame, name_col: str = "name",
                 pos_col: str = "position"):
        self.table = table.reset_index(drop=True)
        self.name_col = name_col
        self.pos_col = pos_col if pos_col in self.table.columns else None

        self._by_key: dict[str, list[int]] = {}
        self._by_short: dict[str, list[int]] = {}
        for i, nm in enumerate(self.table[name_col]):
            for k in alias_keys(nm):
                self._by_key.setdefault(k, []).append(i)
            for k in _initialisms(normalize(nm)):
                self._by_short.setdefault(k, []).append(i)
        self._all_keys = list(self._by_key)

    def _filter_pos(self, idxs: list[int], position: str | None) -> list[int]:
        if not position or not self.pos_col:
            return idxs
        same = [i for i in idxs if self.table.at[i, self.pos_col] == position.upper()]
        return same or idxs

    def resolve(self, query: str, position: str | None = None,
                min_ratio: float = 0.84) -> tuple[pd.Series | None, str]:
        """Return (row, how_it_matched). Row is None when nothing is confident enough."""
        if not query or not str(query).strip():
            return None, "empty"

        for i, key in enumerate(alias_keys(query)):
            if key in self._by_key:
                cand = self._filter_pos(self._by_key[key], position)
                if len(cand) == 1 or (cand and position):
                    return self.table.iloc[cand[0]], "exact" if i == 0 else "alias"
                if cand:
                    return self.table.iloc[cand[0]], "ambiguous"

        base = normalize(query)
        parts = base.split()

        # A single token is what people actually type mid-draft — "Bijan", "Puka",
        # "Mahomes". Accept it when it identifies exactly one player, as either a
        # first or last name, and refuse when it's ambiguous.
        if len(parts) == 1:
            tok = parts[0]
            cand = [i for k, v in self._by_key.items()
                    if tok in k.split() for i in v]
            cand = self._filter_pos(sorted(set(cand)), position)
            if len(cand) == 1:
                return self.table.iloc[cand[0]], "single_token"
            if len(cand) > 1:
                # Prefer an exact surname hit over a first-name collision.
                last = [i for i in cand
                        if normalize(self.table.at[i, self.name_col]).split()[-1] == tok]
                if len(last) == 1:
                    return self.table.iloc[last[0]], "single_token_surname"
                opts = ", ".join(str(self.table.at[i, self.name_col]) for i in cand[:5])
                return None, f"ambiguous ({len(cand)}): {opts}"

        if len(parts) >= 2:
            last, first_i = parts[-1], parts[0][:1]
            cand = [i for k, v in self._by_key.items() if k.endswith(" " + last)
                    and k.startswith(first_i) for i in v]
            cand = self._filter_pos(sorted(set(cand)), position)
            if len(cand) == 1:
                return self.table.iloc[cand[0]], "lastname_initial"

        # Shorthand initialisms, checked late so a real name always wins.
        short = base.replace(" ", "")
        if short in self._by_short:
            cand = self._filter_pos(self._by_short[short], position)
            if len(cand) == 1:
                return self.table.iloc[cand[0]], "initialism"
            if len(cand) > 1:
                opts = ", ".join(str(self.table.at[i, self.name_col]) for i in cand[:5])
                return None, f"ambiguous ({len(cand)}): {opts}"

        # Fuzzy is the last resort. Scoring the query against every key in the index
        # is wasteful — a name that differs in its first letter and its length is
        # never the match. Blocking on those two cheap filters first cuts the
        # candidate set by roughly an order of magnitude before any real comparison.
        # 0.84 rather than something stricter: two typos in a full name lands at
        # about 0.857 ("bijon robinsen" -> "bijan robinson"), while a name that
        # isn't on the board at all scores around 0.15. The gap is wide enough that
        # a slightly looser threshold buys real typo tolerance without risking a
        # wrong match, and genuine ambiguity is caught before this point anyway.
        best, best_r = None, 0.0
        qlen, qinit = len(base), base[:1]
        for k in self._all_keys:
            if abs(len(k) - qlen) > 4:
                continue
            if k[:1] != qinit and (len(k) < 2 or len(base) < 2 or k.split()[-1][:1] != base.split()[-1][:1]):
                continue
            r = _similar(base, k)
            if r > best_r:
                best_r, best = r, k
        if best and best_r >= min_ratio:
            cand = self._filter_pos(self._by_key[best], position)
            if cand:
                return self.table.iloc[cand[0]], f"fuzzy:{best_r:.2f}"
        return None, "unmatched"

    def resolve_many(self, names, positions=None) -> pd.DataFrame:
        """Bulk resolution with a report of how each name matched."""
        positions = positions if positions is not None else [None] * len(names)
        rows = []
        for nm, pos in zip(names, positions):
            row, how = self.resolve(nm, pos)
            rows.append({
                "query": nm,
                "matched_name": (row[self.name_col] if row is not None else None),
                "match_type": how,
                "index": (row.name if row is not None else None),
            })
        return pd.DataFrame(rows)
