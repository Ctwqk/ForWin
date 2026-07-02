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
- Fanqie writer backend notice: `https://notice.fanqienovel.com/docs/9476/zuojiahoutai`
- Fanqie writer changelog: `https://fanqienovel.com/writer/zone/change-log`
- Qidian writer publish flow FAQ: `https://write.qq.com/ask/qfokgyc`
- Qidian chapter length FAQ: `https://write.qq.com/ask/qfoycqb`
- Qidian work information notice: `https://write.qq.com/portal/content/27817262201325501?feedType=2&lcid=74671304322038861`
- Qidian mobile signing guide: `https://write.qq.com/portal/content?caid=14626181805826801&feedType=2&lcid=35546823090990148`
- Qidian writer version notes: `https://write.qq.com/portal/version`

## Confirmed Rules

| Platform | Rule | Operational meaning |
| --- | --- | --- |
| Fanqie | Creating a work requires name, cover, intro, category, and content-safety review. A default cover can be used when no compliant cover is available. | `create_if_missing=true` must have category, intro, protagonist names, and a cover plan. Prefer existing bindings in production. |
| Fanqie | Intro limit is 500 characters in the current writer backend; ForWin keeps the stricter create-work preflight at 50-500 characters. | Do not generate ultra-short intros for Fanqie create-work flows. |
| Fanqie | Chapter title format supports a 5-30 character title in the backend guide. | Keep automated test chapter titles compact and timestamped. |
| Fanqie | Drafts may be saved and are not externally published or counted as work words. | `publish=false` is the normal production smoke path. |
| Fanqie | Publishing or editing chapters sends content through platform review; frequent edits can slow review/release. | Avoid repeated edits and avoid batch publish while validating automation. |
| Fanqie | Before signing, authors should update正文 and目录; signing evaluation starts at 20k words, second chance after 50k, third after 200k. | A one or two chapter ForWin smoke should not be treated as signing-ready. |
| Fanqie | Recommendation starts after signing; current public docs/changelog show the recommendation start window around 8w-15w or 10w-15w depending on page/version. | Do not expect recommendation, revenue, or traffic checks from short test uploads. |
| Fanqie | Uncontracted works can be visible after real-name verification; uncontracted works can be deleted/hidden from work management in the backend guide/changelog. | Test works should remain marked and minimal; deletion/hide is possible for unsigned works but not an automation cleanup step unless explicitly requested. |
| Qidian | Standard novel new-book review requires the first body-volume chapter to reach at least 1000 words. | Do not use sub-1000-word public publish tests on Qidian. |
| Qidian | A chapter cannot be empty; a single chapter must not exceed 20000 words, with 2000-6000 words recommended. | ForWin generated chapters around 3000-4500 Chinese chars are inside the recommended range. |
| Qidian | Work information format: book title within 15 Chinese characters, reader-facing note within 32 characters, intro 5-500 characters, and content must avoid sensitive or infringing text. | `create_if_missing=true` needs a short title, valid intro, and safe metadata. |
| Qidian | Online signing can be invited by editors after new-book review; authors may apply after 100k words for male-channel Qidian/Chuangshi first sites and 50k words for listed female-channel sites. | Current short ForWin production tests are not signing-ready. |
| Qidian | Version notes include draft defaults, timed publish cancellation, signing status display, and a 2-hour interval for chapter unban requests. | Automation must not retry blocked chapter actions aggressively. |
| Qidian | Uncontracted works can be deleted in 作家助手; signed works require editor coordination. | Do not create throwaway signed/contract-affecting artifacts. |

## Unconfirmed Public Quotas

No official public page found on 2026-07-02 gave a stable daily/hourly numeric
quota for:

- new-book creation count per account
- chapter publish count per day or per hour
- draft count per book or account
- maximum number of publish attempts before risk control

Because those hard quotas were not publicly confirmed, ForWin production policy
uses conservative internal ceilings until a current account page or platform
staff notice confirms otherwise:

- no automated `create_if_missing=true` in routine production smoke
- no batch `publish=true`
- no more than one `publish=true` chapter per platform per operator-approved
  production experiment
- no `publish=true` without a passing publisher compliance review
- no `publish=true` if platform pages show risk control, captcha, MFA, missing
  permission, account abnormality, audit rejection, or login instability

## Current Account State

The July 2026 production longform smoke uploaded one generated chapter to each
platform with `publish=false`, `create_if_missing=false`, and existing safe work
bindings. Both upload jobs succeeded as drafts.

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
- the operator has selected the exact project, platform, work binding, chapter,
  and body source
- the expected post-click state is either published or submitted for platform
  review, and the result will be verified from the platform page

If any item is not true, stop at `publish=false` draft upload or API preflight.

