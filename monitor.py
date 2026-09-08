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
    )
}


# ============================================================
# URL HELPERS
# ============================================================

def clean_url(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return None

        if parsed.netloc.lower() != urlparse(BASE_URL).netloc.lower():
            return None

        path = parsed.path or "/"

        url = f"{parsed.scheme}://{parsed.netloc}{path}"

        if path != "/" and url.endswith("/"):
            url = url[:-1]

        return url

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

        if status == 200:

            content_type = response.headers.get(
                "content-type",
                ""
            ).lower()

            if "text/html" in content_type:

                return {
                    "status": "success",
                    "html": response.text,
                    "http_status": status,
                }

            return {
                "status": "failed",
                "html": "",
                "http_status": status,
            }

        # Real page-not-found response
        if status in (404, 410):

            return {
                "status": "removed",
                "html": "",
                "http_status": status,
            }

        # Temporary/server/rate-limit problems
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

    for tag in soup(
        ["script", "style", "noscript", "svg"]
    ):

        tag.decompose()

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

        href = canonical_tag.get(
            "href",
            ""
        ).strip()

        if href:

            canonical = urljoin(
                url,
                href
            )

    # --------------------------------------------------------
    # H1
    # --------------------------------------------------------

    h1 = [
        x.get_text(
            " ",
            strip=True
        )
        for x in soup.find_all("h1")
    ]

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
    # CONTENT
    # --------------------------------------------------------

    main = soup.find("main")

    if main:

        content = main.get_text(
            " ",
            strip=True
        )

    else:

        content = soup.get_text(
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

            locations = soup.find_all("loc")

            for loc in locations:

                value = loc.get_text(
                    strip=True
                )

                # ------------------------------------------------
                # Sitemap index
                # ------------------------------------------------

                if value.endswith(".xml"):

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

                        for item in child_soup.find_all(
                            "loc"
                        ):

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

                    page = clean_url(value)

                    if page:
                        discovered.add(page)

            if discovered:

                return discovered, sitemap_success

        except requests.RequestException:

            continue

    return discovered, sitemap_success


# ============================================================
# CRAWL
# ============================================================

def crawl(old_pages):

    start_time = time.time()

    sitemap_urls, sitemap_ok = get_sitemap_urls()

    homepage = clean_url(BASE_URL)

    if homepage:

        sitemap_urls.add(homepage)

    # --------------------------------------------------------
    # SAFETY:
    # If sitemap fails completely, use old URLs instead of
    # treating every existing page as removed.
    # --------------------------------------------------------

    if not sitemap_ok and old_pages:

        urls = set(old_pages.keys())

        print(
            "[WARNING] Sitemap unavailable. "
            "Using previous URLs for safe crawl.",
            flush=True
        )

    else:

        urls = set(sitemap_urls)

        # Existing URLs are also checked so that a page removed
        # from sitemap can be detected as removed.
        urls.update(old_pages.keys())

    urls = sorted(urls)[:MAX_PAGES]

    print(
        f"[START] {len(urls)} pages",
        flush=True
    )

    pages = {}
    failed = {}
    removed = set()

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        jobs = {
            executor.submit(
                fetch,
                url
            ): url
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
                            result.get("http_status")
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

    # --------------------------------------------------------
    # NEW PAGES
    # --------------------------------------------------------

    for url in sorted(
        new_urls - old_urls
    ):

        changes.append({
            "type": "new",
            "url": url,
            "field": "Page",
            "details": "New page",
            "priority": "medium",
        })

    # --------------------------------------------------------
    # REMOVED PAGES
    # --------------------------------------------------------

    # Only mark pages as removed if we actually received
    # a 404/410 response.
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
    # FAILED PAGES
    # --------------------------------------------------------

    for url in sorted(failed):

        changes.append({
            "type": "failed",
            "url": url,
            "field": "Crawl",
            "details": (
                "Temporary crawl failure"
            ),
            "priority": "medium",
        })

    # --------------------------------------------------------
    # SEO / CONTENT CHANGES
    # --------------------------------------------------------

    fields = [
        ("title", "Title", "high"),
        ("description", "Description", "medium"),
        ("canonical", "Canonical", "high"),
        ("h1", "H1", "high"),
        ("robots", "Robots", "high"),
        ("content", "Content", "low"),
    ]

    for url in sorted(
        old_urls & new_urls
    ):

        before = old[url]
        after = new[url]

        for field, label, priority in fields:

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
    failed_count,
    removed_count
):

    history = []

    if HISTORY_FILE.exists():

        try:

            history = json.loads(
                HISTORY_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(history, list):

                history = []

        except Exception:

            history = []

    now = datetime.now(
        timezone.utc
    )

    new_count = sum(
        c["type"] == "new"
        for c in changes
    )

    changed_count = sum(
        c["type"] == "changed"
        for c in changes
    )

    actual_removed_count = sum(
        c["type"] == "removed"
        for c in changes
    )

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
            actual_removed_count,

        "failed":
            failed_count,

        "duration":
            duration,

    })

    # Keep last 365 crawl records
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

    # ========================================================
    # COUNTS
    # ========================================================

    new_count = sum(
        c["type"] == "new"
        for c in changes
    )

    changed_count = sum(
        c["type"] == "changed"
        for c in changes
    )

    removed_count = sum(
        c["type"] == "removed"
        for c in changes
    )

    title_count = sum(
        c.get("field") == "Title"
        for c in changes
    )

    h1_count = sum(
        c.get("field") == "H1"
        for c in changes
    )

    description_count = sum(
        c.get("field") == "Description"
        for c in changes
    )

    canonical_count = sum(
        c.get("field") == "Canonical"
        for c in changes
    )

    robots_count = sum(
        c.get("field") == "Robots"
        for c in changes
    )

    content_count = sum(
        c.get("field") == "Content"
        for c in changes
    )

    high_priority_count = sum(
        c.get("priority") == "high"
        for c in changes
    )

    # ========================================================
    # ROWS
    # ========================================================

    rows = []

    for change in changes:

        change_type = escape(
            str(change.get("type", ""))
        )

        field = escape(
            str(change.get("field", ""))
        )

        url = escape(
            str(change.get("url", ""))
        )

        priority = escape(
            str(change.get("priority", ""))
        )

        old_value = escape(
            str(change.get("old", ""))
        )

        new_value = escape(
            str(change.get("new", ""))
        )

        details = escape(
            str(change.get("details", ""))
        )

        # ----------------------------------------------------
        # TYPE BADGE
        # ----------------------------------------------------

        if change_type == "new":

            type_badge = (
                '<span class="badge badge-new">'
                'NEW'
                '</span>'
            )

        elif change_type == "removed":

            type_badge = (
                '<span class="badge badge-removed">'
                'REMOVED'
                '</span>'
            )

        elif change_type == "failed":

            type_badge = (
                '<span class="badge badge-failed">'
                'FAILED'
                '</span>'
            )

        else:

            type_badge = (
                '<span class="badge badge-changed">'
                'CHANGED'
                '</span>'
            )

        # ----------------------------------------------------
        # PRIORITY
        # ----------------------------------------------------

        if priority == "high":

            priority_badge = (
                '<span class="priority high">'
                'HIGH'
                '</span>'
            )

        elif priority == "medium":

            priority_badge = (
                '<span class="priority medium">'
                'MEDIUM'
                '</span>'
            )

        else:

            priority_badge = (
                '<span class="priority low">'
                'LOW'
                '</span>'
            )

        # ----------------------------------------------------
        # DETAILS
        # ----------------------------------------------------

        if change_type == "changed":

            details_html = f"""
            <details>

                <summary>
                    View old → new
                </summary>

                <div class="comparison">

                    <div class="old-box">

                        <div class="change-label">
                            OLD
                        </div>

                        <div class="change-value">
                            {old_value}
                        </div>

                    </div>

                    <div class="arrow">
                        →
                    </div>

                    <div class="new-box">

                        <div class="change-label">
                            NEW
                        </div>

                        <div class="change-value">
                            {new_value}
                        </div>

                    </div>

                </div>

            </details>
            """

        elif change_type == "new":

            details_html = """
            <div class="simple-detail">
                New page discovered.
            </div>
            """

        elif change_type == "removed":

            details_html = """
            <div class="simple-detail removed-text">
                Page returned 404/410.
            </div>
            """

        else:

            details_html = """
            <div class="simple-detail failed-text">
                Temporary crawl problem.
                Not counted as removed.
            </div>
            """

        rows.append(
            f"""
            <tr
                data-type="{change_type}"
                data-field="{field.lower()}"
                data-priority="{priority}"
            >

                <td>
                    {type_badge}
                </td>

                <td>
                    {priority_badge}
                </td>

                <td>
                    <a
                        href="{url}"
                        target="_blank"
                        rel="noopener"
                    >
                        {url}
                    </a>
                </td>

                <td>
                    <b>{field}</b>
                </td>

                <td>
                    {details}
                    {details_html}
                </td>

            </tr>
            """
        )

    if not rows:

        rows.append(
            """
            <tr id="empty-row">

                <td colspan="5">

                    <div class="empty">
                        No changes detected.
                    </div>

                </td>

            </tr>
            """
        )

    # ========================================================
    # NOTICE
    # ========================================================

    notice = ""

    if first_run:

        notice = f"""
        <div class="notice">

            <div class="notice-title">
                Baseline created
            </div>

            <div>
                {len(pages)} pages are now being used
                as the baseline for future comparisons.
            </div>

        </div>
        """

    # ========================================================
    # HISTORY ROWS
    # ========================================================

    history_rows = []

    for item in reversed(history[-30:]):

        history_rows.append(
            f"""
            <tr>

                <td>
                    {escape(str(item.get("date", "")))}
                </td>

                <td>
                    {item.get("pages", 0)}
                </td>

                <td>
                    {item.get("new", 0)}
                </td>

                <td>
                    {item.get("changed", 0)}
                </td>

                <td>
                    {item.get("removed", 0)}
                </td>

                <td>
                    {item.get("failed", 0)}
                </td>

                <td>
                    {item.get("duration", 0)} sec
                </td>

            </tr>
            """
        )

    # ========================================================
    # DASHBOARD
    # ========================================================

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
    box-sizing:
        border-box;
}}

body {{

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    margin:
        0;

    background:
        #f4f6f8;

    color:
        #202124;
}}

.container {{

    max-width:
        1500px;

    margin:
        auto;

    padding:
        30px;
}}

h1 {{

    margin:
        0 0 6px 0;

    font-size:
        34px;
}}

.subtitle {{

    color:
        #6b7280;

    margin-bottom:
        25px;

    font-size:
        15px;
}}

.notice {{

    background:
        #e8f4ff;

    border:
        1px solid #b9ddff;

    padding:
        18px;

    margin-bottom:
        25px;

    border-radius:
        10px;
}}

.notice-title {{

    font-weight:
        bold;

    font-size:
        18px;

    margin-bottom:
        5px;
}}

.cards {{

    display:
        grid;

    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));

    gap:
        15px;

    margin-bottom:
        20px;
}}

.card {{

    background:
        white;

    padding:
        20px;

    border-radius:
        12px;

    box-shadow:
        0 2px 10px rgba(0,0,0,.07);
}}

.card-title {{

    color:
        #6b7280;

    font-size:
        14px;
}}

.number {{

    font-size:
        32px;

    font-weight:
        bold;

    margin-top:
        8px;
}}

.health {{

    background:
        white;

    padding:
        20px;

    border-radius:
        12px;

    box-shadow:
        0 2px 10px rgba(0,0,0,.07);

    margin-bottom:
        20px;
}}

.health-grid {{

    display:
        grid;

    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));

    gap:
        15px;
}}

.health-item {{

    padding:
        10px 0;
}}

.health-label {{

    color:
        #777;

    font-size:
        13px;
}}

.health-value {{

    font-weight:
        bold;

    margin-top:
        5px;
}}

.filters {{

    background:
        white;

    padding:
        20px;

    border-radius:
        12px;

    box-shadow:
        0 2px 10px rgba(0,0,0,.07);

    margin-bottom:
        20px;
}}

.filter-row {{

    display:
        flex;

    flex-wrap:
        wrap;

    gap:
        10px;

    align-items:
        center;
}}

.search {{

    flex:
        1;

    min-width:
        260px;

    padding:
        11px 14px;

    border:
        1px solid #d1d5db;

    border-radius:
        8px;

    font-size:
        14px;
}}

select {{

    padding:
        11px 14px;

    border:
        1px solid #d1d5db;

    border-radius:
        8px;

    background:
        white;

    font-size:
        14px;
}}

.section {{

    margin-top:
        35px;

    margin-bottom:
        12px;

    font-size:
        22px;

    font-weight:
        bold;
}}

.table-wrap {{

    background:
        white;

    border-radius:
        12px;

    overflow-x:
        auto;

    box-shadow:
        0 2px 10px rgba(0,0,0,.07);
}}

table {{

    width:
        100%;

    border-collapse:
        collapse;

    min-width:
        1100px;
}}

th,
td {{

    padding:
        15px;

    text-align:
        left;

    border-bottom:
        1px solid #edf0f2;

    vertical-align:
        top;
}}

th {{

    background:
        #fafafa;

    font-size:
        13px;

    color:
        #555;
}}

td a {{

    color:
        #0969da;

    text-decoration:
        none;

    word-break:
        break-all;
}}

td a:hover {{

    text-decoration:
        underline;
}}

.badge,
.priority {{

    display:
        inline-block;

    padding:
        5px 9px;

    border-radius:
        20px;

    font-size:
        11px;

    font-weight:
        bold;
}}

.badge-new {{

    background:
        #dcfce7;

    color:
        #166534;
}}

.badge-changed {{

    background:
        #fff3cd;

    color:
        #92400e;
}}

.badge-removed {{

    background:
        #fee2e2;

    color:
        #991b1b;
}}

.badge-failed {{

    background:
        #e5e7eb;

    color:
        #374151;
}}

.priority.high {{

    background:
        #fee2e2;

    color:
        #991b1b;
}}

.priority.medium {{

    background:
        #fff3cd;

    color:
        #92400e;
}}

.priority.low {{

    background:
        #e5e7eb;

    color:
        #374151;
}}

details {{

    margin-top:
        10px;
}}

summary {{

    cursor:
        pointer;

    color:
        #0969da;

    font-weight:
        600;
}}

.comparison {{

    display:
        grid;

    grid-template-columns:
        1fr 40px 1fr;

    gap:
        10px;

    margin-top:
        12px;

    align-items:
        stretch;
}}

.old-box,
.new-box {{

    padding:
        12px;

    border-radius:
        8px;

    word-break:
        break-word;

    white-space:
        pre-wrap;

    font-size:
        13px;

    max-height:
        350px;

    overflow:
        auto;
}}

.old-box {{

    background:
        #fff1f2;

    border:
        1px solid #fecdd3;
}}

.new-box {{

    background:
        #f0fdf4;

    border:
        1px solid #bbf7d0;
}}

.change-label {{

    font-size:
        11px;

    font-weight:
        bold;

    margin-bottom:
        7px;
}}

.change-value {{

    line-height:
        1.5;
}}

.arrow {{

    display:
        flex;

    align-items:
        center;

    justify-content:
        center;

    font-size:
        24px;

    color:
        #777;
}}

.simple-detail {{

    margin-top:
        8px;

    color:
        #555;
}}

.removed-text {{

    color:
        #991b1b;
}}

.failed-text {{

    color:
        #92400e;
}}

.empty {{

    text-align:
        center;

    padding:
        50px;

    color:
        #6b7280;

    font-size:
        16px;
}}

.footer {{

    margin-top:
        20px;

    color:
        #777;

    font-size:
        13px;
}}

@media (max-width: 700px) {{

    .container {{
        padding:
            15px;
    }}

    h1 {{
        font-size:
            27px;
    }}

    .comparison {{
        grid-template-columns:
            1fr;
    }}

    .arrow {{
        display:
            none;
    }}

}}

</style>

</head>

<body>

<div class="container">

    <h1>
        Website Dashboard
    </h1>

    <div class="subtitle">
        Page and content overview
        · Last checked: {now}
    </div>

    {notice}


    <!-- ================================================= -->
    <!-- OVERVIEW -->
    <!-- ================================================= -->

    <div class="cards">

        <div class="card">

            <div class="card-title">
                Pages
            </div>

            <div class="number">
                {len(pages)}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                New Pages
            </div>

            <div class="number">
                {new_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Changed
            </div>

            <div class="number">
                {changed_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Removed
            </div>

            <div class="number">
                {removed_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                High Priority
            </div>

            <div class="number">
                {high_priority_count}
            </div>

        </div>

    </div>


    <!-- ================================================= -->
    <!-- SEO CHANGES -->
    <!-- ================================================= -->

    <div class="cards">

        <div class="card">

            <div class="card-title">
                Title Changes
            </div>

            <div class="number">
                {title_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                H1 Changes
            </div>

            <div class="number">
                {h1_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Description Changes
            </div>

            <div class="number">
                {description_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Canonical Changes
            </div>

            <div class="number">
                {canonical_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Robots Changes
            </div>

            <div class="number">
                {robots_count}
            </div>

        </div>

        <div class="card">

            <div class="card-title">
                Content Changes
            </div>

            <div class="number">
                {content_count}
            </div>

        </div>

    </div>


    <!-- ================================================= -->
    <!-- CRAWL HEALTH -->
    <!-- ================================================= -->

    <div class="health">

        <div class="health-grid">

            <div class="health-item">

                <div class="health-label">
                    Crawl Status
                </div>

                <div class="health-value">
                    ✓ Successful
                </div>

            </div>

            <div class="health-item">

                <div class="health-label">
                    Pages Scanned
                </div>

                <div class="health-value">
                    {len(pages)}
                </div>

            </div>

            <div class="health-item">

                <div class="health-label">
                    Failed
                </div>

                <div class="health-value">
                    {failed_count}
                </div>

            </div>

            <div class="health-item">

                <div class="health-label">
                    Crawl Duration
                </div>

                <div class="health-value">
                    {duration} seconds
                </div>

            </div>

        </div>

    </div>


    <!-- ================================================= -->
    <!-- FILTERS -->
    <!-- ================================================= -->

    <div class="filters">

        <div class="filter-row">

            <input
                id="search"
                class="search"
                type="text"
                placeholder="Search URL..."
                onkeyup="filterRows()"
            >

            <select
                id="typeFilter"
                onchange="filterRows()"
            >

                <option value="all">
                    All Types
                </option>

                <option value="new">
                    New
                </option>

                <option value="changed">
                    Changed
                </option>

                <option value="removed">
                    Removed
                </option>

                <option value="failed">
                    Failed
                </option>

            </select>

            <select
                id="fieldFilter"
                onchange="filterRows()"
            >

                <option value="all">
                    All Elements
                </option>

                <option value="title">
                    Title
                </option>

                <option value="h1">
                    H1
                </option>

                <option value="description">
                    Description
                </option>

                <option value="canonical">
                    Canonical
                </option>

                <option value="robots">
                    Robots
                </option>

                <option value="content">
                    Content
                </option>

                <option value="crawl">
                    Crawl
                </option>

            </select>

            <select
                id="priorityFilter"
                onchange="filterRows()"
            >

                <option value="all">
                    All Priority
                </option>

                <option value="high">
                    High
                </option>

                <option value="medium">
                    Medium
                </option>

                <option value="low">
                    Low
                </option>

            </select>

        </div>

    </div>


    <!-- ================================================= -->
    <!-- CURRENT CHANGES -->
    <!-- ================================================= -->

    <div class="section">
        Current Changes
    </div>

    <div class="table-wrap">

        <table>

            <thead>

                <tr>

                    <th>
                        Type
                    </th>

                    <th>
                        Priority
                    </th>

                    <th>
                        Page
                    </th>

                    <th>
                        Element
                    </th>

                    <th>
                        Details
                    </th>

                </tr>

            </thead>

            <tbody id="changeTable">

                {"".join(rows)}

            </tbody>

        </table>

    </div>


    <!-- ================================================= -->
    <!-- HISTORY -->
    <!-- ================================================= -->

    <div class="section">
        Change History
    </div>

    <div class="table-wrap">

        <table>

            <thead>

                <tr>

                    <th>
                        Date
                    </th>

                    <th>
                        Pages
                    </th>

                    <th>
                        New
                    </th>

                    <th>
                        Changed
                    </th>

                    <th>
                        Removed
                    </th>

                    <th>
                        Failed
                    </th>

                    <th>
                        Duration
                    </th>

                </tr>

            </thead>

            <tbody>

                {"".join(history_rows)}

            </tbody>

        </table>

    </div>


    <div class="footer">

        Pages scanned:
        {len(pages)}

        ·

        Total detected changes:
        {len(changes)}

        ·

        History records:
        {len(history)}

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
            "#changeTable tr"
        );

    rows.forEach(
        function(row) {{

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

            const show =
                matchesSearch
                && matchesType
                && matchesField
                && matchesPriority;

            row.style.display =
                show ? "" : "none";

        }}
    );

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

    # ========================================================
    # IMPORTANT:
    # Existing snapshot = existing baseline.
    # DO NOT RESET IT.
    # ========================================================

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

    # ========================================================
    # CRAWL
    # ========================================================

    crawl_result = crawl(
        old_pages
    )

    pages = crawl_result["pages"]

    failed = crawl_result["failed"]

    removed = crawl_result["removed"]

    duration = crawl_result["duration"]

    # ========================================================
    # COMPARE
    # ========================================================

    changes = compare(
        old_pages,
        pages,
        failed,
        removed
    )

    # ========================================================
    # SAVE SNAPSHOT
    # ========================================================

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

    # ========================================================
    # SAVE HISTORY
    # ========================================================

    history = save_history(
        pages=pages,
        changes=changes,
        duration=duration,
        failed_count=len(failed),
        removed_count=len(removed),
    )

    # ========================================================
    # DASHBOARD
    # ========================================================

    make_dashboard(
        pages=pages,
        changes=changes,
        history=history,
        duration=duration,
        failed_count=len(failed),
        first_run=first_run,
    )

    # ========================================================
    # LOGS
    # ========================================================

    print(
        f"[RESULT] {len(changes)} changes",
        flush=True
    )

    print(
        f"[PAGES] {len(pages)} pages",
        flush=True
    )

    print(
        f"[FAILED] {len(failed)} pages",
        flush=True
    )

    print(
        f"[COMPLETE] Duration: {duration}s",
        flush=True
    )


if __name__ == "__main__":
    main()
