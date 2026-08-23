from app.core.models import model_chain


def test_model_chain_drops_empty_and_duplicates() -> None:
    assert model_chain(" primary ", "", "primary", "fallback") == ("primary", "fallback")
    assert model_chain("", "  ") == ()
