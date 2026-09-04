# ADR-008 Today-vs-baseline = previous 7 completed local days' daily average

- **Status:** accepted
- **Date:** 2026-08-21
- **Assaf:** ADOPT

**Context**
Bible §20.2 requires comparing today (partial day) to a baseline. Partial-day spend/impressions/clicks must not trigger anomaly investigation because full-day baselines would produce false positives.

**Decision**
Baseline = previous seven **completed** local-calendar days (since=D-7, until=D-1 inclusive Meta date strings). Display is read-only: `date_preset="today"` vs baseline `time_range` from `baseline_7d_time_range`. Additive metrics (spend, impressions, clicks) show 7-day total ÷ 7; CTR compares aggregate ratios without dividing by 7. Missing paired metrics omitted. This comparison does not create an anomaly, change recommendation priority, or persist `CampaignRecommendation`.

**Consequences**
Owner analytics ack may append one informational Hebrew line after the recommendation. Two extra Meta reads when settings exist. No Meta writes. `FakeMetaAdsPort` uses explicit `time_range_snapshots` for baseline range distinct from previous-7d compare range.

**Alternatives considered**
Rolling 7d including today — rejected; partial day skews average. Same window as `previous_7d_time_range` — rejected; that window is D-14..D-8 for 7d-vs-previous-7d anomaly compare, not today baseline. Treat today-under-baseline as anomaly — rejected; false positives on partial days.
