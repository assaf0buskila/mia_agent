"""OAuth query data is scrubbed on real server access records as well as root logs."""

import logging
import random
import time

import httpx
import pytest
from app.core.logging import RedactingFilter, configure_logging
from app.core.redact import redact


def adversarial_paths():
    size = 65536
    families = ["/", "/a", "/,", "/=", '/"', "/?", "?", "/oauth?",
                "/oauth/authorize?", "http://x/", "%2f"]
    paths = {unit: (unit * (size // len(unit) + 1))[:size] for unit in families}
    rng = random.Random(214)
    paths["mixed"] = "".join(rng.choices('/a,=\"?%2foauth', k=size))
    return paths


@pytest.mark.parametrize("family", adversarial_paths())
def test_oauth_query_scrub_is_linear_on_64k_adversarial_paths(family):
    path = adversarial_paths()[family]
    start = time.perf_counter()
    redact(path)
    elapsed = time.perf_counter() - start
    print(f"TIMING {family!r}: {elapsed * 1000:.3f} ms", flush=True)
    assert elapsed < 0.1


def test_uvicorn_access_filter_is_linear_on_64k_path():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "uvicorn", 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/" * 65536, "1.1", 302), None)
    start = time.perf_counter()
    assert RedactingFilter().filter(record)
    elapsed = time.perf_counter() - start
    print(f"TIMING uvicorn: {elapsed * 1000:.3f} ms", flush=True)
    assert elapsed < 0.1


@pytest.mark.parametrize("prefix", ["", "'", '"', "url=", "GET <", "{'url': '"])
@pytest.mark.parametrize("path", ["/oauth/authorize", "/oauth/%61uthorize",
                                  "/%6fauth/authorize", "/oauth/token/suffix"])
def test_oauth_query_all_log_forms_and_non_oauth_queries(prefix, path):
    assert "PRIVATE" not in redact(prefix + path + "?state=PRIVATE'\"<suffix")
    untouched = prefix + "/health?state=PUBLIC"
    assert redact(untouched) == untouched


def test_oauth_query_is_redacted_in_string_and_url_object():
    url = "https://api.test/oauth/authorize?state=private-state&code_challenge=pkce"
    for value in (url, httpx.URL(url), {"url": url}):
        rendered = str(redact(value))
        assert "private-state" not in rendered and "pkce" not in rendered
        assert "/oauth/authorize" in rendered


def test_raw_printable_state_punctuation_never_ends_query_redaction():
    for marker in ("'", '"', "<", ">"):
        url = "/oauth/authorize?state=prefix" + marker + "SENSITIVE&client_id=pinned"
        assert "SENSITIVE" not in redact(url)


def test_uvicorn_access_logger_itself_scrubs_query_even_with_own_handler():
    logger = logging.getLogger("uvicorn.access")
    before = list(logger.filters)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_logging()
        assert any(isinstance(f, RedactingFilter) for f in logger.filters)
        for path in ("/oauth/authorize", "/oauth/authorize/", "/oauth/%61uthorize",
                     "/%6fauth/authorize", "/oauth/authorize//", "/oauth/authorize/callback",
                     "/oauth/token/unknown"):
            record = logging.LogRecord("uvicorn.access", logging.INFO, "uvicorn", 1,
                                       '%s - "%s %s HTTP/%s" %d',
                                       ("127.0.0.1", "GET", path + "?state=private-state",
                                        "1.1", 302), None)
            assert logger.filter(record)
            assert "private-state" not in record.getMessage()
            assert path in record.getMessage()
    finally:
        logger.filters[:] = before
        root.handlers[:] = handlers
        root.setLevel(level)
