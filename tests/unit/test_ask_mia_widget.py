"""Static invariants for the embeddable Ask Mia widget."""

from __future__ import annotations

import re
from pathlib import Path

WIDGET = Path("app/web/ask_mia.js")
ASSETS = Path("app/web/assets")


def _source() -> str:
    return WIDGET.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    needle = f"function {name}("
    start = source.index(needle)
    nxt = source.find("\n  function ", start + len(needle))
    if nxt < 0:
        nxt = len(source)
    return source[start:nxt]


def test_widget_source_is_present() -> None:
    assert WIDGET.is_file()
    source = _source()
    assert source.startswith("(function () {")
    assert "innerHTML" not in source
    assert "eval(" not in source
    assert "document.write" not in source


def test_panel_is_created_hidden() -> None:
    source = _source()
    assert "waBtn.hidden = true" in source
    assert "height:56px" in source
    assert "min-width:56px" in source
    assert "panel.hidden = true" in source
    assert source.index("panel.hidden = true") < source.index("function mount()")
    assert source.index("aria-expanded', 'false'") < source.index("function mount()")
    assert source.count("panel.hidden = false") == 1
    assert "panel.hidden = false" in _function_body(source, "openPanel")


def test_no_auto_open_timer_scroll_or_exit_intent() -> None:
    source = _source()
    lowered = source.lower()
    for needle in (
        "exit-intent",
        "exitintent",
        "exit_intent",
        "mouseleave",
        "setinterval",
        "auto-open",
        "autoopen",
        "auto_open",
        "addeventlistener('scroll'",
        'addeventlistener("scroll"',
        "addeventlistener('mouseout'",
        "addeventlistener('load'",
    ):
        assert needle not in lowered
    assert "setTimeout(mount, 0)" in source
    assert source.count("setTimeout(") == 1
    mount_fn = _function_body(source, "mount")
    assert "openPanel" not in mount_fn
    assert "initSession" not in mount_fn
    observer = source[source.index("IntersectionObserver") :]
    observer = observer[: observer.index("MutationObserver")]
    assert "openPanel" not in observer
    assert "section_viewed" in observer


def test_host_data_mia_open_opens_panel() -> None:
    source = _source()
    assert "data-mia-open" in source
    host = _function_body(source, "onHostOpenClick")
    assert "closest('[data-mia-open]')" in host
    assert "openPanel()" in host
    assert "preventDefault" in host
    tracking = _function_body(source, "setupFunnelTracking")
    assert "onHostOpenClick" in tracking
    assert "onCtaClick" in tracking


def test_session_created_only_on_first_open() -> None:
    source = _source()
    assert "opened = false" in source
    assert source.count("opened = true") == 1
    open_fn = _function_body(source, "openPanel")
    assert "opened = true" in open_fn
    assert "initSession()" in open_fn
    assert source.count("initSession()") == 2
    assert "setupFunnelTracking()" in _function_body(source, "mount")


def test_behavior_tracking_stays_wired_without_open() -> None:
    source = _source()
    tracking = _function_body(source, "setupFunnelTracking")
    assert "page_viewed" in source
    assert "section_viewed" in tracking
    assert "data-mia-section" in source
    assert "data-mia-cta" in source
    assert "data-mia-form" in source
    assert "form_started" in tracking or "form_started" in source
    assert "form_abandoned" in source
    assert "eventQueue" in source
    mount_fn = _function_body(source, "mount")
    assert "setupFunnelTracking()" in mount_fn
    assert mount_fn.index("setupFunnelTracking()") < mount_fn.index("fetchJson")


def test_whatsapp_green_and_assafweb_brand_tokens() -> None:
    source = _source()
    assert "#25d366" in source
    assert "#061b35" in source
    assert "#2f5f93" in source
    assert "#2563eb" in source
    assert "#F8FBFF" in source
    assert "#d9eeff" in source
    assert "#ask-mia-send{background:#2f5f93;color:#fff}" in source
    assert "linear-gradient(135deg,#2f5f93,#2563eb)" in source
    assert "#ask-mia-launcher:focus-visible{outline:2px solid #2563eb" in source


def test_widget_uses_only_assafweb_palette_colors() -> None:
    """Every hex in the widget must be an assafweb.com `:root` token.

    The allowed set is what www.assafweb.com actually ships, plus the WhatsApp
    brand green and an error red. A new colour here means the widget drifted
    away from the site it is embedded in.
    """
    allowed = {
        # assafweb.com :root tokens
        "#061b35",  # --ink
        "#2f5f93",  # --navy
        "#2563eb",  # --action
        "#7ba7d3",  # --steel
        "#d9eeff",  # --mist
        "#f8fbff",  # --paper
        "#2f5f9321",  # --line
        "#eef7ff",  # section tint
        "#fff",
        "#ffffff",
        "#ffffff59",  # FAB hairline on assafweb.com .whatsapp-fab
        "#2563eb59",  # FAB glow on assafweb.com .whatsapp-fab
        # non-palette by intent
        "#25d366",  # WhatsApp brand green
        "#b00",  # error text
    }
    found = {value.lower() for value in re.findall(r"#[0-9a-fA-F]{3,8}\b", _source())}
    assert found <= allowed, f"off-brand colours: {sorted(found - allowed)}"


def test_messages_use_chat_bubble_layout() -> None:
    """Sent/received bubbles + avatars; no React/shadcn in the embed."""
    source = _source()
    paint = _function_body(source, "paintMsg")
    send = _function_body(source, "sendMessage")
    assert "ask-mia-row ask-mia-row-" in paint
    assert "bubbleAvatar(role)" in paint
    assert "ask-mia-bubble-avatar" in source
    assert "flex-direction:row-reverse" in source
    assert ".ask-mia-user{background:#2f5f93;color:#fff" in source
    assert ".ask-mia-mia{background:#eef7ff;color:#061b35" in source
    assert "showLoading()" in send
    assert "hideLoading()" in send
    assert "unsplash" not in source.lower()
    assert "innerHTML" not in source
    assert 'face.textContent = "א"' in source or "face.textContent = 'א'" in source


def test_visible_ask_mia_pill_sits_at_true_bottom() -> None:
    """Clients see one bottom control: a labeled Ask Mia pill, not a second FAB."""
    source = _source()
    assert "clip:rect(0,0,0,0)" not in source
    assert "bottom:max(1.1rem,env(safe-area-inset-bottom,0px))" in source
    assert (
        "#ask-mia-launch-label{white-space:nowrap;font-size:.92rem;"
        "font-weight:800;color:#fff}"
    ) in source
    assert "display:inline-flex" in source
    assert ".whatsapp-fab{display:none!important}" in source
    assert "שאלו את מיה" in source
    assert "bottom:5.4rem" not in source


def test_accessibility_invariants() -> None:
    source = _source()
    assert "aria-expanded" in source
    assert "aria-controls" in source
    assert "aria-label" in source
    assert "min-height:44px" in source
    assert "min-width:56px" in source
    assert ":focus-visible" in source
    assert "panel.dir = 'rtl'" in source
    assert "prefers-reduced-motion" in source
    open_fn = _function_body(source, "openPanel")
    close_fn = _function_body(source, "closePanel")
    assert "aria-expanded', 'true'" in open_fn
    assert "aria-expanded', 'false'" in close_fn


def test_svg_mark_with_letter_fallback() -> None:
    source = _source()
    assert "createElementNS" in source
    assert "currentColor" in source
    assert "textContent = 'מ'" in source
    assert "aria-hidden" in source
    assert "MIA_MARK_PATH" in source


def test_standalone_svg_assets_match_brand() -> None:
    mark = (ASSETS / "mia-mark.svg").read_text(encoding="utf-8")
    icon = (ASSETS / "mia-icon.svg").read_text(encoding="utf-8")
    source = _source()
    assert "currentColor" in mark
    assert 'viewBox="0 0 32 32"' in mark
    assert "#2563eb" in mark
    assert "#061b35" in icon
    assert "#ffffff" in icon
    assert 'viewBox="0 0 64 64"' in icon
    assert 'rx="16"' in icon
    path = source.split("MIA_MARK_PATH =")[1].split("';")[0]
    assert "M7 23V8h4.2L16 16.8" in path
    assert "M7 23V8h4.2L16 16.8 20.8 8H25v15h-3.4V13.1L16 21.2l-5.6-8.1V23H7z" in mark


def test_mic_lives_in_composer_not_launcher() -> None:
    source = _source()
    assert "id = 'ask-mia-mic'" in source
    assert "actions.appendChild(micBtn)" in source
    assert "compose.appendChild(hint)" in source
    assert source.index("actions.appendChild(sendBtn)") < source.index(
        "actions.appendChild(micBtn)"
    )
    assert "launcher.appendChild(micBtn)" not in source
    assert "ask-mia-launch-mic" not in source
    assert "#ask-mia-launcher" in source
    assert "height:56px" in source
    assert "linear-gradient(135deg,#2f5f93,#2563eb)" in source
    assert "שאלו את מיה" in source
    send_css_idx = source.index("#ask-mia-actions button{")
    mic_css_idx = source.index("#ask-mia-mic{background:#d9eeff")
    launcher_css_idx = source.index("#ask-mia-launcher{")
    assert launcher_css_idx < send_css_idx
    assert send_css_idx < mic_css_idx
    assert "min-height:44px" in source[send_css_idx:mic_css_idx]
    assert "border-radius:.65rem" in source[send_css_idx:mic_css_idx]


def test_voice_hint_and_recording_copy() -> None:
    source = _source()
    assert "אפשר גם להקליט. זה יותר קל מלכתוב." in source
    assert "הקלטה למיה" in source
    assert "מקליטה… לחצו שוב לשליחה" in source
    assert "id = 'ask-mia-hint'" in source
    assert "ask-mia-status" in source
    assert "MIC_PERM" in source
    assert "MIC_ERR" in source


def test_widget_uses_mediarecorder_and_no_tts() -> None:
    source = _source()
    lowered = source.lower()
    assert "MediaRecorder" in source
    assert "getUserMedia" in source
    assert "MAX_RECORD_MS = 60000" in source
    assert "audio/webm" in source
    assert "audio/mp4" in source
    assert "/voice" in source
    assert "data.heard" in source
    assert "speechsynthesis" not in lowered
    assert "speechsynthesisutterance" not in lowered
    assert "webkitSpeechRecognition" not in source
    assert source.count("setTimeout(") == 1
    assert "setTimeout(mount, 0)" in source


def test_session_restore_skips_opening_and_retries_stale() -> None:
    source = _source()
    init = _function_body(source, "initSession")
    assert "loadStoredSession()" in init
    assert "restoreTranscript()" in init
    assert "!existing && !resumed" in init
    assert "cfg.opening" in init
    assert "createWebsiteSession()" in init
    send = _function_body(source, "sendMessage")
    assert "retryOnce" in send
    retry = _function_body(source, "retryOnce")
    assert "err.status !== 404" in retry
    voice = _function_body(source, "sendVoice")
    assert "retryOnce" in voice
    create = _function_body(source, "createWebsiteSession")
    assert "saveStoredSession(sessionId)" in create
    assert "TRANSCRIPT_KEY" not in create


_CUSTOMER_FEMININE_ONLY = (
    "שאלי",
    "נסי ",
    "לחצי",
    "תקליטי",
    "כתבי",
    "המשיכי",
    "הקליטי",
)
_CUSTOMER_MASCULINE_ONLY = (
    " אתה ",
    "ספר לי",
    "תקן אותי",
    "בוא נמשיך",
    "מה שאתה",
)


def test_widget_hebrew_addresses_both_genders() -> None:
    source = _source()
    for needle in _CUSTOMER_FEMININE_ONLY + _CUSTOMER_MASCULINE_ONLY:
        assert needle not in source, needle
    assert "שאלו את מיה" in source
    assert "לחצו" in source
    assert "להקליט" in source
    assert "כתבו" in source


def test_whatsapp_handoff_shows_a_card_instead_of_a_page_redirect() -> None:
    """The old flow jumped the whole page to a raw wa.me URL with no confirmation.

    A card keeps the conversation on screen, says what Assaf will already know, and opens
    WhatsApp in a new tab so the visitor can come back.
    """
    source = _source()
    handoff = _function_body(source, "handoff")
    assert "window.location.assign" not in handoff
    assert "placeWhatsAppCta" in handoff

    card = _function_body(source, "paintHandoffCard")
    assert "makeWhatsAppCta" in card
    assert "_blank" in _function_body(source, "makeWhatsAppCta")
    assert "noopener" in _function_body(source, "makeWhatsAppCta")
    # The link is still validated as a wa.me URL before it is ever rendered.
    assert "isWaMeUrl" in handoff


def test_handoff_card_explains_the_context_transfer_in_plural_hebrew() -> None:
    source = _source()
    card = _function_body(source, "paintHandoffCard")
    assert "אסף" in card
    assert "מיה לא עונה שם" in card
    assert "אסף מקבל את כל" not in card
    cta = _function_body(source, "makeWhatsAppCta")
    assert "נעבור" in cta
    # Customer-facing Hebrew stays 2nd-person plural / mixed so it addresses men and women.
    assert "פתח את" not in card
    assert "פתח את" not in cta


def test_whatsapp_click_claims_delivery_only_after_telegram_acceptance() -> None:
    source = _source()
    notify = _function_body(source, "notifyHandoffIssued")
    assert "response.json()" in notify
    assert "notification_status === 'delivered'" in notify
    assert "notification_status === 'failed'" in notify
    assert "אסף קיבל את תקציר השיחה" in notify
    assert "לא הצלחתי להעביר את השיחה לאסף" in notify
    assert "link.addEventListener('click', notifyHandoffIssued)" in _function_body(
        source, "makeWhatsAppCta"
    )


def test_widget_offers_whatsapp_on_handoff_as_well_as_offer_whatsapp() -> None:
    """HANDOFF used to claim a transfer with no CTA. The visitor had no way to reach Assaf."""
    apply = _function_body(_source(), "applyReply")
    assert "offer_whatsapp" in apply
    assert "handoff" in apply
    assert "placeWhatsAppCta" in apply or "requestWhatsAppCta" in apply
    assert "waBtn.hidden = false" not in apply


def test_widget_open_send_uses_textcontent_and_wa_me_href_only() -> None:
    """Playwright is not in this repo. Same guarantees from the widget source.

    Open paints the panel. Send writes bubble text via textContent, never innerHTML.
    The handoff CTA href is assigned only after isWaMeUrl accepts an https wa.me URL.
    """
    source = _source()
    assert "innerHTML" not in source
    open_fn = _function_body(source, "openPanel")
    assert "panel.hidden = false" in open_fn
    assert "initSession()" in open_fn
    send = _function_body(source, "sendMessage")
    assert "appendMsg('user', text)" in send
    assert "postText(text)" in send
    assert "applyReply" in send
    paint = _function_body(source, "paintMsg")
    assert "el.textContent = text" in paint
    assert "innerHTML" not in paint
    handoff = _function_body(source, "handoff")
    assert "isWaMeUrl(data.whatsapp_url)" in handoff
    assert "placeWhatsAppCta(data.whatsapp_url" in handoff
    cta = _function_body(source, "makeWhatsAppCta")
    assert "link.href = url" in cta
    assert "isWaMeUrl(url)" in cta
    wa = _function_body(source, "isWaMeUrl")
    assert "parsed.protocol === 'https:'" in wa
    assert "parsed.hostname === 'wa.me'" in wa


def test_whatsapp_offer_is_a_tappable_button_not_a_raw_url() -> None:
    """Live Ask Mia dumped a raw wa.me URL into the bubble as textContent.

    The visitor could not tap a proper CTA. Offer/handoff now strip that URL from
    visible text and attach one green https wa.me <a href> button instead.
    """
    source = _source()
    assert "innerHTML" not in source
    assert "textContent = url" not in source
    assert "textContent = data.whatsapp_url" not in source
    assert "appendMsg('mia', data.message)" not in source
    apply = _function_body(source, "applyReply")
    assert "stripWaMeUrls" in apply
    assert apply.index("stripWaMeUrls") < apply.index("appendMsg")
    assert "offer_whatsapp" in apply
    assert "handoff" in apply
    assert "isWaMeUrl(data.whatsapp_url)" in apply
    assert "placeWhatsAppCta" in apply
    assert "requestWhatsAppCta" not in source
    assert "status.textContent = WA_NA" in apply
    assert "waBtn.hidden = false" not in apply
    strip = _function_body(source, "stripWaMeUrls")
    assert "wa.me" in strip.replace("\\", "")
    assert "innerHTML" not in strip
    cta = _function_body(source, "makeWhatsAppCta")
    assert "isWaMeUrl(url)" in cta
    assert cta.index("isWaMeUrl(url)") < cta.index("link.href = url")
    assert "createElement('a')" in cta
    assert "נעבור לוואטסאפ" in cta
    assert "_blank" in cta
    assert "noopener" in cta
    assert "https://wa.me/" not in cta
    wa = _function_body(source, "isWaMeUrl")
    assert "parsed.protocol === 'https:'" in wa
    assert "parsed.hostname === 'wa.me'" in wa
    restore = _function_body(source, "restoreTranscript")
    assert "stripWaMeUrls" in restore
    paint = _function_body(source, "paintMsg")
    assert "el.textContent = text" in paint
    assert ".ask-mia-handoff-cta{" in source
    assert "background:#25d366" in source[source.index(".ask-mia-handoff-cta{") :][
        :400
    ]

