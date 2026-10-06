# Observation gate: FIRST_WEEKLY_AUTOMATION_PENDING = 2026-10-11

Production cutover (the app reading the public data site) and the weekly automation are **two separate gates**.
The cutover was verified on 2026-10-07. The first scheduled Sunday run has not happened yet, so it cannot be claimed as verified.

## What must be observed on Sunday 2026-10-11 (Asia/Taipei)

| Time | Normal | If the exchanges have nothing new yet |
|---|---|---|
| 10:00 | SUCCESS, new release, `manifest.json` advanced (both markets on Friday 2026-10-09) | WAITING |
| 14:00 | NOOP, 0 external requests | SUCCESS if new data arrived, else WAITING |
| 20:00 | NOOP, 0 external requests | NOOP after a success; **FAILED** if there was still nothing (Last Known Good unchanged) |

Three attempts without a publication: the last one is FAILED, `manifest.json` is unchanged, the app shows STALE from Monday 2026-10-12.
That is the designed failure path, not a defect of the automation, but somebody must find out why the exchanges gave nothing.

## How to check

```
python scripts/check_weekly_run.py --slot 2026-10-11
```

Reads only the public site. Exit 0 = behaved as designed (PASS, or NO_NEW_DATA on the honest failure path), 1 = unexpected, 2 = pending / in progress.
Also open `/valuation/data-health` on production: "最近一次排程執行", "最近一次成功發布", freshness and missed weekly updates.

## Things to look at if it is not PASS

* Actions tab of `IFAYU/yichuang-market-data`: did the three scheduled runs start at all (GitHub may delay or, after 60 days without activity, disable schedules)?
* `health.json`: `failureReason` of each attempt.
* Did TWSE and TPEx both move to 2026-10-09? (TWSE's open data was a day behind TPEx on 2026-10-06.)
