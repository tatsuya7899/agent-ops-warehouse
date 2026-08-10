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

One **Scorecard** widget per metric, six in a row. This view always has
exactly one row (the latest snapshot), so no date filter is needed.

**Every tile needs a title** (Style tab → Graph title → on, then type
the text) — a bare field name like `evidence_ratio` doesn't tell a
first-time viewer what the number means. Also turn off "Show field
name" under the metric's label settings so the raw column name doesn't
linger next to the title you just wrote.

| Tile | Metric field | Format | Title (write in the dashboard's own language) |
|---|---|---|---|
| Publishing streak | `streak_weeks` | number | "streak, weeks — reference only" |
| Evidence done | `evidence_done` | number | "evidence completed (G1+G2)" |
| Evidence target | `evidence_target` | number | "evidence target (G1:4+G2:4)" |
| Evidence ratio | `evidence_ratio` | **percent, 0 decimals** | "evidence completion rate (%)" |
| Ships this month | `ships_this_month` | number, **conditional formatting: red/green (see below)** | "Ships this month (target: 4+)" |
| Publications, last 2 weeks | `publications_last_two_weeks` | number, **conditional formatting: red/green (see below)** | "publications, last 2 weeks (biweekly cadence)" |

`as_of_date` works as a small "data as of" text tile if the row feels
sparse.

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
- Title: **required** (Style tab → Graph title) — e.g. "monthly Ship
  velocity: articles + commits + X posts, with total as a line"

### C-3. KPI history — source: `dash_kpi_history`

- Chart type: **Time series**
- Dimension: `snapshot_date`
- Metrics: `streak_weeks`, `evidence_done`
- Note: one row per weekly load — the line starts sparse and densifies
  as loads accumulate. That is expected, not a data bug.
- Title: **required** — e.g. "streak (weeks) and evidence completed
  over time — two different scales on purpose"

### C-4. Agent activity — source: `dash_agent_activity`

- Chart type: **Column chart**
- Dimension: `week_start` → granularity **ISO Year Week**
- Metric: `sessions`
- Sort: `week_start` ascending
- Title: **required** — e.g. "weekly agent sessions"

### C-5. Publishing cadence — source: `dash_publish_cadence`

- Chart type: **Combo chart**
- Dimension: `month` → granularity **Year Month**
- Bars: `articles_published`
- Line (right axis): `avg_gap_days` (already rounded to 1 decimal;
  NULL months simply break the line — expected for single-publish months)
- Title: **required** — e.g. "monthly published articles + average gap
  since the previous publish"

**Acceptance criterion 2 in the SPEC ("P2-2") requires a title on every
component above, not just C-1** — a prior pass titled only the
scorecards and an adversarial review flagged the untitled charts as the
same "what am I looking at" problem, just relocated. Don't repeat that.

### C-6. "No target on evidence" note — static text box

Add one text element **next to (not overlapping) the evidence-related
tiles** (evidence done / target / ratio) with a single line explaining
why they carry no color signal — something to the effect of "evidence
progress has no target shown here; the real threshold changes monthly
and lives outside this dashboard." This is the on-canvas half of SPEC
"P2-2" acceptance criterion 3 — don't name the internal planning doc.

**Write this note in the dashboard's own display language** (see SPEC
"P2-2" §"画面内注記の言語方針" — as of v1.5 that's Japanese, matching
the scorecard titles; this English assembly doc stays English since its
audience is fork builders, not dashboard viewers). Position it close
enough to the evidence tiles that a viewer's eye catches it before
wondering why those three tiles are uncolored while ships/publications
are red or green — a floating note elsewhere on the canvas doesn't
satisfy this criterion.

## 2. Suggested wireframe (arrange freely — this is the human's half)

```
┌──────┬──────┬──────┬──────┬──────┬──────┐
│ C-1  │ C-1  │ C-1  │ C-6  │ C-1  │ C-1  │   evidence 3 tiles + note
│streak│ done │target│(note)│ratio │ships │   pinned beside them,
├──────┴──────┴──────┴──────┴──────┴──────┤   ships/pubs tiles carry
│              (or) C-1: pubs              │   the red/green signal
├───────────────────────────────────────────┤
│            C-2 (hero, titled)            │   ship velocity
├───────────┬───────────┬──────────────────┤
│  C-3      │  C-4      │  C-5             │   history · activity ·
│ (titled)  │ (titled)  │ (titled)         │   cadence — all titled
└───────────┴───────────┴──────────────────┘
```

(Exact grid is illustrative — the constraint that matters is C-6 sitting
next to the evidence tiles, and every chart carrying its own title.)

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
