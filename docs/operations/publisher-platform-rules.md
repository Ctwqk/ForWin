# Publisher Platform Rules

Last verified: 2026-07-02.

This document records the platform rules that gate ForWin production publisher
automation. It is an operational safety document, not legal advice. When a
platform account page shows stricter current-account limits than these public
rules, the account page wins.

## Sources

Official or platform-owned sources checked:

- Fanqie help center: `https://fanqienovel.com/docs/8231/90559`
- Fanqie work-operation guide: `https://fanqienovel.com/docs/8231/90699`
- Fanqie longform publishing rules, second edition:
  `https://fanqienovel.com/writer/zone/article/7639950766869839897`
- Fanqie writer backend notice: `https://notice.fanqienovel.com/docs/9476/zuojiahoutai`
- Fanqie writer changelog: `https://fanqienovel.com/writer/zone/change-log`
- Qidian writer publish flow FAQ: `https://write.qq.com/ask/qfokgyc`
- Qidian chapter length FAQ: `https://write.qq.com/ask/qfoycqb`
- Qidian work information notice: `https://write.qq.com/portal/content/27817262201325501?feedType=2&lcid=74671304322038861`
- Qidian mobile signing guide: `https://write.qq.com/portal/content?caid=14626181805826801&feedType=2&lcid=35546823090990148`
- Qidian writer version notes: `https://write.qq.com/portal/version`
- Qidian logged-in editor frontend and read-only endpoints observed through the
  production publisher browser, including `/ccauthorweb/novel/iscancreatenovel`,
  `/ccauthorweb/daywords/getMonthDayWords`, and
  `/ccauthorweb/Chapter/getLastFourChapterPublishTime?CBID=...`
- Qidian editor frontend static bundle:
  `https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js`
- Qidian editor frontend source map:
  `https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js.map`
- Production read-only browser quota probe:
  `python scripts/probe_publisher_platform_quotas.py`

## Confirmed Rules

| Platform | Rule | Operational meaning |
| --- | --- | --- |
| Fanqie | Creating a work requires name, cover, intro, category, and content-safety review. A default cover can be used when no compliant cover is available. | `create_if_missing=true` must have category, intro, protagonist names, and a cover plan. Prefer existing bindings in production. |
| Fanqie | Intro limit is 500 characters in the current writer backend; ForWin keeps the stricter create-work preflight at 50-500 characters. | Do not generate ultra-short intros for Fanqie create-work flows. |
| Fanqie | Chapter title format supports a 5-30 character title in the backend guide. | Keep automated test chapter titles compact and timestamped. |
| Fanqie | Drafts may be saved and are not externally published or counted as work words. | `publish=false` is the normal production smoke path. |
| Fanqie | Longform creation quota, effective 2026-05-22: one author account may create only 1 longform work per natural day and at most 3 per natural month. | Do not use automated `create_if_missing=true` for routine production. If a creation test is explicitly run, it must be single-platform and single-work. |
| Fanqie | Longform update-work quota is author-level based: Lv.0/Lv.1 can update only 1 longform work per day, Lv.2/Lv.3 up to 3, and Lv.4+ up to 5. | Until the current account level is explicitly read from the backend, ForWin must assume the Lv.0/Lv.1 ceiling for publish planning. |
| Fanqie | Longform submitted publishing words are author-level based: daily `<1w/<2w/<5w` and monthly `<25w/<50w/<100w` for Lv.0/Lv.1, Lv.2/Lv.3, and Lv.4+ respectively. | Generated chapters around 3000-4500 Chinese chars are inside the Lv.0/Lv.1 daily word ceiling, but batch publishing can still exceed the work-count ceiling. |
| Fanqie | Publishing or editing chapters sends content through platform review; frequent edits can slow review/release. | Avoid repeated edits and avoid batch publish while validating automation. |
| Fanqie | Before signing, authors should update正文 and目录; signing evaluation starts at 20k words, second chance after 50k, third after 200k. | A one or two chapter ForWin smoke should not be treated as signing-ready. |
| Fanqie | Recommendation starts after signing; current public docs/changelog show the recommendation start window around 8w-15w or 10w-15w depending on page/version. | Do not expect recommendation, revenue, or traffic checks from short test uploads. |
| Fanqie | Uncontracted works can be visible after real-name verification; uncontracted works can be deleted/hidden from work management in the backend guide/changelog. | Test works should remain marked and minimal; deletion/hide is possible for unsigned works but not an automation cleanup step unless explicitly requested. |
| Qidian | Standard novel new-book review requires the first body-volume chapter to reach at least 1000 words. | Do not use sub-1000-word public publish tests on Qidian. |
| Qidian | A chapter cannot be empty; a single chapter must not exceed 20000 words, with 2000-6000 words recommended. | ForWin generated chapters around 3000-4500 Chinese chars are inside the recommended range. |
| Qidian | The logged-in editor frontend gates batch chapter import by file count: one batch imports at most 10 files, and the batch-import entry checks today's uploaded file count and blocks at 50 files for the day. | Treat Qidian file-import upload automation as capped at 10 files per batch and 50 imported files per natural day. This is a batch import/file quota, not proof of a daily public publish quota. |
| Qidian | Work information format: book title within 15 Chinese characters, reader-facing note within 32 characters, intro 5-500 characters, and content must avoid sensitive or infringing text. | `create_if_missing=true` needs a short title, valid intro, and safe metadata. |
| Qidian | Online signing can be invited by editors after new-book review; authors may apply after 100k words for male-channel Qidian/Chuangshi first sites and 50k words for listed female-channel sites. | Current short ForWin production tests are not signing-ready. |
| Qidian | Version notes include draft defaults, timed publish cancellation, signing status display, and a 2-hour interval for chapter unban requests. | Automation must not retry blocked chapter actions aggressively. |
| Qidian | Uncontracted works can be deleted in 作家助手; signed works require editor coordination. | Do not create throwaway signed/contract-affecting artifacts. |

## Unconfirmed Public Quotas

The Fanqie 2026-05-21 official longform publishing-rules article confirms
daily/monthly longform creation count, daily updated-work count, and daily/monthly
submitted-word quotas. The table is rendered visually on the official page, so
`scripts/probe_publisher_platform_quotas.py` records it as
`source_evidence=official_article_image_table`.

No official public page or logged-in read-only probe found on 2026-07-02 gave a
stable numeric quota for:

- Qidian new-book creation count per account
- Qidian chapter publish count per day or per hour
- Fanqie or Qidian draft count per book or account
- Fanqie or Qidian maximum number of publish attempts before risk control
- Fanqie hourly publish/update limits, if any, beyond the official natural-day
  and natural-month longform quotas

The 2026-07-02 production read-only quota probe opened six Fanqie pages and
nine Qidian pages/endpoints/resources through the shared logged-in publisher browser. It
saw no visible account blockers on the current dashboard, create-work pages, or
Qidian create-availability endpoint:

- Fanqie dashboard and create-work pages were accessible; the create-work page
  showed the expected intro counter and review rules, but no current
  "daily-create-limit" banner.
- Fanqie official longform publishing rules confirmed the longform daily/monthly
  creation, update-work, and submitted-word quotas above.
- Qidian dashboard and create-work pages were accessible; no current
  create-frequency or publish-frequency blocker was visible.
- Qidian `iscancreatenovel` returned current-account create availability
  `true`; this is current state, not a quota ceiling.
- Qidian day-words calendar returned current publish counters such as daily
  published words and chapter counts; this is current state, not a quota ceiling.
- Qidian editor frontend exposes a recent-publish-time endpoint
  (`getLastFourChapterPublishTime?CBID=...`); for the current bound work it
  returned "no latest published chapter", not a publish-frequency quota.
- Qidian editor source map confirms the frontend publish path:
  `publishChapter` posts publish or timed-publish requests, while
  `getLastFourChapterPublishTime` is used to render timed-publish "common time"
  shortcuts. This source-map evidence does not expose a stable numeric
  daily/hourly public publish quota.
- Qidian editor frontend validates chapter body length at 1-20000 words and
  warns that new-book review periods should avoid frequent publishing or chapter
  edits, but it does not expose a stable numeric daily/hourly publish limit.
- Qidian editor frontend confirms batch import/file limits: single batch <=10
  files and daily batch-import file count <50. The current bound work's
  read-only upload-count endpoint returned 0 files used today during the
  manual follow-up check.
- Qidian official/help pages confirmed word, intro, review, signing, and deletion
  rules, but did not expose a stable daily/hourly publish quota.

Because Qidian hard quotas and several draft/risk-control quotas were not
confirmed, ForWin production policy keeps the quota objective open and uses
conservative internal ceilings until a current account page or platform staff
notice confirms otherwise:

- no automated `create_if_missing=true` in routine production smoke
- no batch `publish=true`
- no more than one `publish=true` chapter per platform per operator-approved
  production experiment
- no `publish=true` without a passing publisher compliance review
- no `publish=true` if platform pages show risk control, captcha, MFA, missing
  permission, account abnormality, audit rejection, or login instability
- do not claim quota-confirmed `publish=true` while
  `scripts/probe_publisher_platform_quotas.py` reports `quota_incomplete`

## Current Account State

The July 2026 production longform smoke uploaded one generated chapter to each
platform with `publish=false`, `create_if_missing=false`, and existing safe work
bindings. Both upload jobs succeeded as drafts.

The latest read-only quota probe was run at `2026-07-02T06:36:11Z` and returned:

- `status`: `quota_incomplete`
- `blocked_items`: none
- Fanqie: 6/6 probed pages loaded, 17 quota/rule signals,
  `publish_quota_confirmed=true`, no visible current account blocker
- Qidian: 9/9 probed pages/endpoints/resources loaded, 11-12 quota/current-state/source-map signals
  depending on dynamic page text,
  `publish_quota_confirmed=false`, no visible current account blocker
- `publish_true_gate.allowed`: `false` because
  `numeric_publish_frequency_quota_unconfirmed`
- `publish_true_gate.confirmed_platforms`: `["fanqie"]`
- `publish_true_gate.unconfirmed_platforms`: `["qidian"]`

Audit-sync observations from the same run:

- Fanqie showed a revenue entry but did not show a signing/contract entry.
- Qidian did not show signing milestones.

Therefore there is no current evidence that the uploaded production smoke
created a signing state or contract workflow. If a future upload or audit sync
does surface signing, the process is:

1. Stop automatic publishing for that platform.
2. Record a redacted audit snapshot with platform, work binding, visible state,
   and timestamp.
3. Do not accept, reject, sign, fill personal data, or submit contracts through
   automation.
4. Ask a human operator to decide the signing path inside the platform account.
5. Resume automation only after the contract state and allowed update cadence
   are explicitly documented.

## Publish-True Gate

`publish=true` is allowed only when all of these are true:

- production baseline is `ok` for app, MCP, Swarm services, shared publisher
  browser, Fanqie login, Qidian login, and Discord QR policy
- the target platform has an existing work binding; `create_if_missing=false`
- the upload is a single chapter on a single platform
- platform-specific word and metadata rules above are satisfied
- publisher compliance reviewer exists and passes
- no active generation or upload job is already running for the same project
- `scripts/probe_publisher_platform_quotas.py` is run in the same production
  publisher browser session and does not report visible account blockers
- the operator has selected the exact project, platform, work binding, chapter,
  and body source
- the expected post-click state is either published or submitted for platform
  review, and the result will be verified from the platform page

If any item is not true, stop at `publish=false` draft upload or API preflight.
