# Sample queries — live output

Captured 2026-07-28 against the real warehouse (sandbox mode). Text output is committed instead of screenshots — harder to fake, easier to diff. The `subject` column is never selected on public surfaces (privacy boundary).

```
═══ monthly_activity.sql ═══
+---------+---------+--------------+----------+
|  month  | commits | active_repos | articles |
+---------+---------+--------------+----------+
| 2026-07 |     117 |            4 |       15 |
| 2026-03 |      25 |            1 |        0 |
| 2026-02 |       3 |            1 |        0 |
| 2026-01 |      16 |            1 |        0 |
| 2025-12 |       1 |            1 |        0 |
| 2025-06 |       1 |            1 |        0 |
| 2025-05 |      25 |            1 |        0 |
+---------+---------+--------------+----------+

═══ lesson_flow.sql ═══
+--------+---------+------------+------------+
| status | lessons |   oldest   |   newest   |
+--------+---------+------------+------------+
| active |      20 | 2026-07-04 | 2026-07-28 |
+--------+---------+------------+------------+

═══ load_ledger_audit.sql ═══
+---------------------+---------------------+-------------+--------------------------------------------------------------------------------+
|       run_at        |       source        | rows_loaded |                                exclusions_note                                 |
+---------------------+---------------------+-------------+--------------------------------------------------------------------------------+
| 2026-07-28 22:35:54 | raw_kpi_snapshots   |           1 | all KPI fields computed via generate_status.py                                 |
| 2026-07-28 22:35:54 | raw_session_stats   |          10 | scanned 1 session dir(s); skipped 0 excluded dir(s): none; skipped_lines=11767 |
| 2026-07-28 22:35:53 | raw_x_posts         |           1 | skipped 0 unrecorded/unparseable row(s): none                                  |
| 2026-07-28 22:35:53 | raw_metrics_monthly |           1 | parsed 1 monthly row(s) from the Section 1 summary table                       |
| 2026-07-28 22:35:53 | raw_lessons         |          20 | skipped 1 filename-convention violation(s): LESSON_TEMPLATE.md                 |
| 2026-07-28 22:35:53 | raw_articles        |          15 | skipped 0 filename-convention violation(s): none                               |
| 2026-07-28 22:35:53 | raw_git_commits     |         197 | scanned 5 repo(s); skipped 0 not in allowlist: none                            |
+---------------------+---------------------+-------------+--------------------------------------------------------------------------------+

═══ publishing_cadence.sql ═══
+----------+----------+
| iso_week | articles |
+----------+----------+
| 2026-W31 |        2 |
| 2026-W30 |        2 |
| 2026-W29 |        2 |
| 2026-W28 |        6 |
| 2026-W27 |        3 |
+----------+----------+

```
