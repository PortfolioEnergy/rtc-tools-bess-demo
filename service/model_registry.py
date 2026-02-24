"""Convention-based mapping from PE API model_name to local solver type."""

from __future__ import annotations

# Each keyword maps to one of the three supported solver types.
# The model_name from the PE API URL is split into tokens and checked
# against these keywords.
_KEYWORD_TO_SOLVER: dict[str, str] = {
    # Day-ahead scheduling
    "scheduling": "scheduling",
    "day_ahead": "scheduling",
    "dayahead": "scheduling",
    "da": "scheduling",
    # Intraday continuous
    "rolling": "intraday",
    "intraday": "intraday",
    "continuous": "intraday",
    "ic": "intraday",
    # DA setpoints from positions (shortcut — no solver)
    "da_setpoints": "da_setpoints",
    "da_setpoints_from_positions": "da_setpoints",
    "setpoints": "da_setpoints",
}

SUPPORTED_KEYWORDS: list[str] = sorted(_KEYWORD_TO_SOLVER.keys())


def resolve_solver_type(model_name: str) -> str | None:
    """Resolve a PE API model_name to a local solver type.

    Checks the full name first, then splits on ``_`` and ``-`` and checks
    each token.  Returns ``"scheduling"``, ``"intraday"``,
    ``"da_setpoints"``, or ``None`` if no match is found.
    """
    name = model_name.lower().strip()

    # Full-name match first (e.g. "bess_day_ahead" won't match a single
    # token "bess" but it will match if a known key is a substring of
    # the full name when split into tokens below).
    if name in _KEYWORD_TO_SOLVER:
        return _KEYWORD_TO_SOLVER[name]

    # Check all contiguous token combinations (e.g. "bess_day_ahead"
    # produces "bess", "day", "ahead", "bess_day", "day_ahead",
    # "bess_day_ahead").  Longer matches are checked first so that
    # "day_ahead" beats "da" if both would match.
    tokens = name.replace("-", "_").split("_")
    for length in range(len(tokens), 0, -1):
        for start in range(len(tokens) - length + 1):
            candidate = "_".join(tokens[start : start + length])
            if candidate in _KEYWORD_TO_SOLVER:
                return _KEYWORD_TO_SOLVER[candidate]

    return None
