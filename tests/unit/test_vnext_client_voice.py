from pathlib import Path


def test_website_voice_route_uses_same_client_graph_as_text() -> None:
    website = Path("app/api/website.py").read_text(encoding="utf-8")
    assert "compile_client_graph" not in website
    assert "process_website_message" in website
    assert "transcribe_port.transcribe" in website
    assert "run_site_turn" in website or "site_book" in website
    voice_calls_process = website.count("process_website_message")
    assert voice_calls_process >= 2
