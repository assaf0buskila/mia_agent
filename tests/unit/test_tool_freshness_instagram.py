from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.content_insights import ContentInsight
from app.integrations.instagram_insights import (
    DisabledInstagramInsightsPort,
    FakeInstagramInsightsPort,
    enrich_content_insights_ack,
)

MEDIA_ID_1 = "17841400112233445566"
MEDIA_ID_2 = "17841400998877665544"

SAMPLE_ITEMS = [
    ContentInsight(
        media_id=MEDIA_ID_1,
        media_type="IMAGE",
        views="1200",
        reach="900",
        likes="45",
        comments="3",
        saved="12",
    ),
    ContentInsight(
        media_id=MEDIA_ID_2,
        media_type="REELS",
        views="5000",
        reach="4200",
        likes="210",
    ),
]


def test_instagram_adapter_freshness_is_cached_for_real_results() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        ack, outcome = enrich_content_insights_ack(
            "", FakeInstagramInsightsPort(SAMPLE_ITEMS), store, False,
        )
        assert ack
        assert outcome.tool == "instagram_insights"
        assert outcome.freshness == "cached"
        assert outcome.status == "ok"
        assert outcome.result_count == 2
    finally:
        db.close()


def test_instagram_adapter_empty_result_is_unverified() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _ack, outcome = enrich_content_insights_ack(
            "", DisabledInstagramInsightsPort(), store, False,
        )
        assert outcome.freshness == "unverified"
        assert outcome.status == "empty"
    finally:
        db.close()
