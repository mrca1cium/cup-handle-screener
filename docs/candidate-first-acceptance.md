# Candidate-first refresh: offline acceptance

2026-11-21. Draft PR #5, `feature/candidate-first-refresh`. No production rollout.

## Scope and fixes

- Fixed an uninitialized safety flag in the real batch path which the former
  pure scheduling tests did not exercise despite green CI.
- P0 market instruments now share the same 250-request queue; there is no
  additional unbounded market refresh after it. P1 includes current candidates
  and candidates seen during the last 20 exchange sessions. P2 includes cached
  Stage 2 passes with RS >= 80, even when excluded from display for stale data.
  P2 and general stocks receive reserved capacity when eligible work exists.
- Priority freshness uses the latest completed NYSE session with a one-hour
  post-close provider grace period. Current bars are not re-requested just
  because `fetched_at` is old. Existing 12-hour unchanged-bar retry throttling
  and 30-day insufficient-history suppression remain.
- Twelve Data reservations are individual timestamped events in a rolling
  24-hour window, including requests across sequential batches. The local
  limit remains 320 credits, with eight credits reserved per call and at least
  65 seconds between calls. Failures never refund reservations.
- The batch checks credit headers on every outcome, including unavailable,
  insufficient, malformed response and transport failures. Missing/invalid or
  above-eight costs quarantine the ledger persistently. A crash leaving a
  pending request also blocks future Twelve Data calls, even after 24 hours.
  Larger observed costs are recorded. Response-cost globals reset before calls.
- An exclusive process lock covers the batch and ledger commissioning.
  Lock contention, persistence failure, unknown costs and rate limits return
  the existing runner's retry/no-publication exit code 2. Another provider's
  missing credential or blocked budget does not prevent eligible StashGamma
  work. No unmetered Twelve Data `/stocks` bootstrap is performed by the batch.
- Additive output fields expose each candidate's data date, the expected
  session, full snapshot data-status classifications and last attempt status.
  Successful download and successful current-bar update are separate counters.
  Existing scoring code and legacy result/chart fields are preserved.

## Verification

Run the complete suite with network blocked; CI uses Python 3.9:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover rolling-window boundaries, two sequential batches, exclusive locks,
unknown and excessive costs, every response/error path, missing/corrupt state,
crash recovery, commissioning cooldown, queue capacity, history retention,
fairness, calendars, data diagnostics and the real batch orchestration path.

Read-only real-cache comparison used the Mac scheduler's Python 3.9.6, 1,912
readable caches and a frozen 1,959-symbol universe (1,883 caches in that universe).
Both builders used the same data and locally frozen display metadata. HTTP was
disabled and output writes intercepted in memory:

| Comparison | Result |
| --- | --- |
| MATCH rows, excluding additive `data_date` | All 6 identical |
| WATCH rows, excluding additive `data_date` | All 22 identical |
| charts.json | Identical |
| Existing top-level output fields | Identical |
| Stage 2 / RS >= 80 refresh hints | 177 |
| Disk/cache/state writes | 0 |
| Provider requests | 0 |

The frozen metadata simulation selected all 28 candidates in the first 250
under candidate-first ranking, versus 5 with a simple oldest-bar-date ranking
over cached snapshot stocks. This is selection coverage, not updated bars.
Including eligibility, retry throttling, P2 hints and provider budgets:

| Scenario | Selected jobs | Candidate jobs | Twelve Data reserved credits |
| --- | ---: | ---: | ---: |
| Existing uninitialized ledger | 132 StashGamma | 18/28 | 0 |
| Hypothetical commissioned ledger after cooldown | 172 total | 28/28 | 320 |

The latter scenario is conditional, not a claim of provider success. All 28
published candidates still lacked the 2026-11-20 bar at the snapshot. No live
refresh was run. There is no guarantee of daily full-universe coverage.

## Commissioning and rollout gates

The existing production state has no Twelve Data ledger. Do not manufacture a
zero-usage baseline or automatically migrate an ambiguous legacy ledger.

`scripts/initialize_twelvedata_ledger.py --state PATH` only inspects state.
After separately approving rollout, isolating the API key from **all** other
processes/machines/services and confirming the account, an operator can use
`--initialize --exclusive-key`. That deliberately consumes the entire local
budget in the ledger and requires a full 24-hour quiet period. The same lock
prevents initialization while a batch owns state. This commissioning action
was tested only against temporary state; it was not run on production.

Read-only inventory inspected 674 source/config/LaunchAgent files in the APEX,
`.config`, LaunchAgents and TradingView project scopes. Only the known APEX
module/runner/config referenced Twelve Data. This does **not** establish that
no other machine or external service uses the key; operator confirmation is
still required. No credential value was logged or exported.

Official provider documentation describes one credit for one-symbol
`/time_series`, minute resets, Basic daily reset at 00:00 UTC and currently an
800-credit Basic daily limit. It also says `/api_usage` itself costs one credit.
These are documentation facts, not verification of this account. Keep the
more conservative local 320-credit bound and strict unknown-weight stop.
No authenticated `/api_usage` or market-data call was made for this review.

- https://support.twelvedata.com/en/articles/5615854-credits
- https://support.twelvedata.com/en/articles/5713553-control-over-api-usage
- https://twelvedata.com/docs/introduction/libraries

Mac validation observed an existing urllib3/LibreSSL compatibility warning.
Offline results passed, but live HTTP transport and actual account response
headers remain unverified. Do not mark production ready solely from green CI.

Main, Mac launchd settings, existing hosted automations, API subscription limits,
credentials, production caches and published JSON were not changed. VCP work
remains separate. Merge, deployment, commissioning and a live refresh still
require separate authorization.
