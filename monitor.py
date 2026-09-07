import json
import re
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

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type", ""
        ).lower()

        if "text/html" not in content_type:
            return None

        return response.text

    except requests.RequestException:
        return None


# ============================================================
# EXTRACT
# ============================================================

def extract(url, html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(
        ["script", "style", "noscript", "svg"]
    ):
        tag.decompose()

    title = ""

    if soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

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

    h1 = [
        x.get_text(" ", strip=True)
        for x in soup.find_all("h1")
    ]

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

            soup = BeautifulSoup(
                response.text,
                "xml"
            )

            locations = soup.find_all("loc")

            for loc in locations:

                value = loc.get_text(
                    strip=True
                )

                # Sitemap index
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
                return discovered

        except requests.RequestException:
            continue

    return discovered


# ============================================================
# CRAWL
# ============================================================

def crawl():

    urls = get_sitemap_urls()

    homepage = clean_url(BASE_URL)

    if homepage:
        urls.add(homepage)

    urls = sorted(urls)[:MAX_PAGES]

    print(
        f"[START] {len(urls)} pages",
        flush=True
    )

    pages = {}

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
                html = future.result()

                if html:
                    pages[url] = extract(
                        url,
                        html
                    )

            except Exception:
                pass

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

    print(
        f"[DONE] {len(pages)} pages collected",
        flush=True
    )

    return pages


# ============================================================
# COMPARE
# ============================================================

def compare(old, new):

    changes = []

    old_urls = set(old)
    new_urls = set(new)

    for url in sorted(new_urls - old_urls):

        changes.append({
            "type": "new",
            "url": url,
            "details": "New page",
        })

    for url in sorted(old_urls - new_urls):

        changes.append({
            "type": "removed",
            "url": url,
            "details": "Page removed",
        })

    fields = [
        ("title", "Title"),
        ("description", "Description"),
        ("canonical", "Canonical"),
        ("h1", "H1"),
        ("robots", "Robots"),
        ("content", "Content"),
    ]

    for url in sorted(old_urls & new_urls):

        before = old[url]
        after = new[url]

        for field, label in fields:

            if before.get(field) != after.get(field):

                changes.append({
                    "type": "changed",
                    "url": url,
                    "field": label,
                    "old": before.get(field, ""),
                    "new": after.get(field, ""),
                    "details": f"{label} changed",
                })

    return changes


# ============================================================
# DASHBOARD
# ============================================================

def make_dashboard(
    pages,
    changes,
    first_run=False
):

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

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

    rows = []

    for change in changes:

        old = escape(
            str(change.get("old", ""))
        )

        new = escape(
            str(change.get("new", ""))
        )

        rows.append(
            f"""
            <tr>
                <td>{escape(change["type"])}</td>

                <td>
                    <a
                        href="{escape(change["url"])}"
                        target="_blank"
                    >
                        {escape(change["url"])}
                    </a>
                </td>

                <td>
                    {escape(change.get("field", ""))}
                </td>

                <td>
                    <div><b>Old:</b> {old}</div>
                    <div><b>New:</b> {new}</div>
                </td>
            </tr>
            """
        )

    if not rows:

        rows.append(
            """
            <tr>
                <td colspan="4">
                    No changes detected.
                </td>
            </tr>
            """
        )

    notice = ""

    if first_run:

        notice = f"""
        <div class="notice">
            Baseline created for {len(pages)} pages.
            Future runs will compare against this baseline.
        </div>
        """

    html = f"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Website Dashboard</title>

<style>

body {{
    font-family: Arial, sans-serif;
    margin: 0;
    background: #f5f7fa;
    color: #222;
}}

.container {{
    max-width: 1300px;
    margin: auto;
    padding: 30px;
}}

h1 {{
    margin-bottom: 5px;
}}

.subtitle {{
    color: #666;
    margin-bottom: 25px;
}}

.cards {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 15px;
    margin-bottom: 25px;
}}

.card {{
    background: white;
    padding: 20px;
    border-radius: 10px;
    box-shadow:
        0 2px 8px rgba(0,0,0,.08);
}}

.number {{
    font-size: 30px;
    font-weight: bold;
    margin-top: 8px;
}}

.notice {{
    background: #e8f4ff;
    padding: 15px;
    margin-bottom: 25px;
    border-radius: 5px;
}}

.table-wrap {{
    background: white;
    border-radius: 10px;
    overflow-x: auto;
    box-shadow:
        0 2px 8px rgba(0,0,0,.08);
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th, td {{
    padding: 14px;
    text-align: left;
    border-bottom: 1px solid #eee;
    vertical-align: top;
}}

th {{
    background: #fafafa;
}}

a {{
    color: #0969da;
    text-decoration: none;
}}

a:hover {{
    text-decoration: underline;
}}

.footer {{
    margin-top: 20px;
    color: #777;
    font-size: 13px;
}}

</style>

</head>

<body>

<div class="container">

<h1>Website Dashboard</h1>

<div class="subtitle">
Page and content overview
</div>

{notice}

<div class="cards">

<div class="card">
Pages
<div class="number">
{len(pages)}
</div>
</div>

<div class="card">
New
<div class="number">
{new_count}
</div>
</div>

<div class="card">
Changed
<div class="number">
{changed_count}
</div>
</div>

<div class="card">
Removed
<div class="number">
{removed_count}
</div>
</div>

</div>

<div class="table-wrap">

<table>

<thead>

<tr>
<th>Type</th>
<th>Page</th>
<th>Element</th>
<th>Details</th>
</tr>

</thead>

<tbody>

{"".join(rows)}

</tbody>

</table>

</div>

<div class="footer">
Last checked: {now}
</div>

</div>

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

    pages = crawl()

    changes = compare(
        old_pages,
        pages
    )

    snapshot = {
        "site": BASE_URL,
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "pages": pages,
        "changes": changes,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(
            snapshot,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    make_dashboard(
        pages,
        changes,
        first_run=first_run
    )

    print(
        f"[RESULT] {len(changes)} changes",
        flush=True
    )

    print(
        "[COMPLETE]",
        flush=True
    )


if __name__ == "__main__":
    main()
