"""Small shared value types for model-composed replies."""

from pydantic import BaseModel, ConfigDict

_MAX_TOKENS = 10_000_000


class ComposeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    tokens_in: int = 0
    tokens_out: int = 0


def clamp_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, _MAX_TOKENS)
