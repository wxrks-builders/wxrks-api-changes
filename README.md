# wxrks API changes

The published change tracker for the [wxrks API](https://dev.wxrks.com/), live at
**api-changes.agents.wxrks.app**, which serves `index.html` from `main` (allow up to ~30 minutes
after a push). The `CNAME` file still names `api-changes.wxrks.com`, left from a GitHub Pages setup;
that host does not answer and the file is stale.

## It updates itself

A GitHub Action runs once a day, at **07:40 UTC** — 08:40 Dublin time in summer, 07:40 in winter
(GitHub cron has no timezone). It fetches the published API documentation, compares it against the
last recorded surface, and when something has moved it writes change log entries, rebuilds
`index.html`, and commits. The published site follows that commit, so it tracks the docs with no one
touching it.

Daily, not hourly: the change log is dated by day. Several runs in one day split that day's movement
across duplicate entries and write extra snapshots, so one run a day is what keeps the history
readable — one entry set per date, one snapshot per date.

**It commits every day, including the days nothing moved.** On a quiet day the run advances the check
date, rebuilds and commits that alone. A page still dated four days ago reads as abandoned whether or
not the API is stable, and the reader has no way to tell the two apart. A snapshot is written only on
a real change, so the commit message says which kind of day it was, and a second run on the same day
is a no-op.

`.github/workflows/track.yml` is the whole schedule. You can also run it on demand from the
**Actions** tab with **Run workflow**.

## What it will and won't write

Entries created by the Action state only what the diff found: an endpoint that disappeared, a
parameter that appeared, a response field that changed. Facts are grouped by endpoint — the route
once, then what happened to it — and every route links to its own section in the documentation.
Entries are flagged `auto` in the change log. The page no longer badges them one by one — "Where
this comes from" states that the entries come from the comparison, and what marks an entry as
reviewed is the **What to do** note, which only a person can write.

They deliberately carry **no guidance about what to do**. Nothing in this pipeline can know whether
a new field is required or how a change affects a particular integration, and publishing a guess to
customers is worse than publishing nothing. That judgement stays with a person.

## What counts as "Action needed"

Everything here is read off a published Postman collection: request examples, response examples, and
Postman's `optional` flag. Those are hand-maintained documentation, not the API's contract, so an
edited example is evidence something *may* have changed — not proof that a working call now fails.

**Action needed** is therefore reserved for changes the diff can settle on its own: the endpoint is
gone, authentication changed, the URL's path variables changed, or the request body switched between
two real formats. Anything resting on an example body or the `optional` flag is **Worth checking**
instead, and the reader decides. The rationale is in `CLASSIFICATION` in `apitrack.py`.

The failure this guards against is a false "Action needed". A developer who opens their integration
three times for nothing stops trusting the page, and then a real breaking change goes unread.

For the same reason the alert band at the top counts only what the **latest check** found, not every
breaking entry ever recorded. Otherwise one change from last month keeps the alert lit for good, and
an alert that is always on is an alert nobody reads. When the history holds action-needed entries but
today is clear, the band reads "Nothing new needs action" rather than contradicting the tab count.
The entries keep their own dates and tags, so nothing disappears from the feed.

## Nothing is published that the current documentation contradicts

A diff is only true as of its snapshot, and the documentation keeps moving under it. An
endpoint recorded as gone reappears; a field recorded as removed comes back. Left alone the page goes
on asserting it, and someone opens their integration for a change that is not there.

So every daily run re-checks the standing claims against the live surface and withdraws the ones it
disagrees with — a removal for an endpoint that is present, a new endpoint that is absent, a field
reported removed that is back. Only claims the surface can actually settle are judged; wording, a
rename or a section move are left alone, because absence of evidence is not contradiction and
withdrawing a true entry is its own failure.

Withdrawn facts are kept on the block under `withdrawn`, with `withdrawn_on`, rather than deleted.
The page stops saying it and there is still a record of what was said and when it was taken back. An
entry whose every fact has been withdrawn stops rendering and stops counting toward the alert.

**A moved endpoint is reported as a move.** The collection keeps a request's id when its path
changes, so the same id on both sides of a diff is one request that moved. Without that, a move came
out as a breaking removal plus an unrelated new endpoint in a different card — the page announcing
that an endpoint was gone when it was still there under a new path. It now reads as one fact on the
new route, telling the reader which URL to change.

One gap worth knowing: `build` renders without fetching, so a hand-edited change log can publish a
contradicted claim until the next daily `auto` withdraws it.

To replace an auto entry with reviewed wording, edit `data/api-changelog.json`: rewrite `title` and
`summary`, add an `action`, and drop the `"auto": true` flag. The `action` renders as the **What to
do** note, which is what tells a reader the entry was looked at by a person. Then rebuild:

```bash
python3 tools/api-tracker/apitrack.py build --out .
```

## Layout

| Path | What it is |
|---|---|
| `index.html` | The published page. Generated. Self-contained: fonts and the favicon inlined, no external requests. |
| `data/api-changelog.json` | The entries the page renders. This is the file to edit. |
| `data/api-snapshots/` | Recorded API surfaces, one file per date. A snapshot is written only when something moved, so each date means a real change. |
| `tools/api-tracker/` | The tracker: fetch, diff, classify, render. |
| `.github/workflows/track.yml` | The daily schedule. |

Do not hand-edit `index.html`. It is regenerated on every run and your edit would be lost.

## Commands

```bash
python3 tools/api-tracker/apitrack.py auto --out .   # what the Action runs
python3 tools/api-tracker/apitrack.py status         # is the published page current?
python3 tools/api-tracker/apitrack.py diff           # compare the last two snapshots
python3 tools/api-tracker/apitrack.py build --out .  # rebuild after editing the change log
```

Python 3.9+, standard library only. Nothing to install.

## Two things worth knowing

**It tracks the documentation, not the API.** A change that ships without the docs changing is
invisible here. The page says so and asks readers to report it.

**GitHub disables scheduled workflows after 60 days of repository inactivity.** Since the Action
now commits the check date every day, its own commits keep the repository active and the schedule
alive. If the page ever goes quiet for more than a day, check the Actions tab first.

## Contact

Giovanna Carla, Head of Customer Success — giovanna@wxrks.com
