# wxrks API changes

The published change tracker for the [wxrks API](https://dev.wxrks.com/), live at
**api-changes.wxrks.com**.

## It updates itself

A GitHub Action runs once a day, at **07:40 UTC** — 08:40 Dublin time in summer, 07:40 in winter
(GitHub cron has no timezone). It fetches the published API documentation, compares it against the
last recorded surface, and when something has moved it writes change log entries, rebuilds
`index.html`, and commits. The published site follows that commit, so it tracks the docs with no one
touching it.

Daily, not hourly: the change log is dated by day. Several runs in one day split that day's movement
across duplicate entries and write extra snapshots, so one run a day is what keeps the history
readable — one entry set per date, one snapshot per date.

`.github/workflows/track.yml` is the whole schedule. You can also run it on demand from the
**Actions** tab with **Run workflow**.

## What it will and won't write

Entries created by the Action state only what the diff found: an endpoint that disappeared, a
parameter that appeared, a response field that changed. Facts are grouped by endpoint — the route
once, then what happened to it — and every route links to its own section in the documentation.
Entries are flagged `auto` and the page marks them as detected automatically.

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

To replace an auto entry with reviewed wording, edit `data/api-changelog.json`: rewrite `title` and
`summary`, add an `action`, and drop the `"auto": true` flag. The provenance note disappears and the
entry reads as reviewed guidance. Then rebuild:

```bash
python3 tools/api-tracker/apitrack.py build --out .
```

## Layout

| Path | What it is |
|---|---|
| `index.html` | The published page. Generated. Self-contained: fonts inlined, no external requests. |
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

**GitHub disables scheduled workflows after 60 days of repository inactivity.** Commits from the
Action itself count as activity, so this only bites if the API stays frozen for two months. If the
page ever goes quiet for a suspiciously long time, check the Actions tab first.

## Contact

Giovanna Carla, Head of Customer Success — giovanna@wxrks.com
