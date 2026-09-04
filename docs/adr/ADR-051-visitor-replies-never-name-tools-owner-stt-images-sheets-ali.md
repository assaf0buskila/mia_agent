# ADR-051 Visitor replies never name tools; owner STT/images/Sheets aliases

- **Status:** accepted
- **Date:** 2026-09-03
- **Assaf:** ADOPT (live close-up after PRs 14-16)

**Context**
PR 15 named visitor tools and leaked `knowledge_search`, Search Console, and JSON-LD
into the widget, including broken RTL (`.Search Console או JSON-LD`). Asking for
a voice agent hijacked into widget STT. Owner voice notes and images were dropped
or answered as empty. Sheets only recovered after the English brand name.

**Decision**
Visitor replies never print tool names or the Hebrew tool-status word רץ; scrub
even if a model emits them. Empty knowledge does not mention GSC/JSON-LD.
`סוכן קולי לאתר` sells the AssafWeb product. Widget הקלטה still transcribes on
mobile and desktop; mic fail is one short line to type. Owner Telegram transcribes
voice then answers, and describes images. sheets / Google sheets / גוגל שיטס /
האקסל / Contacts / CRM are the locked Contacts + Activity workbook and run on
the first ask. Instagram lines need caption, date, permalink, and account, or
say the API omitted identity. No invented metrics.

**Consequences**
SITE honesty is "no invented numbers", not "name the slug". Owner first Sheets
ask prefetches CRM. Timeout still says still checking, then the real tabs.

**Alternatives considered**
Keep naming visitor tools for honesty — rejected; live leak proved slugs are not
visitor copy. Wait for "Google sheets" before CRM — rejected; aliases are the
same locked sheet.
