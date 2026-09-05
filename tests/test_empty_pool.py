"""A board with every row taken is a named refusal, not a traceback.

`ConditionalLogit.probabilities` reduced over an empty array
(`zero-size array to reduction operation maximum`) and the traceback reached
predict_pick. Not reachable on a live board before the draft ends; reachable
at the last pick of a full room and in any fixture that drafts everyone.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ffdraft import choice


def test_probabilities_over_no_candidates_is_an_empty_distribution():
    m = choice.ConditionalLogit(("a", "b"))
    p = m.probabilities(np.zeros((0, 2)))
    assert p.shape == (0,)


def test_forecast_over_no_candidates_says_so_instead_of_raising():
    wf = choice.WalkForward()
    empty = pd.DataFrame({"name": pd.Series([], dtype=str), "position": pd.Series([], dtype=str),
                          "adp": pd.Series([], dtype=float), "proj_points": pd.Series([], dtype=float),
                          "espn_rank": pd.Series([], dtype=float), "draft_score": pd.Series([], dtype=float),
                          "vor": pd.Series([], dtype=float)})
    out = wf.forecast(empty, [])
    assert out["refused"].startswith("no undrafted rows")
