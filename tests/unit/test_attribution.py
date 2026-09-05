from app.domain.attribution import sanitize_attribution
from app.domain.deals import confidence_from_attribution


def test_website_sanitize_attribution_unchanged() -> None:
    result = sanitize_attribution({
        "utm_source": "meta",
        "utm_campaign": "yuma",
        "ig_content_id": "should-drop",
        "landing_page": "/pricing",
    })
    assert result == {
        "utm_source": "meta",
        "utm_campaign": "yuma",
        "landing_page": "/pricing",
    }
    assert "ig_content_id" not in result


def test_confidence_from_attribution_priority() -> None:
    assert confidence_from_attribution({"utm_source": "meta"}) == "utm"
    assert confidence_from_attribution({
        "ig_content_id": "x",
        "meta_ad_id": "1",
    }) == "ig"
    assert confidence_from_attribution({"meta_ad_id": "1"}) == "meta_ad"
    assert confidence_from_attribution({"meta_campaign_id": "1"}) == "meta_ad"
    assert confidence_from_attribution({"landing_page": "/x"}) == "unknown"
    assert confidence_from_attribution(None) == "unknown"
