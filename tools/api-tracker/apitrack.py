#!/usr/bin/env python3
"""apitrack — detect and communicate wxrks API changes.

The public API docs at https://dev.wxrks.com/ are a published Postman collection.
Postman exposes the whole collection as JSON, so the API surface can be snapshotted
and diffed instead of eyeballed.

    snapshot   fetch the live collection, normalise it, save a dated snapshot
    diff       compare two snapshots, classify every change
    build      render the client-facing changelog page from data/api-changelog.json

Classification answers one question — does the reader have to change their code? —
and the bar for saying yes is high, because the page is read by developers deciding
whether to open their integration. See CLASSIFICATION below for where each line is
drawn and why. A human confirms the call before the page is published.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = REPO / "data" / "api-snapshots"
REPORT_DIR = SNAPSHOT_DIR
CHANGELOG = REPO / "data" / "api-changelog.json"
PAGE_OUT = Path(__file__).resolve().parent / "page.html"

# Published Postman documenter for https://dev.wxrks.com/ — read off the page's
# own prefetch link. If the docs are republished under a new collection, these
# three values change; everything else here is generic.
OWNER_ID = "8194063"
PUBLISHED_ID = "Szmh1c3J"
COLLECTION_URL = (
    f"https://dev.wxrks.com/api/collections/{OWNER_ID}/{PUBLISHED_ID}"
    "?segregateAuth=true&versionTag=latest"
)

# The docs render each request as a <section> whose id is the request's own uuid, so
# `DOCS_URL#<id>` deep-links straight to that endpoint. Verified against the live page:
# the anchor resolves and scrolls to the right section. The ids come from the same
# collection JSON we snapshot, which is why a link is only offered for an endpoint that
# is still in the current surface.
DOCS_URL = "https://dev.wxrks.com/"

SEVERITIES = ("breaking", "review", "additive", "docs")


# --------------------------------------------------------------------------
# normalising the collection into a comparable API surface
# --------------------------------------------------------------------------

def strip_html(raw) -> str:
    """Postman stores descriptions as HTML fragments; compare the text only."""
    if isinstance(raw, dict):
        raw = raw.get("content", "")
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(raw))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def normalise_path(url) -> str:
    """`{{host}}/api/v3/project/:uuid?x=1` -> `/api/v3/project/:uuid`."""
    if isinstance(url, dict):
        url = url.get("raw", "")
    path = str(url).split("?", 1)[0]
    path = re.sub(r"^\{\{[^}]+\}\}", "", path)
    path = re.sub(r"^https?://[^/]+", "", path)
    return "/" + path.strip("/")


def sanitise_json(raw: str) -> str:
    """Postman bodies carry {{vars}}, // comments and trailing commas."""
    raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    return raw


def field_paths(value, prefix: str = "") -> list[str]:
    """Flatten a parsed JSON example into dotted field paths.

    Lists collapse to a single `[]` segment: clients care that the array exists
    and what its members look like, not how many the example happened to hold.
    """
    out: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            out.extend(field_paths(value[key], f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        base = f"{prefix}[]" if prefix else "[]"
        out.append(base)
        if value:
            out.extend(field_paths(value[0], base))
    elif prefix:
        out.append(prefix)
    return out


def body_shape(body: dict | None) -> dict:
    """Describe a request body as {mode, fields, approximate}."""
    if not body:
        return {"mode": None, "fields": [], "approximate": False}
    mode = body.get("mode")
    if mode == "formdata":
        keys = sorted(f.get("key", "") for f in body.get("formdata") or [])
        return {"mode": mode, "fields": keys, "approximate": False}
    if mode == "raw":
        raw = body.get("raw") or ""
        try:
            return {
                "mode": mode,
                "fields": sorted(set(field_paths(json.loads(sanitise_json(raw))))),
                "approximate": False,
            }
        except (json.JSONDecodeError, TypeError):
            # Unparseable example — fall back to top-level key names and say so,
            # so a diff on this endpoint is never reported as authoritative.
            keys = sorted(set(re.findall(r'"([A-Za-z0-9_.\-]+)"\s*:', raw)))
            return {"mode": mode, "fields": keys, "approximate": True}
    return {"mode": mode, "fields": [], "approximate": False}


def query_params(url_object: dict) -> dict:
    """Query params keyed by name.

    Postman has no required flag, so optionality is read from the two signals it
    does carry: a disabled param is not part of the canonical call, and wxrks'
    own descriptions start with "Optional".
    """
    params = {}
    for q in url_object.get("query") or []:
        key = q.get("key")
        if not key:
            continue
        desc = strip_html(q.get("description"))
        params[key] = {
            "optional": bool(q.get("disabled")) or desc.lower().startswith("optional"),
            "desc": digest(desc),
        }
    return params


def normalise(collection: dict) -> dict:
    records: list[tuple[str, dict]] = []

    def walk(items, folders=()):
        for item in items:
            if "item" in item:
                walk(item["item"], folders + (item.get("name", ""),))
                continue
            req = item.get("request") or {}
            method = (req.get("method") or "GET").upper()
            path = normalise_path(req.get("url"))
            route = f"{method} {path}"
            url_object = req.get("urlObject") or {}
            responses = {}
            for resp in item.get("response") or []:
                code = str(resp.get("code") or resp.get("name") or "?")
                fields: list[str] = []
                raw = resp.get("body")
                if raw:
                    try:
                        fields = sorted(set(field_paths(json.loads(sanitise_json(raw)))))
                    except (json.JSONDecodeError, TypeError):
                        fields = []
                # Several examples can share a status code; union their fields.
                merged = set(responses.get(code, {}).get("fields", [])) | set(fields)
                responses[code] = {"fields": sorted(merged)}
            record = {
                "name": item.get("name", ""),
                "folder": " / ".join(f for f in folders if f),
                "method": method,
                "path": path,
                "route": route,
                "auth": (req.get("auth") or {}).get("type"),
                "headers": sorted(
                    h.get("key", "") for h in req.get("header") or [] if not h.get("disabled")
                ),
                "query": query_params(url_object),
                "path_vars": sorted(
                    v.get("key") or v.get("value") or "" for v in url_object.get("variable") or []
                ),
                "body": body_shape(req.get("body")),
                "responses": responses,
                "desc": digest(strip_html(req.get("description"))),
                # Carried out to `doc_ids` below, never left in the record: the
                # "did anything move" check compares whole endpoint dicts, and a
                # field the diff does not inspect would make every old snapshot
                # look changed.
                "_doc_id": item.get("id") or item.get("_postman_id") or "",
            }
            records.append((route, record))

    walk(collection.get("item") or [])

    # The collection documents 9 routes twice (different payloads, same
    # method+path). Keying on the route alone would silently drop one of each
    # pair, so a duplicated route carries its request name in the key. Keys stay
    # order-independent: the suffix depends only on which routes are duplicated.
    seen: dict[str, int] = {}
    for route, _ in records:
        seen[route] = seen.get(route, 0) + 1
    endpoints: dict[str, dict] = {}
    doc_ids: dict[str, str] = {}
    for route, record in records:
        key = route if seen[route] == 1 else f"{route} [{record['name']}]"
        while key in endpoints:  # same route and same name: keep both
            key += "*"
        doc_id = record.pop("_doc_id", "")
        if doc_id:
            doc_ids[key] = doc_id
        endpoints[key] = record

    info = collection.get("info") or {}
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": COLLECTION_URL,
        "collection": {
            "name": info.get("name"),
            "collection_id": info.get("collectionId"),
            "publish_date": info.get("publishDate"),
            "description": digest(strip_html(info.get("description"))),
        },
        "endpoint_count": len(endpoints),
        "duplicate_routes": sorted(r for r, n in seen.items() if n > 1),
        "endpoints": endpoints,
        # Kept beside `endpoints`, not inside it, so the change detection in cmd_auto
        # keeps comparing like with like against snapshots recorded before this existed.
        "doc_ids": doc_ids,
    }


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def cmd_snapshot(args) -> int:
    if args.from_file:
        collection = json.loads(Path(args.from_file).read_text())
    else:
        req = urllib.request.Request(
            COLLECTION_URL, headers={"User-Agent": "wxrks-apitrack/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as fh:
            collection = json.loads(fh.read().decode("utf-8"))

    snap = normalise(collection)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = args.label or date.today().isoformat()
    out = SNAPSHOT_DIR / f"{stamp}.json"
    if out.exists() and not args.force:
        print(f"{out.name} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    out.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")

    print(f"saved {out.relative_to(REPO)}  ({snap['endpoint_count']} endpoints)")
    if snap["duplicate_routes"]:
        print(f"note: {len(snap['duplicate_routes'])} routes are documented more than once; "
              "their keys carry the request name")

    others = sorted(p for p in SNAPSHOT_DIR.glob("*.json") if p != out)
    if others:
        print(f"previous snapshot: {others[-1].name} — "
              f"run `diff {others[-1].stem} {stamp}` to see what moved")
    return 0


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

CLASSIFICATION = """Where the severity lines are drawn.

Everything here is inferred from a published Postman collection: request examples,
response examples, and Postman's own `optional` flag on parameters. Those are
hand-maintained documentation artefacts, not the API's contract. An example body
edited by whoever wrote the docs is evidence that something *may* have changed, not
proof that a call which worked yesterday fails today.

So `breaking` — the page calls it "Action needed" — is reserved for changes where the
diff itself establishes that a previously-correct call is now wrong:

    the endpoint is gone from the documentation
    the authentication type changed
    a path variable was added or removed, so the URL is different
    the request body switched between two concrete formats (raw <-> formdata)

`review` ("Worth checking") is for changes that plausibly require action but where
the evidence cannot settle it — anything resting on an example body or on the
`optional` flag. The reader is told what moved and decides for themselves.

`additive` is for what can only add: a new endpoint, a new optional parameter, an
extra field in a response. `docs` is for how the documentation presents itself:
wording, section, name, which examples it shows.

The failure mode this guards against is a false "Action needed". A developer who
opens their integration three times for nothing stops trusting the page, and then a
real breaking change goes unread. Under-calling a change costs one reader one
comparison; over-calling it costs the page its credibility.
"""


def _change(severity: str, kind: str, endpoint: str, detail: str, label: str = "") -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "endpoint": endpoint,
        "label": label,
        "detail": detail,
    }


def named(paths) -> set:
    """Drop the bare root-array marker from a set of field paths.

    `field_paths` records a top-level JSON array as the path `[]`. That is meaningful
    inside a snapshot but useless as customer copy — "new field: []" says nothing — and
    it is redundant anyway: a response that changes shape also changes every prefixed
    path beneath it. Filtered here rather than in `field_paths` so that snapshots
    recorded before this existed still compare byte for byte.
    """
    return {p for p in paths if p != "[]"}


def diff_endpoint(key: str, old: dict, new: dict) -> list[dict]:
    changes: list[dict] = []
    label = new.get("name") or old.get("name") or ""

    if old.get("auth") != new.get("auth"):
        changes.append(_change(
            "breaking", "auth", key, label=label,
            detail=f"auth changed: {old.get('auth')} -> {new.get('auth')}"))

    removed_hdr = sorted(set(old["headers"]) - set(new["headers"]))
    added_hdr = sorted(set(new["headers"]) - set(old["headers"]))
    if removed_hdr:
        changes.append(_change("review", "header", key, label=label,
                               detail=f"header no longer documented: {', '.join(removed_hdr)}"))
    if added_hdr:
        changes.append(_change("review", "header", key, label=label,
                               detail=f"new header expected: {', '.join(added_hdr)}"))

    # Path variables are positional — any change rewrites the URL clients call.
    removed_var = sorted(set(old["path_vars"]) - set(new["path_vars"]))
    added_var = sorted(set(new["path_vars"]) - set(old["path_vars"]))
    for group, verb in ((removed_var, "removed"), (added_var, "added")):
        if group:
            changes.append(_change("breaking", "path_var", key, label=label,
                                   detail=f"path variable {verb}: {', '.join(group)}"))

    # Query parameters come from the URL example and Postman's `optional` flag, which
    # is set by hand and often not set at all. None of it establishes a break.
    old_q, new_q = old["query"], new["query"]
    for name in sorted(set(old_q) - set(new_q)):
        changes.append(_change("review", "query", key, label=label,
                               detail=f"query param removed: {name}"))
    for name in sorted(set(new_q) - set(old_q)):
        if new_q[name]["optional"]:
            changes.append(_change("additive", "query", key, label=label,
                                   detail=f"new optional query param: {name}"))
        else:
            changes.append(_change("review", "query", key, label=label,
                                   detail=f"new query param not marked optional: {name} "
                                          "— confirm whether it is required"))
    for name in sorted(set(old_q) & set(new_q)):
        if old_q[name]["optional"] and not new_q[name]["optional"]:
            changes.append(_change("review", "query", key, label=label,
                                   detail=f"query param {name} is no longer optional"))
        elif not old_q[name]["optional"] and new_q[name]["optional"]:
            changes.append(_change("additive", "query", key, label=label,
                                   detail=f"query param {name} is now optional"))

    ob, nb = old["body"], new["body"]
    approx = ob.get("approximate") or nb.get("approximate")
    suffix = " (example body could not be parsed — verify by hand)" if approx else ""
    if ob["mode"] != nb["mode"]:
        # A mode of None means no request body was documented at all. Gaining or losing
        # the example is a documentation event; switching between two real formats is
        # the one body change that does break a working call.
        concrete = ob["mode"] and nb["mode"]
        changes.append(_change("breaking" if concrete else "review", "body", key, label=label,
                               detail=f"request body format changed: {ob['mode']} -> {nb['mode']}"))
    else:
        gone = sorted(named(ob["fields"]) - named(nb["fields"]))
        new_fields = sorted(named(nb["fields"]) - named(ob["fields"]))
        if gone:
            # Fields read off the example body, so a docs edit looks identical to a
            # real removal. The reader compares against their own payload.
            changes.append(_change("review", "body", key, label=label,
                                   detail=f"request field removed: {', '.join(gone)}{suffix}"))
        if new_fields:
            changes.append(_change("review", "body", key, label=label,
                                   detail=f"new request field: {', '.join(new_fields)}{suffix} "
                                          "— confirm whether it is required"))

    old_r, new_r = old["responses"], new["responses"]
    for code in sorted(set(old_r) - set(new_r)):
        # Which examples the docs choose to show is presentation, not behaviour.
        changes.append(_change("docs", "response", key, label=label,
                               detail=f"documented response {code} no longer shown"))
    for code in sorted(set(new_r) - set(old_r)):
        changes.append(_change("additive", "response", key, label=label,
                               detail=f"new documented response: {code}"))
    for code in sorted(set(old_r) & set(new_r)):
        gone = sorted(named(old_r[code]["fields"]) - named(new_r[code]["fields"]))
        added = sorted(named(new_r[code]["fields"]) - named(old_r[code]["fields"]))
        if gone:
            # Worth checking rather than action needed: a field vanishing from a
            # response example matters only to a reader who actually reads that field.
            changes.append(_change("review", "response", key, label=label,
                                   detail=f"{code} response field removed: {', '.join(gone)}"))
        if added:
            changes.append(_change("additive", "response", key, label=label,
                                   detail=f"{code} response field added: {', '.join(added)}"))

    if old["desc"] != new["desc"]:
        changes.append(_change("docs", "description", key, label=label,
                               detail="description text changed"))
    if old["folder"] != new["folder"]:
        changes.append(_change("docs", "folder", key, label=label,
                               detail=f"moved section: {old['folder']} -> {new['folder']}"))
    if old["name"] != new["name"]:
        changes.append(_change("docs", "rename", key, label=label,
                               detail=f"renamed: {old['name']} -> {new['name']}"))
    return changes


def diff_snapshots(old: dict, new: dict) -> dict:
    oe, ne = old["endpoints"], new["endpoints"]
    changes: list[dict] = []

    # A duplicated route's key carries its request name, so renaming one entry of
    # a pair re-keys it. Check the route itself before calling anything removed —
    # the route is still there, so nothing the reader calls has moved. That makes it
    # a documentation reshuffle, not a change to act on.
    old_routes = {r["route"] for r in oe.values()}
    new_routes = {r["route"] for r in ne.values()}

    for key in sorted(set(oe) - set(ne)):
        if oe[key]["route"] in new_routes:
            changes.append(_change(
                "docs", "entry_rekeyed", key, label=oe[key]["name"],
                detail=f"this entry is gone but {oe[key]['route']} is still documented "
                       "— likely renamed or merged, confirm the route still behaves the same"))
        else:
            changes.append(_change("breaking", "endpoint_removed", key,
                                   label=oe[key]["name"],
                                   detail="endpoint no longer in the documentation"))
    for key in sorted(set(ne) - set(oe)):
        if ne[key]["route"] in old_routes:
            # The route was already documented; the docs now describe it twice, or
            # under a new name. Nothing the reader calls has changed.
            changes.append(_change(
                "docs", "entry_rekeyed", key, label=ne[key]["name"],
                detail=f"new documentation entry for the existing route {ne[key]['route']}"))
        else:
            changes.append(_change("additive", "endpoint_added", key,
                                   label=ne[key]["name"],
                                   detail=f"new endpoint in {ne[key]['folder'] or 'the API'}"))
    for key in sorted(set(oe) & set(ne)):
        changes.extend(diff_endpoint(key, oe[key], ne[key]))

    order = {s: i for i, s in enumerate(SEVERITIES)}
    changes.sort(key=lambda c: (order[c["severity"]], c["endpoint"], c["kind"]))
    counts = {s: sum(1 for c in changes if c["severity"] == s) for s in SEVERITIES}
    return {
        "from": {"captured_at": old["captured_at"], "endpoints": old["endpoint_count"]},
        "to": {"captured_at": new["captured_at"], "endpoints": new["endpoint_count"]},
        "counts": counts,
        "changes": changes,
    }


def show_path(path: Path) -> str:
    """Repo-relative when it really is inside the repo, absolute otherwise.

    The scheduled agent runs a copy of this file from ~/Library/Application Support,
    where REPO resolves to ~/Library. Without the marker check, paths there would be
    printed as if they were repo-relative.
    """
    if (REPO / "CLAUDE.md").exists():
        try:
            return str(path.relative_to(REPO))
        except ValueError:
            pass
    return str(path)


def latest_snapshot(directory: Path | None = None) -> Path | None:
    snaps = sorted((directory or SNAPSHOT_DIR).glob("*.json"))
    return snaps[-1] if snaps else None


def fetch_surface() -> dict:
    req = urllib.request.Request(COLLECTION_URL, headers={"User-Agent": "wxrks-apitrack/1.0"})
    with urllib.request.urlopen(req, timeout=60) as fh:
        return normalise(json.loads(fh.read().decode("utf-8")))


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification. Never let this fail the check."""
    if sys.platform != "darwin":
        return
    script = (
        f'display notification {json.dumps(message)} '
        f'with title {json.dumps(title)}'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def stamp_last_checked(day: date) -> None:
    """Keep the changelog's freshness claim true without a rebuild.

    Best effort: a launchd agent has no TCC access to ~/Documents, so this fails
    with EPERM in scheduled runs. The detection still counts — only the cosmetic
    date stamp is skipped, and the next interactive run writes it.
    """
    try:
        if not CHANGELOG.exists():
            return
        data = json.loads(CHANGELOG.read_text())
        human = f"{day.day} {day:%B %Y}"
        if data.get("last_checked") == human:
            return
        data["last_checked"] = human
        CHANGELOG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        pass


def cmd_check(args) -> int:
    """Fetch, compare against the last stored surface, report only real movement.

    Exit 0 when nothing moved, 10 when it did, so a scheduled wrapper can branch.
    A snapshot is stored only when the surface actually changed — daily identical
    snapshots would cost ~72 MB a year and tell you nothing.
    """
    today = date.today()
    # A launchd agent cannot reach ~/Documents (TCC), so the scheduled run keeps its
    # detector state in an unprotected directory. Interactive runs default to the repo.
    state = Path(args.state_dir).expanduser() if args.state_dir else SNAPSHOT_DIR
    new = fetch_surface()
    previous = latest_snapshot(state)

    if previous is None:
        state.mkdir(parents=True, exist_ok=True)
        out = state / f"{today.isoformat()}.json"
        out.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")
        stamp_last_checked(today)
        print(f"no previous snapshot; stored baseline {out.name} "
              f"({new['endpoint_count']} endpoints)")
        return 0

    old = json.loads(previous.read_text())
    if old["endpoints"] == new["endpoints"]:
        stamp_last_checked(today)
        print(f"{today.isoformat()}: no change ({new['endpoint_count']} endpoints, "
              f"last moved {previous.stem})")
        return 0

    result = diff_snapshots(old, new)
    # Exactly one snapshot per date, overwritten if the docs move again the same day.
    # A suffixed second file (`-b`) sorted *before* the plain one, so latest_snapshot()
    # went on returning the earlier surface and every later run re-reported the same
    # changes. One file per day removes that by construction.
    out = state / f"{today.isoformat()}.json"
    out.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")

    counts = result["counts"]
    report = state / f"CHANGES-{today.isoformat()}.md"
    lines = [
        f"# wxrks API changed on {today.isoformat()}",
        "",
        f"Compared `{previous.stem}` against today's fetch. "
        f"{old['endpoint_count']} to {new['endpoint_count']} endpoints.",
        "",
        "| Severity | Count |",
        "|---|---|",
        *(f"| {s} | {counts[s]} |" for s in SEVERITIES),
        "",
        "Confirm every `review` item with engineering before it reaches customers, then write",
        "the confirmed changes into `data/api-changelog.json` and rebuild.",
        "",
    ]
    current = None
    for ch in result["changes"]:
        if ch["severity"] != current:
            current = ch["severity"]
            lines.append(f"## {current}")
            lines.append("")
        name = f" ({ch['label']})" if ch["label"] else ""
        lines.append(f"- `{ch['endpoint']}`{name}  \n  {ch['detail']}")
    lines.append("")
    report.write_text("\n".join(lines))
    stamp_last_checked(today)

    headline = " · ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
    print(f"{today.isoformat()}: API surface moved — {headline}")
    print(f"stored {show_path(out)}")
    print(f"report {show_path(report)}")
    if args.notify:
        notify("wxrks API changed", f"{headline}. See {report.name}")
    return 10


def load_snapshot(name: str) -> dict:
    path = Path(name)
    if not path.exists():
        path = SNAPSHOT_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no snapshot named {name}")
    return json.loads(path.read_text())


def cmd_diff(args) -> int:
    snaps = sorted(SNAPSHOT_DIR.glob("*.json"))
    if args.old and args.new:
        old, new = load_snapshot(args.old), load_snapshot(args.new)
    elif len(snaps) >= 2:
        old, new = json.loads(snaps[-2].read_text()), json.loads(snaps[-1].read_text())
        print(f"# comparing {snaps[-2].stem} -> {snaps[-1].stem}\n")
    else:
        raise SystemExit("need two snapshots; run `snapshot` at least twice")

    result = diff_snapshots(old, new)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    c = result["counts"]
    print(f"{old['endpoint_count']} -> {new['endpoint_count']} endpoints")
    print(" · ".join(f"{c[s]} {s}" for s in SEVERITIES))
    if not result["changes"]:
        print("\nno change in the documented API surface.")
        return 0
    current = None
    for ch in result["changes"]:
        if ch["severity"] != current:
            current = ch["severity"]
            print(f"\n{current.upper()}")
        name = f" ({ch['label']})" if ch["label"] else ""
        print(f"  {ch['endpoint']}{name}\n      {ch['detail']}")
    return 0


# --------------------------------------------------------------------------
# build the client-facing page
# --------------------------------------------------------------------------

def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


IMPACT_COPY = {
    "breaking": ("Action needed", "Your integration needs an update for this."),
    "review": ("Worth checking", "Confirm whether this applies to your integration."),
    "additive": ("New capability", "Nothing to change: pick this up when you're ready."),
    "docs": ("Documentation", "Wording and structure only. The API itself is unchanged."),
}


def endpoint_parts(endpoint: str) -> tuple[str, str]:
    """Split "POST /api/v3/thing" into verb and path so paths line up in a column.

    A route documented twice carries its request name in the key (`... [Create Rate]`).
    That name is shown in its own right beside the path, so the suffix is dropped here
    rather than printed twice.
    """
    key = re.sub(r"\s*\[[^\]]*\]\**$", "", endpoint)
    verb, _, path = key.partition(" ")
    return (verb, path) if path else ("", key)


def render_changes(changes: list[dict], more: int = 0) -> list[str]:
    """One block per endpoint: the route stated once, then its facts one per line.

    The alternative — every fact joined into a paragraph, with the routes repeated
    underneath as chips — makes the reader parse a run-on sentence to find out
    whether their own endpoint is in it. Structure does that work instead.
    """
    parts = ['  <ul class="changes">']
    for block in changes:
        verb, path = endpoint_parts(block.get("endpoint", ""))
        docs = block.get("docs_url")
        parts.append("    <li>")
        parts.append('      <p class="route">')
        # The verb and path together are the link, so the click target is the whole
        # route rather than a separate "docs" link the reader has to hunt for.
        if docs:
            parts.append(f'        <a href="{esc(docs)}" target="_blank" rel="noopener">'
                         f'<span class="verb">{esc(verb)}</span>'
                         f'<code>{esc(path)}</code></a>')
        else:
            parts.append(f'        <span class="verb">{esc(verb)}</span>'
                         f'<code>{esc(path)}</code>')
        if block.get("label"):
            parts.append(f'        <span class="route-name">{esc(block["label"])}</span>')
        parts.append("      </p>")
        parts.append('      <ul class="facts">')
        for item in block.get("items") or []:
            parts.append(f"        <li>{esc(item)}</li>")
        parts.append("      </ul>")
        parts.append("    </li>")
    parts.append("  </ul>")
    if more:
        parts.append(f'  <p class="more">And {more} more '
                     f'endpoint{"" if more == 1 else "s"} in this group.</p>')
    return parts


def render_entry(entry: dict) -> str:
    impact = entry.get("impact", "additive")
    heading, default_note = IMPACT_COPY.get(impact, IMPACT_COPY["additive"])
    # The date lives on the day heading that wraps the card, not on the card itself.
    parts = [
        f'<article class="card entry" data-impact="{esc(impact)}">',
        '  <header class="entry-head">',
        f'    <span class="tag tag-{esc(impact)}">{esc(heading)}</span>',
        "  </header>",
        f'  <h4>{esc(entry["title"])}</h4>',
    ]
    if entry.get("summary"):
        parts.append(f'  <p class="summary">{esc(entry["summary"])}</p>')

    if entry.get("changes"):
        parts += render_changes(entry["changes"], entry.get("more", 0))

    for ep in entry.get("endpoints") or []:
        parts.append(f'  <p class="endpoint"><code>{esc(ep)}</code></p>')

    if entry.get("action"):
        parts.append('  <div class="action">')
        parts.append('    <span class="action-label">What to do</span>')
        parts.append(f'    <p>{esc(entry["action"])}</p>')
        parts.append("  </div>")
    elif entry.get("auto"):
        # Never present a machine-written entry as reviewed guidance.
        parts.append(f'  <p class="detected">{esc(AUTO_NOTE)}</p>')
    elif impact in ("additive", "docs"):
        parts.append(f'  <p class="no-action">{esc(default_note)}</p>')

    if entry.get("effective"):
        parts.append(f'  <p class="effective">In effect from '
                     f'<strong>{esc(entry["effective"])}</strong>.</p>')
    if entry.get("docs_url"):
        parts.append(f'  <p class="docs-link"><a href="{esc(entry["docs_url"])}" '
                     f'target="_blank" rel="noopener">Read the endpoint docs</a></p>')
    parts.append("</article>")
    return "\n".join(parts)


def pretty_date(iso: str) -> str:
    """2026-08-17 -> 17 August 2026, matching how `last_checked` reads."""
    try:
        day = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{day.day} {day:%B %Y}"


def render_feed(entries: list[dict]) -> str:
    """Group the cards by date under one heading per day.

    Several entries share a date whenever a check finds changes at more than one
    severity — four cards each repeating "2026-08-17" reads as four separate events
    instead of one day's worth of movement.
    """
    out: list[str] = []
    for day, group in groupby(entries, key=lambda e: e["date"]):
        out.append(f'<section class="day" data-date="{esc(day)}">')
        out.append(f'  <h3 class="day-head"><time datetime="{esc(day)}">'
                   f'{esc(pretty_date(day))}</time></h3>')
        out.extend(render_entry(e) for e in group)
        out.append("</section>")
    return "\n".join(out)


SOCIAL_DESCRIPTION = (
    "Track every update to the wxrks API and keep your integration on the latest version."
)

# The published copy, as committed. GitHub Pages serves this file verbatim, so comparing
# against it tells us whether the live site is behind the local build.
PUBLISHED_URL = (
    "https://raw.githubusercontent.com/wxrks-builders/wxrks-api-changes/main/index.html"
)


def standalone_document(page: str, data: dict) -> str:
    """Wrap the page as a complete HTML document for self-hosting.

    The Artifact host supplies its own `<head>`, so `page.html` is a fragment. A page
    served from api-changes.wxrks.com has to carry its own doctype, charset and
    viewport — without the viewport meta, mobile browsers render it at desktop width.
    """
    checked = esc(data.get("last_checked", ""))
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="description" content="{esc(SOCIAL_DESCRIPTION)}">\n'
        '<meta property="og:title" content="wxrks API changes">\n'
        f'<meta property="og:description" content="{esc(SOCIAL_DESCRIPTION)}">\n'
        '<meta property="og:type" content="website">\n'
        '<meta name="robots" content="index, follow">\n'
        f'<!-- Generated by tools/api-tracker/apitrack.py. Last checked {checked}. -->\n'
        "</head>\n<body>\n" + page + "\n</body>\n</html>\n"
    )


def build_page(data: dict) -> str:
    """Render the page fragment from the change log. Pure: no files written."""
    entries = sorted(data.get("entries") or [], key=lambda e: e["date"], reverse=True)
    here = Path(__file__).resolve().parent
    template = (here / "template.html").read_text()

    # Inter is inlined as base64 rather than linked: the Artifact CSP blocks font
    # CDNs, and a silent fallback would lose the typography this page matches.
    fonts = (here / "inter-fonts.css").read_text()

    counts = {s: sum(1 for e in entries if e.get("impact") == s) for s in SEVERITIES}
    body = render_feed(entries) or '<p class="empty">No API changes recorded yet.</p>'
    return (
        template
        .replace("/*FONTS*/", fonts)
        .replace("<!--ENTRIES-->", body)
        .replace("<!--CHECKED-->", esc(data.get("last_checked", "—")))
        .replace("<!--ENDPOINTS-->", esc(data.get("endpoint_count", "—")))
        .replace("<!--BREAKING-->", str(counts["breaking"]))
        .replace("<!--TOTAL-->", str(len(entries)))
        .replace("<!--CONTACT-->", esc(data.get("contact", "")))
    )


def cmd_build(args) -> int:
    data = json.loads(CHANGELOG.read_text())
    entries = sorted(data.get("entries") or [], key=lambda e: e["date"], reverse=True)
    counts = {s: sum(1 for e in entries if e.get("impact") == s) for s in SEVERITIES}
    page = build_page(data)
    PAGE_OUT.write_text(page)

    # A detected change that never made it into the change log would publish a page
    # claiming a fresh check date while silently omitting the change. Warn loudly.
    newest_snapshot = latest_snapshot()
    if newest_snapshot and entries:
        newest_entry = max(e["date"] for e in entries)
        if newest_snapshot.stem[:10] > newest_entry:
            print(f"WARNING: the API moved on {newest_snapshot.stem[:10]} but the newest "
                  f"change log entry is {newest_entry}.")
            print("         Publishing now would show a fresh check date and omit that "
                  "change. Write it up first.")
    print(f"built {PAGE_OUT.relative_to(REPO)}  ({len(entries)} entries, "
          f"{counts['breaking']} action-needed)")

    if args.out:
        # A deploy folder holds nothing but the page. This repo carries account
        # files, pricing and portfolio data — never serve it as a web root.
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(standalone_document(page, data))
        (out_dir / "robots.txt").write_text("User-agent: *\nAllow: /\n")
        print(f"wrote {out_dir}/index.html — deploy this folder, not the repo")
    else:
        print("publish it with the Artifact tool to refresh the live page (same URL).")
    return 0


# Short by design. Repeating the full explanation on every card crowded the facts out;
# it now sits once in the "Where this comes from" panel. What must never be lost is the
# marker itself — an auto entry has to be visibly distinct from reviewed guidance.
AUTO_NOTE = "Detected automatically"

# What the diff can honestly say per severity, with no human judgement added.
AUTO_TITLES = {
    "breaking": "Changes to endpoints you may already be calling",
    "review": "Changes worth checking against your integration",
    "additive": "New endpoints and fields",
    "docs": "Documentation updates",
}


def _plural(items: str, one: str, many: str) -> str:
    """Diff details carry comma-joined names; a comma means more than one."""
    return many if "," in items else one


# Diff details are phrased for whoever triages them — terse, lowercase, arrow-notated.
# On the page each has to read as a plain statement of fact. One rule per detail shape,
# and nothing is added that the diff did not establish. An unrecognised shape falls
# through to the fallback in `human_detail` and is published as written rather than
# dropped or guessed at.
HUMAN_RULES: list[tuple[str, object]] = [
    (r"auth changed: (.+) -> (.+)", r"Authentication changed from \1 to \2"),
    (r"header no longer documented: (.+)",
     lambda m: f"{_plural(m[1], 'Header', 'Headers')} no longer documented: {m[1]}"),
    (r"new header expected: (.+)",
     lambda m: f"{_plural(m[1], 'New header', 'New headers')} expected: {m[1]}"),
    (r"path variable removed: (.+)",
     lambda m: f"{_plural(m[1], 'Path variable', 'Path variables')} removed: {m[1]}"),
    (r"path variable added: (.+)",
     lambda m: f"{_plural(m[1], 'New path variable', 'New path variables')}: {m[1]}"),
    (r"query param removed: (.+)", r"Query parameter removed: \1"),
    (r"new optional query param: (.+)", r"New optional query parameter: \1"),
    (r"new query param not marked optional: (.+)",
     r"New query parameter, not marked optional: \1"),
    (r"query param (.+) is no longer optional",
     r"Query parameter \1 is no longer marked optional"),
    (r"query param (.+) is now optional", r"Query parameter \1 is now optional"),
    # `mode` is None when no request body is documented at all. "changed from None to
    # raw" is Python leaking into customer copy; say what it means instead.
    (r"request body format changed: None -> (.+)",
     r"A request body is now documented (\1); there was none before"),
    (r"request body format changed: (.+) -> None",
     r"The documented request body (\1) is gone"),
    (r"request body format changed: (.+) -> (.+)",
     r"Request body format changed from \1 to \2"),
    (r"request field removed: (.+)",
     lambda m: f"{_plural(m[1], 'Request field removed', 'Request fields removed')}: {m[1]}"),
    (r"new request field: (.+)",
     lambda m: f"{_plural(m[1], 'New request field', 'New request fields')}: {m[1]}"),
    (r"documented response (.+) no longer shown", r"Response \1 is no longer documented"),
    (r"new documented response: (.+)", r"New documented response: \1"),
    # No em dash in generated text: `human_detail` treats " — " as the start of the
    # internal half and cuts there, so its own output has to stay clear of it.
    (r"(.+) response field removed: (.+)",
     lambda m: f"{_plural(m[2], 'Field', 'Fields')} removed from the "
               f"{m[1]} response: {m[2]}"),
    (r"(.+) response field added: (.+)",
     lambda m: f"{_plural(m[2], 'New field', 'New fields')} in the "
               f"{m[1]} response: {m[2]}"),
    (r"description text changed", "Description text updated"),
    (r"moved section: (.*) -> (.+)",
     lambda m: f"Moved to the {m[2]} section"
               + (f" (was {m[1].strip()})" if m[1].strip() else "")),
    (r"renamed: (.+) -> (.+)", "Renamed to “\\2” (was “\\1”)"),
    (r"new endpoint in the API", "New endpoint"),
    (r"new endpoint in (.+)", r"New endpoint in the \1 section"),
    (r"endpoint no longer in the documentation", "No longer in the documentation"),
    (r"new documentation entry for the existing route (.+)",
     r"Second documentation entry for the existing route \1"),
]


def human_detail(detail: str) -> str:
    """Rewrite one diff detail as a sentence a customer can read on the page.

    Details also carry notes written for whoever triages them ("confirm whether it is
    required", "verify by hand"). Those must not reach a public page: they read as
    instructions to the reader and expose our uncertainty as if it were theirs.
    Everything after an em dash is that internal half, and it is cut first.
    """
    text = detail.replace(" (example body could not be parsed — verify by hand)", "")
    text = text.split(" — ", 1)[0].strip()
    for pattern, repl in HUMAN_RULES:
        match = re.fullmatch(pattern, text)
        if match:
            return match.expand(repl) if isinstance(repl, str) else repl(match)
    return text[:1].upper() + text[1:]


def auto_entries(result: dict, day: date, doc_ids: dict | None = None) -> list[dict]:
    """Turn a diff into change log entries stating only what the diff found.

    One entry per severity present. Inside it, the facts are grouped by endpoint so
    the page can state each route once and list what happened to it — see
    `render_changes`. No invented guidance: `action` stays empty and the entry is
    flagged `auto` so the page says where it came from and Giovanna can replace it
    with curated prose later.
    """
    by_severity: dict[str, list[dict]] = {}
    for change in result["changes"]:
        by_severity.setdefault(change["severity"], []).append(change)

    # High on purpose. A reader is here to find out whether *their* endpoint moved, so
    # hiding one behind "and N more" defeats the page. This is an overflow guard against
    # a day when the whole collection is republished, not a display preference.
    limit = 60
    entries = []
    for severity in SEVERITIES:
        group = by_severity.get(severity)
        if not group:
            continue

        by_endpoint: dict[str, dict] = {}
        for change in group:
            if change["endpoint"] not in by_endpoint:
                block = {
                    "endpoint": change["endpoint"],
                    "label": change.get("label") or "",
                    "items": [],
                }
                # No id means the endpoint is not in the current surface — it was
                # removed. Linking to a section that no longer renders is worse
                # than not linking.
                doc_id = (doc_ids or {}).get(change["endpoint"])
                if doc_id:
                    block["docs_url"] = f"{DOCS_URL}#{doc_id}"
                by_endpoint[change["endpoint"]] = block
            block = by_endpoint[change["endpoint"]]
            fact = human_detail(change["detail"])
            if fact not in block["items"]:
                block["items"].append(fact)

        blocks = [by_endpoint[key] for key in sorted(by_endpoint)]
        entry = {
            "date": day.isoformat(),
            "impact": severity,
            "auto": True,
            "title": AUTO_TITLES[severity],
            # Scale only. What the severity *means* is the tag and the legend's job;
            # a machine-written entry must not read as advice about this change.
            "summary": ("1 endpoint changed." if len(blocks) == 1
                        else f"{len(blocks)} endpoints changed."),
            "changes": blocks[:limit],
        }
        if len(blocks) > limit:
            entry["more"] = len(blocks) - limit
        entries.append(entry)
    return entries


def cmd_auto(args) -> int:
    """Detect, write entries, rebuild. Everything a scheduled job needs, in one call.

    Exit 0 when nothing moved and nothing was written, 10 when the page changed and
    the caller should commit.
    """
    today = date.today()
    new = fetch_surface()
    previous = latest_snapshot()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    data = json.loads(CHANGELOG.read_text()) if CHANGELOG.exists() else {"entries": []}

    if previous is None:
        (SNAPSHOT_DIR / f"{today.isoformat()}.json").write_text(
            json.dumps(new, indent=2, sort_keys=True) + "\n")
        print(f"stored baseline snapshot ({new['endpoint_count']} endpoints); "
              "nothing to publish yet")
        return 0

    old = json.loads(previous.read_text())
    if old["endpoints"] == new["endpoints"]:
        print(f"{today.isoformat()}: no change ({new['endpoint_count']} endpoints)")
        return 0

    result = diff_snapshots(old, new)
    # One snapshot per date — see the note in cmd_check. A same-day rerun overwrites
    # it and its findings are folded into the day's existing cards below.
    out = SNAPSHOT_DIR / f"{today.isoformat()}.json"
    out.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")

    added = auto_entries(result, today, new.get("doc_ids"))

    # A second run on the same day (a manual dispatch, or a schedule change) would
    # otherwise prepend a second card with the same date and severity, and the page
    # would show one day's movement twice. Fold it into the card already there.
    kept = list(data.get("entries") or [])
    for entry in added:
        twin = next((e for e in kept if e.get("date") == entry["date"]
                     and e.get("impact") == entry["impact"] and e.get("auto")), None)
        if twin is None:
            continue
        seen = {b["endpoint"] for b in twin.get("changes") or []}
        merged = list(twin.get("changes") or [])
        for block in entry["changes"]:
            if block["endpoint"] in seen:
                target = next(b for b in merged if b["endpoint"] == block["endpoint"])
                target["items"] += [i for i in block["items"] if i not in target["items"]]
            else:
                merged.append(block)
        twin["changes"] = merged
        twin["summary"] = ("1 endpoint changed." if len(merged) == 1
                           else f"{len(merged)} endpoints changed.")
        entry["_merged"] = True

    folded = sum(1 for e in added if e.get("_merged"))
    added = [e for e in added if not e.pop("_merged", False)]
    data["entries"] = added + kept
    data["last_checked"] = f"{today.day} {today:%B %Y}"
    data["endpoint_count"] = new["endpoint_count"]
    CHANGELOG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    page = build_page(data)
    PAGE_OUT.write_text(page)
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(standalone_document(page, data))
        (out_dir / "robots.txt").write_text("User-agent: *\nAllow: /\n")

    counts = result["counts"]
    headline = " · ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
    print(f"{today.isoformat()}: API surface moved — {headline}")
    # Both numbers matter: "wrote 0 entries" on a day that moved reads like a no-op
    # when in fact the findings went into cards that were already there.
    wrote = f"wrote {len(added)} new entr{'y' if len(added) == 1 else 'ies'}"
    if folded:
        wrote += (f" and folded {folded} into today's existing "
                  f"card{'' if folded == 1 else 's'}")
    print(f"{wrote}; rebuilt the page")
    return 10


def cmd_status(args) -> int:
    """Answer one question: is the published site current with the live API docs?

    Read-only. Checks the three links in the chain independently, because only the
    first is automated and a break in any of them leaves the site quietly stale.
    """
    data = json.loads(CHANGELOG.read_text()) if CHANGELOG.exists() else {"entries": []}
    entries = sorted(data.get("entries") or [], key=lambda e: e["date"])
    stale: list[str] = []

    print("1. the API docs vs our last snapshot")
    previous = latest_snapshot()
    if previous is None:
        print("   no snapshot yet — run `check`")
        stale.append("no snapshot")
    else:
        old = json.loads(previous.read_text())
        try:
            new = fetch_surface()
        except (urllib.error.URLError, OSError) as exc:
            print(f"   could not reach the docs: {exc}")
            return 1
        if old["endpoints"] == new["endpoints"]:
            print(f"   current — no movement since {previous.stem} "
                  f"({new['endpoint_count']} endpoints)")
        else:
            counts = diff_snapshots(old, new)["counts"]
            moved = " · ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
            print(f"   MOVED since {previous.stem} — {moved}")
            print("   → run `check` to record it, then write the entries")
            stale.append("API moved, not yet snapshotted")

    print("\n2. our snapshots vs the change log")
    if previous is not None and entries:
        newest_entry = entries[-1]["date"]
        if previous.stem[:10] > newest_entry:
            print(f"   BEHIND — the API moved on {previous.stem[:10]} but the newest "
                  f"change log entry is {newest_entry}")
            print("   → write the change up in data/api-changelog.json")
            stale.append("change log missing an entry")
        else:
            print(f"   current — newest entry {newest_entry} covers snapshot {previous.stem}")
    elif not entries:
        print("   the change log has no entries")
        stale.append("empty change log")

    print("\n3. the local build vs the published site")
    try:
        page = build_page(data)
        expected = standalone_document(page, data)
        req = urllib.request.Request(PUBLISHED_URL,
                                     headers={"User-Agent": "wxrks-apitrack/1.0"})
        with urllib.request.urlopen(req, timeout=30) as fh:
            live = fh.read().decode("utf-8")
        if live == expected:
            print("   current — the published page matches what a build produces now")
        else:
            print("   STALE — the published page differs from a fresh local build")
            print("   → rebuild with --out and upload the folder again")
            stale.append("published site behind local build")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"   could not read the published page: {exc}")
        stale.append("published page unreadable")

    print()
    if stale:
        print("VERDICT: the site is not current. " + "; ".join(stale) + ".")
        return 10
    print("VERDICT: the published site is current with the API docs.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="apitrack", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="fetch and store the current API surface")
    s.add_argument("--label", help="snapshot name (default: today's date)")
    s.add_argument("--from-file", help="normalise a local collection JSON instead of fetching")
    s.add_argument("--force", action="store_true", help="overwrite an existing snapshot")
    s.set_defaults(func=cmd_snapshot)

    a = sub.add_parser("auto", help="detect, write entries and rebuild in one step (for CI)")
    a.add_argument("--out", metavar="DIR",
                   help="also write DIR/index.html (use '.' in the publishing repo)")
    a.set_defaults(func=cmd_auto)

    st = sub.add_parser("status", help="is the published site current with the API docs?")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("check", help="fetch and report only if the API surface moved")
    c.add_argument("--notify", action="store_true",
                   help="show a macOS notification when something moved")
    c.add_argument("--state-dir", metavar="DIR",
                   help="where to keep detector state (default: data/api-snapshots). "
                        "The scheduled agent uses a directory outside ~/Documents, "
                        "which launchd cannot read.")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("diff", help="compare two snapshots (defaults to the latest two)")
    d.add_argument("old", nargs="?")
    d.add_argument("new", nargs="?")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_diff)

    b = sub.add_parser("build", help="render the client-facing changelog page")
    b.add_argument("--out", metavar="DIR",
                   help="also write DIR/index.html for hosting on a real domain")
    b.set_defaults(func=cmd_build)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
