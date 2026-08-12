# wxrks API changes

The published change tracker for the [wxrks API](https://dev.wxrks.com/), live at
**api-changes.wxrks.com**.

This repository holds **only the built page**. It is deliberately kept separate from the internal
tooling that generates it.

## Contents

| File | What it is |
|---|---|
| `index.html` | The whole page. Self-contained: fonts inlined as base64, no external scripts, stylesheets or images, nothing fetched at runtime. |
| `robots.txt` | Allows indexing, so developers can find the page by searching. |

Do not hand-edit `index.html`. It is generated.

## How it gets updated

The page is built by an internal tracker that snapshots the published API surface, diffs it against
the previous state, and classifies every change. Each entry's wording is written by a person, so
publishing is deliberate rather than automatic.

To ship an update, from the tooling repo:

```bash
python3 tools/api-tracker/apitrack.py build --out ~/Documents/wxrks-api-changes
cd ~/Documents/wxrks-api-changes
git add -A
git commit -m "API changes: <what moved>"
git push
```

The host redeploys on push.

## Contact

Giovanna Carla, Head of Customer Success — giovanna@wxrks.com
