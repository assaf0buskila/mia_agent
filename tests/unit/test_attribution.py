from app.domain.attribution import sanitize_attribution, sanitize_instagram_attribution
from app.domain.deals import confidence_from_attribution


def test_sanitize_instagram_attribution_story_reply() -> None:
    result = sanitize_instagram_attribution({
        "ig_content_id": "story.media.123",
        "ig_trigger_source": "STORY",
        "url": "https://cdn.example/expired.jpg",
    })
    assert result == {
        "ig_content_id": "story.media.123",
        "ig_trigger_source": "STORY",
    }
    assert "url" not in result


def test_sanitize_instagram_attribution_referral_shortlinks() -> None:
    result = sanitize_instagram_attribution({
        "ig_trigger_source": "SHORTLINKS",
        "ig_ref": "my-campaign_ref=1",
    })
    assert result == {
        "ig_trigger_source": "SHORTLINKS",
        "ig_ref": "my-campaign_ref=1",
    }


def test_sanitize_instagram_attribution_ads_ids_only() -> None:
    result = sanitize_instagram_attribution({
        "ig_trigger_source": "ADS",
        "meta_ad_id": "1234567890",
        "meta_post_id": "9876543210",
        "meta_campaign_id": "1122334455",
        "photo_url": "https://cdn.example/photo.jpg",
        "ad_title": "Buy now",
    })
    assert result == {
        "ig_trigger_source": "ADS",
        "meta_ad_id": "1234567890",
        "meta_post_id": "9876543210",
        "meta_campaign_id": "1122334455",
    }


def test_sanitize_instagram_attribution_drops_invalid_ref() -> None:
    assert sanitize_instagram_attribution({"ig_ref": "bad ref @here"}) == {}
    assert sanitize_instagram_attribution({"ig_ref": "spaces not allowed"}) == {}


def test_sanitize_instagram_attribution_drops_unknown_source() -> None:
    assert sanitize_instagram_attribution({"ig_trigger_source": "INVENTED"}) == {}


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
