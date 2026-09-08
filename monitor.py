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
        "Mozilla/5.0 (compatible; WebsiteAudit/1.0; "
        "+https://github.com/)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# URL HELPERS
# ============================================================

def clean_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return None

        base_host = urlparse(BASE_URL).netloc.lower()

        if parsed.netloc.lower() != base_host:
            return None

        path = parsed.path or "/"

        ignored_extensions = (
            ".jpg", ".jpeg", ".png", ".gif", ".webp",
            ".svg", ".ico", ".pdf", ".zip", ".mp4",
            ".mp3", ".webm", ".css", ".js", ".xml",
            ".json", ".woff", ".woff2", ".ttf"
        )

        if path.lower().endswith(ignored_extensions):
            return None

        clean = (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{path}"
        )

        if path != "/" and clean.endswith("/"):
            clean = clean[:-1]

        return clean

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

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if (
            status == 200
            and "text/html" in content_type
        ):

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

    except requests.RequestException:

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
        }


# ============================================================
# EXTRACT
# ============================================================

def extract(url, html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = ""

    if soup.title:

        title = soup.title.get_text(
            " ",
            strip=True
        )

    # --------------------------------------------------------
    # DESCRIPTION
    # --------------------------------------------------------

    description = ""

    description_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^description$",
                re.I
            )
        }
    )

    if description_tag:

        description = description_tag.get(
            "content",
            ""
        ).strip()

    # --------------------------------------------------------
    # CANONICAL
    # --------------------------------------------------------

    canonical = ""

    canonical_tag = soup.find(
        "link",
        attrs={
            "rel": lambda value:
                value and "canonical" in value
        }
    )

    if canonical_tag:

        canonical = canonical_tag.get(
            "href",
            ""
        ).strip()

    # --------------------------------------------------------
    # H1
    # --------------------------------------------------------

    h1_tags = soup.find_all("h1")

    h1 = " | ".join(
        tag.get_text(
            " ",
            strip=True
        )
        for tag in h1_tags
    )

    # --------------------------------------------------------
    # ROBOTS
    # --------------------------------------------------------

    robots = ""

    robots_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^robots$",
                re.I
            )
        }
    )

    if robots_tag:

        robots = robots_tag.get(
            "content",
            ""
        ).strip()

    # --------------------------------------------------------
    # IMAGES
    # --------------------------------------------------------

    images = []

    for img in soup.find_all("img"):

        src = (
            img.get("src")
            or img.get("data-src")
            or ""
        ).strip()

        alt = img.get(
            "alt",
            ""
        ).strip()

        if src:

            images.append({
                "src": src,
                "alt": alt,
            })

    images = sorted(
        images,
        key=lambda x: (
            x.get("src", ""),
            x.get("alt", "")
        )
    )

    # --------------------------------------------------------
    # INTERNAL LINKS
    # --------------------------------------------------------

    internal_links = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href", "").strip()

        absolute = urljoin(
            url,
            href
        )

        cleaned = clean_url(
            absolute
        )

        if cleaned:

            internal_links.add(
                cleaned
            )

    internal_links = sorted(
        internal_links
    )

    # --------------------------------------------------------
    # JSON-LD / SCHEMA
    # --------------------------------------------------------

    schemas = []

    for script in soup.find_all(
        "script",
        attrs={
            "type": re.compile(
                r"application/ld\+json",
                re.I
            )
        }
    ):

        raw = script.string or script.get_text()

        raw = raw.strip()

        if raw:

            try:

                parsed = json.loads(raw)

                schemas.append(
                    parsed
                )

            except Exception:

                schemas.append(
                    raw
                )

    # --------------------------------------------------------
    # MAIN CONTENT
    # --------------------------------------------------------

    content_soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in content_soup(
        ["script", "style", "noscript", "svg"]
    ):

        tag.decompose()

    main = content_soup.find("main")

    if main:

        content = main.get_text(
            " ",
            strip=True
        )

    else:

        content = content_soup.get_text(
            " ",
            strip=True
        )

    content = re.sub(
        r"\s+",
        " ",
        content
    ).strip()

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

        "schema": schemas,
    }


# ============================================================
# SITEMAP
# ============================================================

def get_sitemap_urls():

    discovered = set()

    sitemap_success = False

    candidates = [
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml",
    ]

    for sitemap_url in candidates:

        print(
            f"[SITEMAP] Checking {sitemap_url}",
            flush=True
        )

        try:

            response = requests.get(
                sitemap_url,
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            if response.status_code != 200:
                continue

            sitemap_success = True

            soup = BeautifulSoup(
                response.text,
                "xml"
            )

            for loc in soup.find_all("loc"):

                value = loc.get_text(
                    strip=True
                )

                if value.lower().endswith(".xml"):

                    try:

                        child = requests.get(
                            value,
                            headers=HEADERS,
                            timeout=TIMEOUT,
                        )

                        if child.status_code != 200:
                            continue

                        child_soup = BeautifulSoup(
                            child.text,
                            "xml"
                        )

                        for item in child_soup.find_all("loc"):

                            page = clean_url(
                                item.get_text(
                                    strip=True
                                )
                            )

                            if page:
                                discovered.add(page)

                    except requests.RequestException:
                        continue

                else:

                    page = clean_url(
                        value
                    )

                    if page:
                        discovered.add(page)

            if discovered:

                print(
                    f"[SITEMAP] "
                    f"{len(discovered)} HTML URLs found",
                    flush=True
                )

                return (
                    discovered,
                    sitemap_success
                )

        except requests.RequestException as error:

            print(
                f"[SITEMAP ERROR] {error}",
                flush=True
            )

    print(
        "[SITEMAP] No usable sitemap found",
        flush=True
    )

    return (
        discovered,
        sitemap_success
    )


# ============================================================
# CRAWL
# ============================================================

def crawl(old_pages):

    start_time = time.time()

    sitemap_urls, sitemap_ok = (
        get_sitemap_urls()
    )

    homepage = clean_url(
        BASE_URL
    )

    if homepage:
        sitemap_urls.add(homepage)

    if not sitemap_ok and old_pages:

        urls = set(
            old_pages.keys()
        )

        print(
            "[WARNING] Sitemap unavailable. "
            "Using previous URLs.",
            flush=True
        )

    else:

        urls = set(
            sitemap_urls
        )

        urls.update(
            old_pages.keys()
        )

    urls = {
        clean_url(url)
        for url in urls
    }

    urls.discard(None)

    urls = sorted(urls)[:MAX_PAGES]

    print(
        f"[START] {len(urls)} pages",
        flush=True
    )

    pages = {}

    failed = {}

    removed = set()

    # --------------------------------------------------------
    # FIRST PASS
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        jobs = {
            executor.submit(fetch, url): url
            for url in urls
        }

        completed = 0

        for future in as_completed(jobs):

            url = jobs[future]

            try:

                result = future.result()

                status = result["status"]

                if status == "success":

                    pages[url] = extract(
                        url,
                        result["html"]
                    )

                elif status == "removed":

                    removed.add(url)

                else:

                    failed[url] = {
                        "http_status":
                            result.get(
                                "http_status"
                            )
                    }

            except Exception as error:

                failed[url] = {
                    "http_status": None,
                    "error": str(error)
                }

            completed += 1

            if (
                completed % 25 == 0
                or completed == len(urls)
            ):

                print(
                    f"[PROGRESS] "
                    f"{completed}/{len(urls)}",
                    flush=True
                )

    # --------------------------------------------------------
    # DISCOVER INTERNAL LINKS
    # --------------------------------------------------------

    discovered_links = set()

    for data in pages.values():

        for link in data.get(
            "internal_links",
            []
        ):

            cleaned = clean_url(link)

            if cleaned:
                discovered_links.add(cleaned)

    additional = (
        discovered_links
        - set(pages.keys())
        - set(failed.keys())
        - removed
    )

    additional = sorted(
        additional
    )

    remaining_slots = (
        MAX_PAGES
        - len(pages)
    )

    if remaining_slots > 0:

        additional = additional[
            :remaining_slots
        ]

    else:

        additional = []

    print(
        f"[DISCOVERY] "
        f"{len(additional)} additional internal-link pages",
        flush=True
    )

    # --------------------------------------------------------
    # SECOND PASS
    # --------------------------------------------------------

    if additional:

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            jobs = {
                executor.submit(fetch, url): url
                for url in additional
            }

            completed = 0

            for future in as_completed(jobs):

                url = jobs[future]

                try:

                    result = future.result()

                    status = result["status"]

                    if status == "success":

                        pages[url] = extract(
                            url,
                            result["html"]
                        )

                    elif status == "removed":

                        removed.add(url)

                    else:

                        failed[url] = {
                            "http_status":
                                result.get(
                                    "http_status"
                                )
                        }

                except Exception as error:

                    failed[url] = {
                        "http_status": None,
                        "error": str(error)
                    }

                completed += 1

                if (
                    completed % 25 == 0
                    or completed == len(additional)
                ):

                    print(
                        f"[DISCOVERY PROGRESS] "
                        f"{completed}/{len(additional)}",
                        flush=True
                    )

    duration = round(
        time.time() - start_time,
        2
    )

    print(
        f"[DONE] {len(pages)} pages collected",
        flush=True
    )

    print(
        f"[FAILED] {len(failed)} pages",
        flush=True
    )

    print(
        f"[REMOVED] {len(removed)} pages",
        flush=True
    )

    print(
        f"[DURATION] {duration} seconds",
        flush=True
    )

    return {
        "pages": pages,
        "failed": failed,
        "removed": removed,
        "duration": duration,
    }


# ============================================================
# COMPARE
# ============================================================

def compare(old, new, failed, removed):

    changes = []

    old_urls = set(old)
    new_urls = set(new)

    # --------------------------------------------------------
    # NEW
    # --------------------------------------------------------

    for url in sorted(
        new_urls - old_urls
    ):

        changes.append({
            "type": "new",
            "url": url,
            "field": "Page",
            "details": "New page discovered",
            "priority": "medium",
        })

    # --------------------------------------------------------
    # REMOVED
    # --------------------------------------------------------

    for url in sorted(
        (old_urls - new_urls) & removed
    ):

        changes.append({
            "type": "removed",
            "url": url,
            "field": "Page",
            "details": "Page returned 404/410",
            "priority": "high",
        })

    # --------------------------------------------------------
    # FAILED
    # --------------------------------------------------------

    for url in sorted(failed):

        changes.append({
            "type": "failed",
            "url": url,
            "field": "Crawl",
            "details": "Temporary crawl failure",
            "priority": "medium",
        })

    # --------------------------------------------------------
    # EXISTING PAGE CHANGES
    # --------------------------------------------------------

    fields = [
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

    for url in sorted(
        old_urls & new_urls
    ):

        before = old[url]
        after = new[url]

        for field, label, priority in fields:

            # New fields should not create false
            # changes when upgrading old snapshots.
            if field not in before:
                continue

            if before.get(field) != after.get(field):

                changes.append({
                    "type": "changed",
                    "url": url,
                    "field": label,
                    "old": before.get(
                        field,
                        ""
                    ),
                    "new": after.get(
                        field,
                        ""
                    ),
                    "details": f"{label} changed",
                    "priority": priority,
                })

    return changes


# ============================================================
# HISTORY
# ============================================================

def save_history(
    pages,
    changes,
    duration,
    failed_count
):

    history = []

    if HISTORY_FILE.exists():

        try:

            history = json.loads(
                HISTORY_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(
                history,
                list
            ):

                history = []

        except Exception:

            history = []

    now = datetime.now(
        timezone.utc
    )

    new_count = sum(
        c.get("type") == "new"
        for c in changes
    )

    changed_count = sum(
        c.get("type") == "changed"
        for c in changes
    )

    removed_count = sum(
        c.get("type") == "removed"
        for c in changes
    )

    change_details = []

    for change in changes:

        item = {
            "type": change.get("type", ""),
            "url": change.get("url", ""),
            "field": change.get("field", ""),
            "priority": change.get("priority", ""),
            "details": change.get("details", ""),
        }

        if "old" in change:
            item["old"] = change.get("old", "")

        if "new" in change:
            item["new"] = change.get("new", "")

        change_details.append(item)

    history.append({

        "date":
            now.strftime(
                "%Y-%m-%d"
            ),

        "checked_at":
            now.isoformat(),

        "pages":
            len(pages),

        "new":
            new_count,

        "changed":
            changed_count,

        "removed":
            removed_count,

        "failed":
            failed_count,

        "duration":
            duration,

        "changes":
            change_details,
    })

    history = history[-365:]

    HISTORY_FILE.write_text(
        json.dumps(
            history,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    return history


# ============================================================
# DASHBOARD
# ============================================================

def make_dashboard(
    pages,
    changes,
    history,
    duration,
    failed_count,
    first_run
):

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    new_count = sum(
        c.get("type") == "new"
        for c in changes
    )

    changed_count = sum(
        c.get("type") == "changed"
        for c in changes
    )

    removed_count = sum(
        c.get("type") == "removed"
        for c in changes
    )

    failed_change_count = sum(
        c.get("type") == "failed"
        for c in changes
    )

    high_priority_count = sum(
        c.get("priority") == "high"
        for c in changes
    )

    def field_count(name):

        return sum(
            c.get("type") == "changed"
            and c.get("field") == name
            for c in changes
        )

    title_count = field_count("Title")
    h1_count = field_count("H1")
    description_count = field_count("Description")
    canonical_count = field_count("Canonical")
    robots_count = field_count("Robots")
    content_count = field_count("Content")
    image_count = field_count("Images")
    link_count = field_count("Internal Links")
    schema_count = field_count("Schema")

    rows = []

    for change in changes:

        change_type = escape(
            str(change.get("type", "")).upper()
        )

        field = escape(
            str(change.get("field", ""))
        )

        priority = escape(
            str(change.get("priority", "")).upper()
        )

        url = escape(
            str(change.get("url", ""))
        )

        details = escape(
            str(change.get("details", ""))
        )

        old_value = escape(
            str(change.get("old", ""))
        )

        new_value = escape(
            str(change.get("new", ""))
        )

        rows.append(
            f"""
            <tr
                data-type="{escape(str(change.get("type", "")))}"
                data-field="{escape(str(change.get("field", "")))}"
                data-priority="{escape(str(change.get("priority", "")))}"
            >
                <td><strong>{change_type}</strong></td>
                <td><strong>{priority}</strong></td>
                <td>
                    <a
                        href="{url}"
                        target="_blank"
                        rel="noopener noreferrer"
                    >
                        {url}
                    </a>
                </td>
                <td>{field}</td>
                <td>{details}</td>
                <td>
                    {
                        (
                            "<details>"
                            "<summary>View</summary>"
                            "<div><b>OLD:</b><br>"
                            f"{old_value}"
                            "</div><br>"
                            "<div><b>NEW:</b><br>"
                            f"{new_value}"
                            "</div>"
                            "</details>"
                        )
                        if change.get("type") == "changed"
                        else ""
                    }
                </td>
            </tr>
            """
        )

    rows_html = "\n".join(rows)

    if not rows_html:

        rows_html = """
        <tr>
            <td colspan="6">
                No changes detected.
            </td>
        </tr>
        """

    notice = ""

    if first_run:

        notice = """
        <div class="notice">
            First crawl completed. This run creates the
            baseline for future comparisons.
        </div>
        """

    history_rows = []

    for item in reversed(history):

        history_rows.append(
            f"""
            <tr>
                <td>
                    {escape(str(item.get("checked_at", "")))}
                </td>
                <td>{item.get("pages", 0)}</td>
                <td>{item.get("new", 0)}</td>
                <td>{item.get("changed", 0)}</td>
                <td>{item.get("removed", 0)}</td>
                <td>{item.get("failed", 0)}</td>
                <td>{item.get("duration", 0)} sec</td>
            </tr>
            """
        )

    history_html = "\n".join(
        history_rows
    )

    html = f"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Website Dashboard</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    font-family: Arial, Helvetica, sans-serif;
    margin: 0;
    background: #f4f6f8;
    color: #202124;
}}

.container {{
    max-width: 1500px;
    margin: auto;
    padding: 30px;
}}

h1 {{
    margin: 0 0 6px 0;
    font-size: 34px;
}}

.subtitle {{
    color: #6b7280;
    margin-bottom: 25px;
    font-size: 15px;
}}

.notice {{
    background: #e8f4ff;
    border: 1px solid #b9ddff;
    padding: 18px;
    margin-bottom: 25px;
    border-radius: 10px;
}}

.cards {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 15px;
    margin-bottom: 20px;
}}

.card {{
    background: white;
    padding: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.07);
}}

.card-title {{
    color: #6b7280;
    font-size: 14px;
}}

.number {{
    font-size: 32px;
    font-weight: bold;
    margin-top: 8px;
}}

.health {{
    background: white;
    padding: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.07);
    margin-bottom: 20px;
}}

.health-grid {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 15px;
}}

.health-label {{
    color: #777;
    font-size: 13px;
}}

.health-value {{
    font-weight: bold;
    margin-top: 5px;
}}

.filters {{
    background: white;
    padding: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.07);
    margin-bottom: 20px;
}}

.filter-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}}

.search {{
    flex: 1;
    min-width: 260px;
    padding: 11px 14px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font-size: 14px;
}}

select {{
    padding: 11px 14px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    background: white;
}}

.section {{
    background: white;
    padding: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.07);
    margin-bottom: 20px;
}}

.table-wrap {{
    overflow-x: auto;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th, td {{
    padding: 13px;
    border-bottom: 1px solid #e5e7eb;
    text-align: left;
    vertical-align: top;
}}

th {{
    background: #f9fafb;
}}

a {{
    color: #0969da;
    word-break: break-word;
}}

details {{
    max-width: 500px;
}}

.footer {{
    margin-top: 20px;
    color: #777;
    font-size: 13px;
}}

@media (max-width: 700px) {{

    .container {{
        padding: 15px;
    }}

    h1 {{
        font-size: 27px;
    }}

}}

</style>

</head>

<body>

<div class="container">

<h1>Website Dashboard</h1>

<div class="subtitle">
    Page and content overview · Last checked: {now}
</div>

{notice}

<!-- OVERVIEW -->

<div class="cards">

<div class="card">
<div class="card-title">Pages</div>
<div class="number">{len(pages)}</div>
</div>

<div class="card">
<div class="card-title">New Pages</div>
<div class="number">{new_count}</div>
</div>

<div class="card">
<div class="card-title">Changed</div>
<div class="number">{changed_count}</div>
</div>

<div class="card">
<div class="card-title">Removed</div>
<div class="number">{removed_count}</div>
</div>

<div class="card">
<div class="card-title">Failed</div>
<div class="number">{failed_change_count}</div>
</div>

<div class="card">
<div class="card-title">High Priority</div>
<div class="number">{high_priority_count}</div>
</div>

</div>


<!-- SEO -->

<div class="cards">

<div class="card">
<div class="card-title">Title Changes</div>
<div class="number">{title_count}</div>
</div>

<div class="card">
<div class="card-title">H1 Changes</div>
<div class="number">{h1_count}</div>
</div>

<div class="card">
<div class="card-title">Description Changes</div>
<div class="number">{description_count}</div>
</div>

<div class="card">
<div class="card-title">Canonical Changes</div>
<div class="number">{canonical_count}</div>
</div>

<div class="card">
<div class="card-title">Robots Changes</div>
<div class="number">{robots_count}</div>
</div>

<div class="card">
<div class="card-title">Content Changes</div>
<div class="number">{content_count}</div>
</div>

<div class="card">
<div class="card-title">Image Changes</div>
<div class="number">{image_count}</div>
</div>

<div class="card">
<div class="card-title">Link Changes</div>
<div class="number">{link_count}</div>
</div>

<div class="card">
<div class="card-title">Schema Changes</div>
<div class="number">{schema_count}</div>
</div>

</div>


<!-- HEALTH -->

<div class="health">

<div class="health-grid">

<div>
<div class="health-label">Crawl Status</div>
<div class="health-value">
{"✓ Successful" if failed_count == 0 else "⚠ Needs Attention"}
</div>
</div>

<div>
<div class="health-label">Pages Scanned</div>
<div class="health-value">{len(pages)}</div>
</div>

<div>
<div class="health-label">Failed</div>
<div class="health-value">{failed_count}</div>
</div>

<div>
<div class="health-label">Crawl Duration</div>
<div class="health-value">{duration} seconds</div>
</div>

</div>

</div>


<!-- FILTERS -->

<div class="filters">

<div class="filter-row">

<input
    class="search"
    id="search"
    type="text"
    placeholder="Search URL, field or details..."
    onkeyup="filterRows()"
>

<select id="typeFilter" onchange="filterRows()">

<option value="all">All Types</option>
<option value="new">New</option>
<option value="changed">Changed</option>
<option value="removed">Removed</option>
<option value="failed">Failed</option>

</select>

<select id="fieldFilter" onchange="filterRows()">

<option value="all">All Fields</option>
<option value="Title">Title</option>
<option value="H1">H1</option>
<option value="Description">Description</option>
<option value="Canonical">Canonical</option>
<option value="Robots">Robots</option>
<option value="Content">Content</option>
<option value="Images">Images</option>
<option value="Internal Links">Internal Links</option>
<option value="Schema">Schema</option>
<option value="Page">Page</option>
<option value="Crawl">Crawl</option>

</select>

<select id="priorityFilter" onchange="filterRows()">

<option value="all">All Priority</option>
<option value="high">High</option>
<option value="medium">Medium</option>
<option value="low">Low</option>

</select>

</div>

</div>


<!-- CURRENT CHANGES -->

<div class="section">

<h2>Current Changes</h2>

<div class="table-wrap">

<table id="changeTable">

<thead>

<tr>

<th>Type</th>
<th>Priority</th>
<th>Page</th>
<th>Element</th>
<th>Details</th>
<th>Values</th>

</tr>

</thead>

<tbody>

{rows_html}

</tbody>

</table>

</div>

</div>


<!-- HISTORY -->

<div class="section">

<h2>History</h2>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>Date</th>
<th>Pages</th>
<th>New</th>
<th>Changed</th>
<th>Removed</th>
<th>Failed</th>
<th>Duration</th>

</tr>

</thead>

<tbody>

{history_html}

</tbody>

</table>

</div>

</div>


<div class="footer">

Pages scanned: {len(pages)}
· Current changes: {len(changes)}
· History records: {len(history)}

</div>

</div>


<script>

function filterRows() {{

    const search =
        document
            .getElementById("search")
            .value
            .toLowerCase();

    const type =
        document
            .getElementById("typeFilter")
            .value;

    const field =
        document
            .getElementById("fieldFilter")
            .value;

    const priority =
        document
            .getElementById("priorityFilter")
            .value;

    const rows =
        document.querySelectorAll(
            "#changeTable tbody tr"
        );

    rows.forEach(function(row) {{

        const rowType =
            row.dataset.type || "";

        const rowField =
            row.dataset.field || "";

        const rowPriority =
            row.dataset.priority || "";

        const rowText =
            row.innerText.toLowerCase();

        const matchesSearch =
            rowText.includes(search);

        const matchesType =
            type === "all"
            || rowType === type;

        const matchesField =
            field === "all"
            || rowField === field;

        const matchesPriority =
            priority === "all"
            || rowPriority === priority;

        row.style.display =
            (
                matchesSearch
                && matchesType
                && matchesField
                && matchesPriority
            )
            ? ""
            : "none";

    }});

}}

</script>

</body>

</html>
"""

    DASHBOARD_FILE.write_text(
        html,
        encoding="utf-8"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    DATA_DIR.mkdir(
        exist_ok=True
    )

    old_pages = {}

    first_run = not SNAPSHOT_FILE.exists()

    if SNAPSHOT_FILE.exists():

        try:

            data = json.loads(
                SNAPSHOT_FILE.read_text(
                    encoding="utf-8"
                )
            )

            old_pages = data.get(
                "pages",
                {}
            )

        except Exception:

            old_pages = {}

    print(
        f"[BASELINE] {len(old_pages)} pages",
        flush=True
    )

    crawl_result = crawl(
        old_pages
    )

    pages = crawl_result["pages"]

    failed = crawl_result["failed"]

    removed = crawl_result["removed"]

    duration = crawl_result["duration"]

    changes = compare(
        old_pages,
        pages,
        failed,
        removed
    )

    # --------------------------------------------------------
    # SAVE SNAPSHOT
    # --------------------------------------------------------

    snapshot = {

        "site":
            BASE_URL,

        "checked_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "pages":
            pages,

        "changes":
            changes,

        "failed":
            failed,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(
            snapshot,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------

    history = save_history(
        pages=pages,
        changes=changes,
        duration=duration,
        failed_count=len(failed),
    )

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

    make_dashboard(
        pages=pages,
        changes=changes,
        history=history,
        duration=duration,
        failed_count=len(failed),
        first_run=first_run,
    )

    # --------------------------------------------------------
    # CLEAN FINAL LOGS
    # --------------------------------------------------------

    new_count = sum(
        c.get("type") == "new"
        for c in changes
    )

    changed_count = sum(
        c.get("type") == "changed"
        for c in changes
    )

    removed_count = sum(
        c.get("type") == "removed"
        for c in changes
    )

    failed_count = sum(
        c.get("type") == "failed"
        for c in changes
    )

    print(
        f"[NEW] {new_count} pages",
        flush=True
    )

    print(
        f"[CHANGED] {changed_count} records",
        flush=True
    )

    print(
        f"[REMOVED] {removed_count} pages",
        flush=True
    )

    print(
        f"[FAILED] {failed_count} pages",
        flush=True
    )

    print(
        f"[PAGES] {len(pages)} pages",
        flush=True
    )

    print(
        f"[COMPLETE] Duration: {duration}s",
        flush=True
    )


if __name__ == "__main__":
    main()
