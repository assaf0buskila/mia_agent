# Website minimization report (recommendation only)

**Date:** 2026-08-22  
**Scope:** PRD §7.1 vs current AssafWeb homepage (`LandingPage.tsx`). **No deletes.** No Mia git-push.

## Competing conversion paths on the homepage

AssafWeb currently exposes several parallel ways to contact Assaf. Each path optimizes for a different moment, but together they split attribution and bypass Mia on some routes.

| Path | Location | Behavior today | Mia impact |
| --- | --- | --- | --- |
| Ask Mia widget | Injected script | Session, funnel events, optional WhatsApp handoff token | **Primary** Mia-owned path |
| Hero / contact WhatsApp | Hero + contact section | Direct `wa.me` or similar | No handoff token; new lead context |
| FAB WhatsApp | Fixed action button | Raw `wa.me` | Bypasses Mia handoff |
| Contact form | `data-mia-form` | Opens WhatsApp with PII in query string | **Bypasses Mia handoff**; posts `form_started` / `form_abandoned` only |

## PRD §7.1 alignment

The Bible already calls for minimization: compress services copy, merge about/process where possible, keep four FAQ items. This report does **not** propose removing WhatsApp CTAs outright — Assaf may want multiple entry points for trust and mobile habit.

## Recommendation (later, after Assaf approval)

1. **Unify form + FAB onto widget handoff** — When the visitor submits the contact form or taps FAB, route through Mia `POST .../handoff` and reuse the same opaque token the widget uses, instead of embedding name/phone in a `wa.me` query string.
2. **Keep one visible human WhatsApp escape hatch** — e.g. hero only, if Assaf wants a non-chat path; label it clearly so analytics can segment `cta_click` vs `whatsapp_handoff`.
3. **Do not auto-rewrite** — Mia may **propose** exact before/after copy via `website_edit` approval; Cursor applies in AssafWeb after Assaf says yes.

## Out of scope for this report

- Deleting sections or CTAs
- Adding GA4 gtag to Next (product decision)
- AWS / production deploy

## Metric to track after unification

- Ratio of `whatsapp_handoff` canonical events to raw WhatsApp CTA clicks (should rise when form/FAB use handoff)
- `website_funnel_drop` on owner analytics (existing Mia anomaly)
