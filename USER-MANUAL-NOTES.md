# User Manual Notes

Scratch pad for topics that should go into a proper user manual someday.

---

## How the Report Handles Partial and Gap-Filled Days

### The problem

The logger runs on a laptop (or eventually a Raspberry Pi). When the laptop is closed,
no data is collected. This creates two distinct situations:

1. **Today** — the current calendar day is always incomplete. Even if the laptop has
   been running all day, midnight hasn't arrived yet, so we only have a fraction of
   the day's data.

2. **Past days with gaps** — any previous day where the laptop was closed for more
   than `max_gap_hours` (default 4 hours, set in `config.ini`) will have one or more
   holes in the data.

### How the math works

Every %/day figure in the report is a *rate*, not a raw total. The formula is:

```
%/day = SOC drop ÷ logged hours × 24
```

So even a partial day produces a meaningful number — it's the annualised rate for
the time that was observed. This is the same math used for full days.

### Today (partial day)

- Today's bar in the discharge chart is shown in **light blue** with a blue border,
  distinct from the steelblue bars for complete days.
- Hovering over it shows: *"Partial day — rate will change"*
- Today is **excluded from the 7-day average and running average**. Only complete
  calendar days feed into those figures. This prevents a low-usage morning from
  dragging down your averages.

### Past days with laptop-closed gaps

When a gap exceeds `max_gap_hours`, the session is split in two. Both halves may
fall on the same calendar date. The daily rate for that date is calculated by
merging the segments:

```
drop  = drop_before_gap + drop_after_gap
hours = hours_before_gap + hours_after_gap   ← gap hours are NOT counted
%/day = drop ÷ hours × 24
```

These days appear as **complete days** in the chart and averages — they are not
flagged as partial because they are not "today."

### Is the gap-day rate accurate?

Roughly, yes. It is a sample extrapolated to 24 hours, which is the best estimate
possible without data.

It can be slightly misleading in edge cases:

| Laptop closed during… | Effect on %/day |
|---|---|
| Low-draw period (e.g., sleeping) | Slightly **inflated** — cheap hours skipped, costly hours over-represented |
| High-draw period (e.g., cooking) | Slightly **deflated** — high-draw hours missing from the sample |

For typical boondocking use the error is small. Once the Raspberry Pi is running
24/7, gaps disappear entirely.

### Summary

| Situation | Shown in chart | Included in averages? |
|---|---|---|
| Today (partial) | Light blue bar, partial-day label | No — excluded |
| Past day, complete data | Steelblue bar | Yes |
| Past day, laptop-closed gap | Steelblue bar (gap not visible) | Yes |

---
