"""Eval-driven model id helpers. Never hard-code production model names here."""


def model_chain(*names: str) -> tuple[str, ...]:
    """Primary then fallback. Empty and duplicate ids are dropped, order preserved."""
    chain: list[str] = []
    for name in names:
        cleaned = name.strip()
        if cleaned and cleaned not in chain:
            chain.append(cleaned)
    return tuple(chain)
