# FIXTURES — YouTube Analytics API responses (NOT real data)

Every JSON file in this directory is a **hand-written test fixture**.
No file here was captured from a real channel, no number here is a
real measurement, and no credential is involved. They exist so the
Round 5 analytics ingestion acceptance tests can drive the whole
sync → storage → read-API path through a mocked HTTP transport with
payloads shaped *exactly* like the real APIs return.

Shapes reproduced (from Google's API references):

* Analytics API v2 `reports.query` — a
  `youtubeAnalytics#resultTable`: `kind` + `columnHeaders`
  (`name` / `columnType` / `dataType`) + `rows` as positional arrays
  in header order. Real wire metric names are used
  (`estimatedMinutesWatched`, `comments`, `audienceWatchRatio`, …) —
  never the task-facing aliases.
* Data API v3 `channels.list?part=statistics` — a
  `youtube#channelListResponse` whose `statistics` counters are
  **strings**, as the real API returns them.

Fixture channel / videos (invented, internally consistent):

* channel: `UC1234567890123456789012`
* videos: `vidA1234567` (established performer), `vidB1234567`
  (mid performer), `vidZero1234` (the zero-views edge case)

Files:

| file | what it is |
|---|---|
| `channel_totals.json` | Analytics resultTable, no dimension: period totals for the channel |
| `per_video_metrics.json` | Analytics resultTable, `dimensions=video`: one row per video, **including an explicit all-zero row for `vidZero1234`** |
| `retention_full.json` | Analytics resultTable, `audienceWatchRatio` × `elapsedVideoTimeRatio`, full coverage 0.0 → 1.0 |
| `retention_early_end.json` | Same report, but the curve **ends early**: last `elapsedVideoTimeRatio` is 0.62 (< 1 coverage — the API returns no points past where data exists) |
| `empty_rows.json` | Analytics resultTable with valid headers and `"rows": []` (a range/channel with no data — absence, not zeroes) |
| `data_api_channels_list.json` | Data API v3 lifetime statistics for the fixture channel (string counters) |
