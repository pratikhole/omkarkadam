import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.excelr.com"
MAX_PAGES = 5000
MAX_WORKERS = 8
TIMEOUT = 20

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"
DASHBOARD_FILE = Path("dashboard.html")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WebsiteAudit/1.0; +https://github.com/)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TRACKED_FIELDS = [
    ("title", "Title", "high"),
    ("description", "Description", "medium"),
    ("canonical", "Canonical", "high"),
    ("h1", "H1", "high"),
    ("robots", "Robots", "high"),
    ("content", "Content", "low"),
    ("images", "Images", "medium"),
    ("internal_links", "Internal Links", "medium"),
    ("schema", "Schema", "high"),
]


# ============================================================
# URL
# ============================================================

def clean_url(url):
    try:
        parsed = urlparse(url)
        base = urlparse(BASE_URL)

        if parsed.scheme not in ("http", "https"):
            return None

        if parsed.netloc.lower() != base.netloc.lower():
            return None

        path = parsed.path or "/"
        result = f"{base.scheme}://{base.netloc}{path}"

        if path != "/" and result.endswith("/"):
            result = result[:-1]

        return result

    except Exception:
        return None


# ============================================================
# FETCH
# ============================================================

def fetch(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )

        status = response.status_code
        content_type = response.headers.get("content-type", "").lower()

        if status == 200 and "text/html" in content_type:
            return {
                "status": "success",
                "html": response.text,
                "http_status": status,
            }

        if status in (404, 410):
            return {
                "status": "removed",
                "html": "",
                "http_status": status,
            }

        return {
            "status": "failed",
            "html": "",
            "http_status": status,
        }

    except requests.RequestException as error:
        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "error": str(error),
        }


# ============================================================
# EXTRACT
# ============================================================

def extract(url, html):
    soup = BeautifulSoup(html, "html.parser")
    base = urlparse(BASE_URL)

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    description = ""
    tag = soup.find(
        "meta",
        attrs={"name": re.compile(r"^description$", re.I)}
    )
    if tag:
        description = tag.get("content", "").strip()

    canonical = ""
    tag = soup.find(
        "link",
        attrs={"rel": lambda value: value and "canonical" in value}
    )
    if tag and tag.get("href"):
        canonical = urljoin(url, tag.get("href").strip())

    h1 = [
        x.get_text(" ", strip=True)
        for x in soup.find_all("h1")
    ]

    robots = ""
    tag = soup.find(
        "meta",
        attrs={"name": re.compile(r"^robots$", re.I)}
    )
    if tag:
        robots = tag.get("content", "").strip()

    # Collect useful elements BEFORE removing them.
    images = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src:
            absolute = urljoin(url, src.strip())
            if urlparse(absolute).netloc.lower() == base.netloc.lower():
                images.add(absolute)

    internal_links = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(url, href)
        normalized = clean_url(absolute)
        if normalized:
            internal_links.add(normalized)

    schema_values = []
    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)}
    ):
        value = script.get_text(" ", strip=True)
        if value:
            schema_values.append(value)

    # Visible text.
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    main = soup.find("main")
    content = (
        main.get_text(" ", strip=True)
        if main
        else soup.get_text(" ", strip=True)
    )
    content = re.sub(r"\s+", " ", content).strip()

    images = sorted(images)
    internal_links = sorted(internal_links)
    schema_values = sorted(schema_values)

    return {
        "url": url,
        "title": title,
        "description": description,
        "canonical": canonical,
        "h1": h1,
        "robots": robots,
        "content": content,
        "content_length": len(content),
        "images": images,
        "internal_links": internal_links,
        "schema": schema_values,
    }


# ============================================================
# SITEMAP
# ============================================================

def get_sitemap_urls():
    discovered = set()
    sitemap_ok = False

    candidates = [
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml",
    ]

    for sitemap_url in candidates:
        try:
            response = requests.get(
                sitemap_url,
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            if response.status_code != 200:
                continue

            sitemap_ok = True
            soup = BeautifulSoup(response.text, "xml")

            for loc in soup.find_all("loc"):
                value = loc.get_text(strip=True)

                if value.lower().endswith(".xml"):
                    try:
                        child = requests.get(
                            value,
                            headers=HEADERS,
                            timeout=TIMEOUT,
                        )
                        if child.status_code != 200:
                            continue

                        child_soup = BeautifulSoup(child.text, "xml")

                        for child_loc in child_soup.find_all("loc"):
                            page = clean_url(
                                child_loc.get_text(strip=True)
                            )
                            if page:
                                discovered.add(page)

                    except requests.RequestException:
                        continue

                else:
                    page = clean_url(value)
                    if page:
                        discovered.add(page)

            if discovered:
                break

        except requests.RequestException:
            continue

    homepage = clean_url(BASE_URL)
    if homepage:
        discovered.add(homepage)

    return discovered, sitemap_ok


# ============================================================
# CRAWL
# ============================================================

def crawl(old_pages):
    start_time = time.time()

    sitemap_urls, sitemap_ok = get_sitemap_urls()

    urls = set(sitemap_urls)
    urls.update(old_pages.keys())

    # Always keep the known baseline pages.
    urls = set(sorted(urls)[:MAX_PAGES])

    print(f"[START] {len(urls)} pages", flush=True)

    pages = {}
    failed = {}
    removed = set()

    # First pass: sitemap + baseline pages.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        jobs = {
            executor.submit(fetch, url): url
            for url in sorted(urls)
        }

        completed = 0

        for future in as_completed(jobs):
            url = jobs[future]

            try:
                result = future.result()

                if result["status"] == "success":
                    pages[url] = extract(url, result["html"])

                elif result["status"] == "removed":
                    removed.add(url)

                else:
                    failed[url] = {
                        "http_status": result.get("http_status"),
                        "error": result.get("error"),
                    }

            except Exception as error:
                failed[url] = {
                    "http_status": None,
                    "error": str(error),
                }

            completed += 1

            if completed % 25 == 0 or completed == len(jobs):
                print(
                    f"[PROGRESS] {completed}/{len(jobs)}",
                    flush=True
                )

    # Second pass: discover internal links that were not in sitemap.
    discovered = set()

    for page in pages.values():
        discovered.update(page.get("internal_links", []))

    new_candidates = sorted(
        discovered - set(pages.keys()) - set(failed.keys()) - removed
    )[: max(0, MAX_PAGES - len(pages))]

    if new_candidates:
        print(
            f"[DISCOVERY] {len(new_candidates)} additional internal-link pages",
            flush=True
        )

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            jobs = {
                executor.submit(fetch, url): url
                for url in new_candidates
            }

            for future in as_completed(jobs):
                url = jobs[future]

                try:
                    result = future.result()

                    if result["status"] == "success":
                        pages[url] = extract(url, result["html"])

                    elif result["status"] == "removed":
                        removed.add(url)

                    else:
                        failed[url] = {
                            "http_status": result.get("http_status"),
                            "error": result.get("error"),
                        }

                except Exception as error:
                    failed[url] = {
                        "http_status": None,
                        "error": str(error),
                    }

    duration = round(time.time() - start_time, 2)

    print(f"[DONE] {len(pages)} pages collected", flush=True)
    print(f"[FAILED] {len(failed)} pages", flush=True)
    print(f"[REMOVED] {len(removed)} pages", flush=True)
    print(f"[DURATION] {duration} seconds", flush=True)

    return {
        "pages": pages,
        "failed": failed,
        "removed": removed,
        "duration": duration,
        "sitemap_ok": sitemap_ok,
        "attempted": len(urls),
    }


# ============================================================
# COMPARE
# ============================================================

def compare(old, new, failed, removed):
    changes = []

    old_urls = set(old)
    new_urls = set(new)

    for url in sorted(new_urls - old_urls):
        changes.append({
            "type": "new",
            "url": url,
            "field": "Page",
            "details": "New page discovered",
            "priority": "medium",
        })

    for url in sorted((old_urls - new_urls) & removed):
        changes.append({
            "type": "removed",
            "url": url,
            "field": "Page",
            "details": "Page returned 404/410",
            "priority": "high",
        })

    for url in sorted(failed):
        changes.append({
            "type": "failed",
            "url": url,
            "field": "Crawl",
            "details": "Temporary crawl failure",
            "priority": "medium",
        })

    for url in sorted(old_urls & new_urls):
        before = old[url]
        after = new[url]

        for field, label, priority in TRACKED_FIELDS:
            # New fields did not exist in the old baseline.
            # Do not create false changes during the upgrade.
            if field not in before:
                continue

            if before.get(field) != after.get(field):
                changes.append({
                    "type": "changed",
                    "url": url,
                    "field": label,
                    "old": before.get(field, ""),
                    "new": after.get(field, ""),
                    "details": f"{label} changed",
                    "priority": priority,
                })

    return changes


# ============================================================
# HISTORY
# ============================================================

def save_history(pages, changes, duration, failed_count):
    history = []

    if HISTORY_FILE.exists():
        try:
            history = json.loads(
                HISTORY_FILE.read_text(encoding="utf-8")
            )
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

    now = datetime.now(timezone.utc)

    history.append({
        "date": now.strftime("%Y-%m-%d"),
        "checked_at": now.isoformat(),
        "pages": len(pages),
        "new": sum(c["type"] == "new" for c in changes),
        "changed": sum(c["type"] == "changed" for c in changes),
        "removed": sum(c["type"] == "removed" for c in changes),
        "failed": failed_count,
        "duration": duration,
    })

    history = history[-365:]

    HISTORY_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return history


# ============================================================
# DASHBOARD
# ============================================================

def make_dashboard(pages, changes, history, duration, failed_count):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    new_count = sum(c["type"] == "new" for c in changes)
    changed_count = sum(c["type"] == "changed" for c in changes)
    removed_count = sum(c["type"] == "removed" for c in changes)
    high_count = sum(c.get("priority") == "high" for c in changes)

    field_counts = {
        label: sum(
            c.get("field") == label
            for c in changes
        )
        for _, label, _ in TRACKED_FIELDS
    }

    rows = []

    for c in changes:
        typ = escape(str(c.get("type", "")))
        field = escape(str(c.get("field", "")))
        priority = escape(str(c.get("priority", "")))
        url = escape(str(c.get("url", "")))

        if typ == "new":
            badge = '<span class="new">NEW</span>'
        elif typ == "removed":
            badge = '<span class="removed">REMOVED</span>'
        elif typ == "failed":
            badge = '<span class="failed">FAILED</span>'
        else:
            badge = '<span class="changed">CHANGED</span>'

        old_value = escape(str(c.get("old", "")))
        new_value = escape(str(c.get("new", "")))

        if typ == "changed":
            detail = f"""
            <details>
                <summary>View old → new</summary>
                <div class="compare">
                    <div class="old">
                        <b>OLD</b>
                        <div>{old_value}</div>
                    </div>
                    <div class="arrow">→</div>
                    <div class="newbox">
                        <b>NEW</b>
                        <div>{new_value}</div>
                    </div>
                </div>
            </details>
            """
        else:
            detail = escape(str(c.get("details", "")))

        rows.append(f"""
        <tr data-type="{typ}"
            data-field="{field.lower()}"
            data-priority="{priority}">
            <td>{badge}</td>
            <td><b>{priority.upper()}</b></td>
            <td>
                <a href="{url}" target="_blank" rel="noopener">
                    {url}
                </a>
            </td>
            <td><b>{field}</b></td>
            <td>{detail}</td>
        </tr>
        """)

    if not rows:
        rows.append("""
        <tr id="empty">
            <td colspan="5">No changes detected.</td>
        </tr>
        """)

    history_rows = []

    for item in reversed(history[-30:]):
        history_rows.append(f"""
        <tr>
            <td>{escape(str(item.get("date", "")))}</td>
            <td>{item.get("pages", 0)}</td>
            <td>{item.get("new", 0)}</td>
            <td>{item.get("changed", 0)}</td>
            <td>{item.get("removed", 0)}</td>
            <td>{item.get("failed", 0)}</td>
            <td>{item.get("duration", 0)} sec</td>
        </tr>
        """)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Website Dashboard</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f4f6f8;color:#202124}}
.container{{max-width:1500px;margin:auto;padding:30px}}
h1{{margin:0 0 6px;font-size:34px}}
.subtitle{{color:#6b7280;margin-bottom:25px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;margin-bottom:20px}}
.card,.health,.filters,.table-wrap{{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07)}}
.card{{padding:20px}}
.label{{color:#6b7280;font-size:14px}}
.number{{font-size:32px;font-weight:bold;margin-top:8px}}
.health,.filters{{padding:20px;margin-bottom:20px}}
.healthgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px}}
.small{{color:#777;font-size:13px}}
.value{{font-weight:bold;margin-top:5px}}
.filterrow{{display:flex;flex-wrap:wrap;gap:10px}}
input,select{{padding:11px 14px;border:1px solid #d1d5db;border-radius:8px;background:#fff}}
input{{flex:1;min-width:260px}}
.section{{margin:35px 0 12px;font-size:22px;font-weight:bold}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;min-width:1100px}}
th,td{{padding:15px;text-align:left;border-bottom:1px solid #edf0f2;vertical-align:top}}
th{{background:#fafafa;color:#555;font-size:13px}}
a{{color:#0969da;text-decoration:none;word-break:break-all}}
span{{display:inline-block;padding:5px 9px;border-radius:20px;font-size:11px;font-weight:bold}}
.new{{background:#dcfce7;color:#166534}}
.changed{{background:#fff3cd;color:#92400e}}
.removed{{background:#fee2e2;color:#991b1b}}
.failed{{background:#e5e7eb;color:#374151}}
details{{margin-top:8px}}
summary{{cursor:pointer;color:#0969da;font-weight:600}}
.compare{{display:grid;grid-template-columns:1fr 40px 1fr;gap:10px;margin-top:10px}}
.old,.newbox{{padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-word;max-height:350px;overflow:auto}}
.old{{background:#fff1f2;border:1px solid #fecdd3}}
.newbox{{background:#f0fdf4;border:1px solid #bbf7d0}}
.arrow{{display:flex;align-items:center;justify-content:center;font-size:24px;color:#777}}
#empty{{text-align:center;color:#6b7280}}
.footer{{margin-top:20px;color:#777;font-size:13px}}
@media(max-width:700px){{
.container{{padding:15px}}h1{{font-size:27px}}
.compare{{grid-template-columns:1fr}}.arrow{{display:none}}
}}
</style>
</head>
<body>
<div class="container">

<h1>Website Dashboard</h1>
<div class="subtitle">Page and content overview · Last checked: {now}</div>

<div class="cards">
<div class="card"><div class="label">Pages</div><div class="number">{len(pages)}</div></div>
<div class="card"><div class="label">New Pages</div><div class="number">{new_count}</div></div>
<div class="card"><div class="label">Changed</div><div class="number">{changed_count}</div></div>
<div class="card"><div class="label">Removed</div><div class="number">{removed_count}</div></div>
<div class="card"><div class="label">High Priority</div><div class="number">{high_count}</div></div>
</div>

<div class="cards">
<div class="card"><div class="label">Title Changes</div><div class="number">{field_counts.get("Title",0)}</div></div>
<div class="card"><div class="label">H1 Changes</div><div class="number">{field_counts.get("H1",0)}</div></div>
<div class="card"><div class="label">Description Changes</div><div class="number">{field_counts.get("Description",0)}</div></div>
<div class="card"><div class="label">Canonical Changes</div><div class="number">{field_counts.get("Canonical",0)}</div></div>
<div class="card"><div class="label">Robots Changes</div><div class="number">{field_counts.get("Robots",0)}</div></div>
<div class="card"><div class="label">Content Changes</div><div class="number">{field_counts.get("Content",0)}</div></div>
<div class="card"><div class="label">Image Changes</div><div class="number">{field_counts.get("Images",0)}</div></div>
<div class="card"><div class="label">Link Changes</div><div class="number">{field_counts.get("Internal Links",0)}</div></div>
<div class="card"><div class="label">Schema Changes</div><div class="number">{field_counts.get("Schema",0)}</div></div>
</div>

<div class="health">
<div class="healthgrid">
<div><div class="small">Crawl Status</div><div class="value">✓ Successful</div></div>
<div><div class="small">Pages Scanned</div><div class="value">{len(pages)}</div></div>
<div><div class="small">Failed</div><div class="value">{failed_count}</div></div>
<div><div class="small">Crawl Duration</div><div class="value">{duration} seconds</div></div>
</div>
</div>

<div class="filters">
<div class="filterrow">
<input id="search" placeholder="Search URL..." onkeyup="filterRows()">
<select id="type" onchange="filterRows()">
<option value="all">All Types</option>
<option value="new">New</option>
<option value="changed">Changed</option>
<option value="removed">Removed</option>
<option value="failed">Failed</option>
</select>
<select id="field" onchange="filterRows()">
<option value="all">All Elements</option>
<option value="title">Title</option>
<option value="h1">H1</option>
<option value="description">Description</option>
<option value="canonical">Canonical</option>
<option value="robots">Robots</option>
<option value="content">Content</option>
<option value="images">Images</option>
<option value="internal links">Internal Links</option>
<option value="schema">Schema</option>
<option value="crawl">Crawl</option>
</select>
<select id="priority" onchange="filterRows()">
<option value="all">All Priority</option>
<option value="high">High</option>
<option value="medium">Medium</option>
<option value="low">Low</option>
</select>
</div>
</div>

<div class="section">Current Changes</div>
<div class="table-wrap">
<table>
<thead><tr><th>Type</th><th>Priority</th><th>Page</th><th>Element</th><th>Details</th></tr></thead>
<tbody id="changeTable">
{"".join(rows)}
</tbody>
</table>
</div>

<div class="section">Change History</div>
<div class="table-wrap">
<table>
<thead><tr><th>Date</th><th>Pages</th><th>New</th><th>Changed</th><th>Removed</th><th>Failed</th><th>Duration</th></tr></thead>
<tbody>{"".join(history_rows)}</tbody>
</table>
</div>

<div class="footer">
Pages scanned: {len(pages)} · Total detected changes: {len(changes)} · History records: {len(history)}
</div>

</div>

<script>
function filterRows(){{
const search=document.getElementById("search").value.toLowerCase();
const type=document.getElementById("type").value;
const field=document.getElementById("field").value;
const priority=document.getElementById("priority").value;

document.querySelectorAll("#changeTable tr").forEach(row=>{{
const t=row.dataset.type||"";
const f=row.dataset.field||"";
const p=row.dataset.priority||"";
const text=row.innerText.toLowerCase();

row.style.display =
(text.includes(search) &&
(type==="all"||t===type) &&
(field==="all"||f===field) &&
(priority==="all"||p===priority)) ? "" : "none";
}});
}}
</script>

</body>
</html>
"""

    DASHBOARD_FILE.write_text(html, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main():
    DATA_DIR.mkdir(exist_ok=True)

    old_pages = {}

    if SNAPSHOT_FILE.exists():
        try:
            data = json.loads(
                SNAPSHOT_FILE.read_text(encoding="utf-8")
            )
            old_pages = data.get("pages", {})
        except Exception:
            old_pages = {}

    print(f"[BASELINE] {len(old_pages)} pages", flush=True)

    result = crawl(old_pages)

    pages = result["pages"]
    failed = result["failed"]
    removed = result["removed"]
    duration = result["duration"]

    changes = compare(
        old_pages,
        pages,
        failed,
        removed,
    )

    print(f"[RESULT] {len(changes)} changes", flush=True)
    print(f"[PAGES] {len(pages)} pages", flush=True)
    print(f"[FAILED] {len(failed)} pages", flush=True)
    print(f"[REMOVED] {len(removed)} pages", flush=True)

    snapshot = {
        "site": BASE_URL,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
        "changes": changes,
        "failed": failed,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    history = save_history(
        pages=pages,
        changes=changes,
        duration=duration,
        failed_count=len(failed),
    )

    make_dashboard(
        pages=pages,
        changes=changes,
        history=history,
        duration=duration,
        failed_count=len(failed),
    )

    print(
        f"[COMPLETE] Duration: {duration}s",
        flush=True
    )


if __name__ == "__main__":
    main()
