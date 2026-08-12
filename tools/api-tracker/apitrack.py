#!/usr/bin/env python3
"""apitrack — detect and communicate wxrks API changes.

The public API docs at https://dev.wxrks.com/ are a published Postman collection.
Postman exposes the whole collection as JSON, so the API surface can be snapshotted
and diffed instead of eyeballed.

    snapshot   fetch the live collection, normalise it, save a dated snapshot
    diff       compare two snapshots, classify every change
    build      render the client-facing changelog page from data/api-changelog.json

Classification is deliberately conservative: anything that could break a client
integration is `breaking`, anything ambiguous is `review`, and only pure text edits
are `docs`. A human confirms the call before the page is published.
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
    for route, record in records:
        key = route if seen[route] == 1 else f"{route} [{record['name']}]"
        while key in endpoints:  # same route and same name: keep both
            key += "*"
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

def _change(severity: str, kind: str, endpoint: str, detail: str, label: str = "") -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "endpoint": endpoint,
        "label": label,
        "detail": detail,
    }


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

    old_q, new_q = old["query"], new["query"]
    for name in sorted(set(old_q) - set(new_q)):
        changes.append(_change("breaking", "query", key, label=label,
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
            changes.append(_change("breaking", "query", key, label=label,
                                   detail=f"query param {name} is no longer optional"))
        elif not old_q[name]["optional"] and new_q[name]["optional"]:
            changes.append(_change("additive", "query", key, label=label,
                                   detail=f"query param {name} is now optional"))

    ob, nb = old["body"], new["body"]
    approx = ob.get("approximate") or nb.get("approximate")
    suffix = " (example body could not be parsed — verify by hand)" if approx else ""
    if ob["mode"] != nb["mode"]:
        changes.append(_change("breaking", "body", key, label=label,
                               detail=f"request body format changed: {ob['mode']} -> {nb['mode']}"))
    else:
        gone = sorted(set(ob["fields"]) - set(nb["fields"]))
        new_fields = sorted(set(nb["fields"]) - set(ob["fields"]))
        if gone:
            changes.append(_change("breaking", "body", key, label=label,
                                   detail=f"request field removed: {', '.join(gone)}{suffix}"))
        if new_fields:
            changes.append(_change("review", "body", key, label=label,
                                   detail=f"new request field: {', '.join(new_fields)}{suffix} "
                                          "— confirm whether it is required"))

    old_r, new_r = old["responses"], new["responses"]
    for code in sorted(set(old_r) - set(new_r)):
        changes.append(_change("review", "response", key, label=label,
                               detail=f"documented response {code} no longer shown"))
    for code in sorted(set(new_r) - set(old_r)):
        changes.append(_change("additive", "response", key, label=label,
                               detail=f"new documented response: {code}"))
    for code in sorted(set(old_r) & set(new_r)):
        gone = sorted(set(old_r[code]["fields"]) - set(new_r[code]["fields"]))
        added = sorted(set(new_r[code]["fields"]) - set(old_r[code]["fields"]))
        if gone:
            changes.append(_change("breaking", "response", key, label=label,
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
    # a live route reads as a rename to review, not as a breaking removal.
    old_routes = {r["route"] for r in oe.values()}
    new_routes = {r["route"] for r in ne.values()}

    for key in sorted(set(oe) - set(ne)):
        if oe[key]["route"] in new_routes:
            changes.append(_change(
                "review", "entry_rekeyed", key, label=oe[key]["name"],
                detail=f"this entry is gone but {oe[key]['route']} is still documented "
                       "— likely renamed or merged, confirm the route still behaves the same"))
        else:
            changes.append(_change("breaking", "endpoint_removed", key,
                                   label=oe[key]["name"],
                                   detail="endpoint no longer in the documentation"))
    for key in sorted(set(ne) - set(oe)):
        if ne[key]["route"] in old_routes:
            changes.append(_change(
                "review", "entry_rekeyed", key, label=ne[key]["name"],
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
    out = state / f"{today.isoformat()}.json"
    if out == previous:  # already moved once today; keep one snapshot per day
        out = state / f"{today.isoformat()}-b.json"
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


def render_entry(entry: dict) -> str:
    impact = entry.get("impact", "additive")
    heading, default_note = IMPACT_COPY.get(impact, IMPACT_COPY["additive"])
    parts = [
        f'<article class="card entry" data-impact="{esc(impact)}">',
        '  <header class="entry-head">',
        f'    <time datetime="{esc(entry["date"])}">{esc(entry["date"])}</time>',
        f'    <span class="tag tag-{esc(impact)}">{esc(heading)}</span>',
        "  </header>",
        f'  <h3>{esc(entry["title"])}</h3>',
    ]
    if entry.get("summary"):
        parts.append(f'  <p class="summary">{esc(entry["summary"])}</p>')

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
    body = "\n".join(render_entry(e) for e in entries) or (
        '<p class="empty">No API changes recorded yet.</p>'
    )
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


AUTO_NOTE = (
    "Detected automatically from the published API documentation. If this affects you and "
    "the detail above isn't enough, tell us and we'll add it."
)

# What the diff can honestly say per severity, with no human judgement added.
AUTO_TITLES = {
    "breaking": "Changes to endpoints you may already be calling",
    "review": "Changes worth checking against your integration",
    "additive": "New endpoints and fields",
    "docs": "Documentation updates",
}


def public_detail(detail: str) -> str:
    """Strip the internal half of a diff detail so it can face customers.

    Diff details carry notes written for whoever triages them ("confirm whether it is
    required", "verify by hand"). Those must not reach a public page: they read as
    instructions to the reader and expose our own uncertainty as if it were theirs.
    Everything after an em dash is that internal half.
    """
    detail = detail.replace(" (example body could not be parsed — verify by hand)", "")
    detail = detail.split(" — ", 1)[0]
    return detail.replace(" -> ", " to ").strip()


def auto_entries(result: dict, day: date) -> list[dict]:
    """Turn a diff into change log entries stating only what the diff found.

    One entry per severity present, listing the mechanical facts. No invented
    guidance: `action` stays empty and the entry is flagged `auto` so the page says
    where it came from and Giovanna can replace it with curated prose later.
    """
    by_severity: dict[str, list[dict]] = {}
    for change in result["changes"]:
        by_severity.setdefault(change["severity"], []).append(change)

    limit = 8
    entries = []
    for severity in SEVERITIES:
        group = by_severity.get(severity)
        if not group:
            continue
        shown = [f"{c['endpoint']}: {public_detail(c['detail'])}" for c in group[:limit]]
        summary = "; ".join(shown) + "."
        if len(group) > limit:
            summary += f" And {len(group) - limit} more."
        endpoints = sorted({c["endpoint"] for c in group})
        entries.append({
            "date": day.isoformat(),
            "impact": severity,
            "auto": True,
            "title": AUTO_TITLES[severity],
            "summary": summary,
            "endpoints": endpoints[:12],
        })
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
    out = SNAPSHOT_DIR / f"{today.isoformat()}.json"
    if out == previous:
        out = SNAPSHOT_DIR / f"{today.isoformat()}-b.json"
    out.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")

    added = auto_entries(result, today)
    data["entries"] = added + (data.get("entries") or [])
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
    print(f"wrote {len(added)} entr{'y' if len(added) == 1 else 'ies'} and rebuilt the page")
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
