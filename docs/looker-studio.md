# Looker Studio dashboard (P2) — assembly sheet

Division of labor: **the repo builds the components, a human arranges them.**
Every aggregation, rename, ratio and rounding already happened in the
`dash_*` views (dbt-tested, 71/71 green) — Looker Studio needs **zero
calculated fields and zero blends**. Each component below is "pick the
view, pick the chart type, drop the listed fields into the listed slots".

## 0. Setup (once, ~3 min)

1. lookerstudio.google.com → **Create → Report** → Add data → **BigQuery**.
2. Project `agent-ops-warehouse` → dataset `marts` → add these five as
   separate data sources:
   `dash_kpi_current` · `dash_ship_velocity` · `dash_kpi_history` ·
   `dash_agent_activity` · `dash_publish_cadence`
3. No field editing needed anywhere. All dates are `DATE`; set the
   display granularity per component as noted.

## 1. Components

### C-1. KPI scorecards (top row) — source: `dash_kpi_current`

One **Scorecard** widget per metric, five in a row. This view always has
exactly one row (the latest snapshot), so no date filter is needed.

| Tile | Metric field | Format |
|---|---|---|
| Publishing streak | `streak_weeks` | number ("weeks" in the tile label) |
| Evidence done | `evidence_done` | number (label it "of 8" or show target beside it) |
| Evidence target | `evidence_target` | number (optional — skip if you label the previous tile) |
| Evidence ratio | `evidence_ratio` | **percent, 0 decimals** |
| Ships this month | `ships_this_month` | number, **conditional formatting: red/green (see below)** |

(`publications_last_two_weeks` is available as a sixth tile — if you add
it, apply the matching conditional formatting from below. `as_of_date`
works as a small "data as of" text tile.)

**Conditional formatting (Style tab → Conditional formatting → Add):**
Looker Studio scorecards can only condition on the tile's own metric
value, not on a separate field — so `ship_status`/`cadence_status`
can't be wired in directly as the trigger. Instead, add two rules that
mirror the same thresholds those columns encode:

- `ships_this_month`: rule "next value or greater, 4" → green; rule
  "less than, 4" → red (mirrors `ship_status`)
- `publications_last_two_weeks`: rule "next value or greater, 1" →
  green; rule "less than, 1" → red (mirrors `cadence_status`)

`ship_status`/`cadence_status` remain in the data source as the single
computed source for these thresholds (dbt, not Looker) — the rules
above just restate the same fixed numbers as a native comparison
because that's the only binding Looker Studio's scorecard supports. See
`SPEC` "P2-2" for why `evidence_*` intentionally has no such rule.

### C-2. Ship velocity (hero chart) — source: `dash_ship_velocity`

- Chart type: **Combo chart** (stacked bars + line)
- Dimension: `month` → granularity **Year Month**
- Bar series (stacked): `articles`, `commits`, `x_posts`
- Line series (right axis): `total`
- Sort: `month` ascending

### C-3. KPI history — source: `dash_kpi_history`

- Chart type: **Time series**
- Dimension: `snapshot_date`
- Metrics: `streak_weeks`, `evidence_done`
- Note: one row per weekly load — the line starts sparse and densifies
  as loads accumulate. That is expected, not a data bug.

### C-4. Agent activity — source: `dash_agent_activity`

- Chart type: **Column chart**
- Dimension: `week_start` → granularity **ISO Year Week**
- Metric: `sessions`
- Sort: `week_start` ascending

### C-5. Publishing cadence — source: `dash_publish_cadence`

- Chart type: **Combo chart**
- Dimension: `month` → granularity **Year Month**
- Bars: `articles_published`
- Line (right axis): `avg_gap_days` (already rounded to 1 decimal;
  NULL months simply break the line — expected for single-publish months)

### C-6. "No target on evidence" note — static text box

Add one text element near the KPI scorecard row (C-1) with a single line
along these lines: *"Evidence progress has no target shown — its real
threshold changes monthly and lives outside this dashboard."* This is
the on-canvas half of SPEC "P2-2" acceptance criterion 3 — don't name
the internal planning doc here, keep it to that one sentence.

## 2. Suggested wireframe (arrange freely — this is the human's half)

```
┌──────┬──────┬──────┬──────┬──────┐
│ C-1  │ C-1  │ C-1  │ C-1  │ C-1  │   scorecard row
├──────────────────────────────────┤
│         C-6 (note, small)        │   "no target on evidence" text
├──────┴──────┴──────┴──────┴──────┤
│            C-2 (hero)            │   ship velocity
├───────────┬───────────┬──────────┤
│   C-3     │   C-4     │   C-5    │   history · activity · cadence
└───────────┴───────────┴──────────┘
```

## 3. Sharing

Private by default. "Anyone with the link can view" is a publish action —
it goes through the same review gate as any public artifact.

## 4. Why the dashboard itself is not code

Looker Studio has no usable IaC surface on the free tier. Everything
upstream is reproducible code (Terraform + loader + dbt + tested views);
the dashboard is a ~10-minute manual assembly documented here, which a
fork can follow verbatim.

## 5. What this dashboard is for (and isn't)

This is evidence that the BigQuery + dbt + Looker stack runs, not a
decision-making instrument. There are no target/pass-fail thresholds
shown here on purpose — those live in a private planning document and
change monthly; baking them in here would create two sources of truth.
See the project SPEC ("P2-2" section) for the accepted-as-done definition.
