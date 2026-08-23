# Mia v1 communication operating model

ADR-017. One Mia brain. Channels are interfaces, not separate agents.

## Four answers

1. **Assaf talks privately with Mia in Telegram.**
2. **Customers talk with Mia on the AssafWeb website.**
3. **Personal WhatsApp stays human-only.** Mia does not roam the inbox.
4. **Hot / close-ready leads:** Mia stops selling and hands off to Assaf in Telegram.

## Diagram

```
                    ASSAF
                      ↕
                  TELEGRAM
                      ↕
                     MIA
                 ↙    ↓    ↘
            WEBSITE  EMAIL  WHATSAPP
               ↕       ↓       ↕
          PROSPECTS  APPROVAL  VERIFIED
                              BUSINESS
                              CONTINUATION
```

Separately:

```
ASSAF
  ↕
PERSONAL WHATSAPP CONTACTS

MIA DOES NOT INTERFERE
```

Hot lead:

```
HOT LEAD
→ MIA STOPS
→ TELEGRAM ALERT
→ ASSAF TAKES OVER
```

## Channel roles

| Surface | Role |
| --- | --- |
| Telegram | Private owner control. Numeric user-id allowlist. Greeting / unclassified text returns an operator status digest (counts + command menu). Voice notes in; text out. No TTS. No customer sales graph. |
| Website | Primary autonomous sales conversation. Widget + sales graph. |
| WhatsApp | Controlled continuation only. Valid path: website → secure handoff token → same lead. Production `MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true`. |
| Email | Read / classify / summarize / draft. Send stays approval-gated (`MIA_GMAIL_SEND` default false). |
| Instagram | Not a v1 autonomous sales inbox. Existing analytics/research may remain. |

## WhatsApp protection

States (`app/domain/conversation_scope.py`): `OWNER`, `MIA_BUSINESS`, `HUMAN_BUSINESS`, `PERSONAL`, `DO_NOT_AUTOMATE`, `UNKNOWN`.

- Unknown existing contact → human only.
- Personal / do-not-automate → no reply, no lead, no follow-up, no STT, no CRM analysis.
- A customer cannot self-promote into Mia-controlled state by wording.

## Website → WhatsApp

`POST /v1/website/sessions/{id}/handoff` issues a one-time token. WhatsApp inbound consumes it, keeps `lead_id`, marks `MIA_BUSINESS`, and Mia introduces herself once in Hebrew. Production (`MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE=true`): expired or tampered tokens are silent — no lead, no reply. A personal / do-not-automate number is never upgraded by a token.

On the website sales graph, `NextAction.OFFER_WHATSAPP` is the continuation offer after real business context (workflow known and pain P2+). It is **not** owner `HANDOFF`: no takeover, no follow-up cancel, no R3 approval. The widget highlights the existing WhatsApp button; the visitor still clicks. Server persists `whatsapp_handoff_offered` when the offer is spoken; `whatsapp_handoff` still fires when the token is issued.

## Takeover

`NextAction.HANDOFF` (owner-required / close-ready) → `HUMAN_TAKEOVER_REQUIRED`, cancel follow-ups, Telegram notify when the owner bot is configured. Assaf: “Take over {lead_id}” / “Give this lead back to Mia {lead_id}”. Name-only (“Take over Daniel”) is an Understanding Check — leads have no names.

Manual WhatsApp phone-app echo is **not claimed**. Meta can emit `smb_message_echoes` only after official Business App + Cloud API coexistence onboarding. Our webhook parser reads inbound customer `messages` only and does not subscribe to that field. Composio cannot supply it. Assaf takeover is Telegram commands plus the existing owner WhatsApp phrases.

## Transport (unchanged ADR-016)

Inbound WhatsApp stays Meta HMAC. One outbound sender: production `MIA_WHATSAPP_SENDER=composio` (`WHATSAPP_SEND_MESSAGE`) or Graph `direct`. Never both. Composio has no WhatsApp inbound-message trigger.

## Production safety

Do not flip `MIA_AUTOMATION_MODE` out of shadow as part of this model. WhatsApp prospect send stays shadow/approval-gated in production. Email send stays approval-gated.

## Intentionally deferred (v1)

- Instagram as an autonomous sales inbox.
- Meta Business App + Cloud API coexistence and `smb_message_echoes` (phone-app manual-reply detection).
- Gmail Drafts-folder create (`CREATE_DRAFT`). v1 draft is owner-facing thread summary persist; send stays `MIA_GMAIL_SEND=false` + approval.
- Name-only takeover (“Take over Daniel”) — leads have no names.
