import hashlib
import json
import re
import time
import difflib
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.excelr.com"
MAX_PAGES = 5000
MAX_WORKERS = 8
TIMEOUT = 20
MAX_DASHBOARD_ROWS = 1000
MAX_DIFF_CHARS = 4000
VISUAL_WORKERS = 4
VISUAL_TIMEOUT_MS = 30000
VISUAL_VIEWPORT = {"width": 1440, "height": 900}

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"
VISUAL_FILE = DATA_DIR / "visual.json"
DIFF_FILE = DATA_DIR / "latest_diffs.json"
CONTENT_FILE = DATA_DIR / "content.json.gz"
DASHBOARD_FILE = Path("dashboard.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WebsiteAudit/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FIELDS = [
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


def clean_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None
        if p.netloc.lower() != urlparse(BASE_URL).netloc.lower():
            return None
        path = p.path or "/"
        bad = (
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".pdf", ".zip", ".mp4", ".mp3", ".webm", ".css", ".js",
            ".xml", ".json", ".woff", ".woff2", ".ttf", ".eot"
        )
        if path.lower().endswith(bad):
            return None
        out = f"{p.scheme}://{p.netloc}{path}"
        if path != "/" and out.endswith("/"):
            out = out[:-1]
        return out
    except Exception:
        return None


def normalize_text(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()


def stable_hash(value):
    if not isinstance(value, str):
        value = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":")
        )
    return hashlib.sha256(
        value.encode("utf-8", errors="ignore")
    ).hexdigest()


def fetch(url):
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True
        )
        ct = r.headers.get("content-type", "").lower()

        if r.status_code == 200 and "text/html" in ct:
            return {
                "status": "success",
                "html": r.text,
                "http_status": 200,
                "final_url": clean_url(r.url) or r.url
            }

        if r.status_code in (404, 410):
            return {
                "status": "removed",
                "html": "",
                "http_status": r.status_code,
                "final_url": clean_url(r.url) or r.url
            }

        return {
            "status": "failed",
            "html": "",
            "http_status": r.status_code,
            "final_url": clean_url(r.url) or r.url
        }

    except requests.RequestException as e:
        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "error": str(e)
        }


def extract(url, html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    tag = soup.find(
        "meta",
        attrs={"name": re.compile("^description$", re.I)}
    )
    description = (tag.get("content", "") if tag else "").strip()

    tag = soup.find(
        "link",
        attrs={"rel": lambda v: v and "canonical" in v}
    )
    canonical = (tag.get("href", "") if tag else "").strip()

    h1 = " | ".join(
        x.get_text(" ", strip=True)
        for x in soup.find_all("h1")
    )

    tag = soup.find(
        "meta",
        attrs={"name": re.compile("^robots$", re.I)}
    )
    robots = (tag.get("content", "") if tag else "").strip()

    images = []
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        ).strip()

        if src:
            images.append({
                "src": src,
                "alt": (img.get("alt") or "").strip()
            })

    images.sort(key=lambda x: (x["src"], x["alt"]))

    links = set()
    for a in soup.find_all("a", href=True):
        u = clean_url(urljoin(url, a.get("href", "").strip()))
        if u:
            links.add(u)

    links = sorted(links)

    schemas = []
    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)}
    ):
        raw = (script.string or script.get_text() or "").strip()

        if raw:
            try:
                schemas.append(json.loads(raw))
            except Exception:
                schemas.append(raw)

    text_soup = BeautifulSoup(html, "html.parser")

    for t in text_soup(["script", "style", "noscript", "svg"]):
        t.decompose()

    main = text_soup.find("main")

    content = (
        main.get_text(" ", strip=True)
        if main
        else text_soup.get_text(" ", strip=True)
    )

    content = normalize_text(content)

    values = {
        "title": normalize_text(title),
        "description": normalize_text(description),
        "canonical": normalize_text(canonical),
        "h1": normalize_text(h1),
        "robots": normalize_text(robots),
        "content": content,
        "images": images,
        "internal_links": links,
        "schema": schemas,
    }

    page = {
        "url": url,
        "fingerprints": {
            k: stable_hash(v)
            for k, v in values.items()
        },
        "content_length": len(content),
        "values": {
            "title": values["title"],
            "description": values["description"],
            "canonical": values["canonical"],
            "h1": values["h1"],
            "robots": values["robots"],
        },
        "content_preview": values["content"][:2000],
    }

    return page, links, values


def get_sitemap_urls():
    found = set()

    for sitemap_url in (
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml"
    ):
        print(f"[SITEMAP] Checking {sitemap_url}", flush=True)

        try:
            r = requests.get(
                sitemap_url,
                headers=HEADERS,
                timeout=TIMEOUT
            )

            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "xml")
            locs = [
                x.get_text(strip=True)
                for x in soup.find_all("loc")
            ]

            for loc in locs:
                if loc.lower().endswith(".xml"):
                    try:
                        child = requests.get(
                            loc,
                            headers=HEADERS,
                            timeout=TIMEOUT
                        )

                        if child.status_code != 200:
                            continue

                        child_soup = BeautifulSoup(
                            child.text,
                            "xml"
                        )

                        for item in [
                            x.get_text(strip=True)
                            for x in child_soup.find_all("loc")
                        ]:
                            u = clean_url(item)
                            if u:
                                found.add(u)

                    except requests.RequestException:
                        pass

                else:
                    u = clean_url(loc)
                    if u:
                        found.add(u)

            if found:
                print(
                    f"[SITEMAP] {len(found)} HTML URLs found",
                    flush=True
                )
                return found

        except requests.RequestException as e:
            print(f"[SITEMAP ERROR] {e}", flush=True)

    return found


def load_snapshot():
    if not SNAPSHOT_FILE.exists():
        return {}

    try:
        raw = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )

        pages = (
            raw.get("pages", {})
            if isinstance(raw, dict)
            else {}
        )

        out = {}

        for url, data in pages.items():
            if not isinstance(data, dict):
                continue

            if isinstance(data.get("fingerprints"), dict):
                out[url] = data
            else:
                vals = {
                    k: data.get(k)
                    for k, _, _ in FIELDS
                    if k in data
                }

                out[url] = {
                    "url": url,
                    "fingerprints": {
                        k: stable_hash(v)
                        for k, v in vals.items()
                    },
                    "content_length": data.get(
                        "content_length",
                        len(data.get("content", "") or "")
                    ),
                    "values": {
                        k: data.get(k, "")
                        for k in (
                            "title",
                            "description",
                            "canonical",
                            "h1",
                            "robots"
                        )
                    },
                    "content_preview": (
                        data.get("content", "") or ""
                    )[:2000],
                }

        return out

    except Exception as e:
        print(
            f"[WARNING] Snapshot read failed: {e}",
            flush=True
        )
        return {}


def crawl(old_pages):
    started = time.time()

    sitemap = get_sitemap_urls()

    home = clean_url(BASE_URL)
    if home:
        sitemap.add(home)

    urls = set(sitemap)
    urls.update(old_pages.keys())

    urls = sorted(u for u in urls if u)[:MAX_PAGES]

    print(f"[START] {len(urls)} pages", flush=True)

    pages = {}
    values_by_url = {}
    failed = {}
    removed = set()
    discovered = set()

    def run_batch(batch, label):
        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as ex:

            futures = {
                ex.submit(fetch, u): u
                for u in batch
            }

            done = 0

            for future in as_completed(futures):
                url = futures[future]

                try:
                    result = future.result()

                    if result["status"] == "success":
                        page, links, values = extract(
                            url,
                            result["html"]
                        )

                        pages[url] = page
                        values_by_url[url] = values
                        discovered.update(links)

                    elif result["status"] == "removed":
                        removed.add(url)

                    else:
                        failed[url] = {
                            "http_status": result.get(
                                "http_status"
                            )
                        }

                except Exception as e:
                    failed[url] = {
                        "http_status": None,
                        "error": str(e)
                    }

                done += 1

                if done % 25 == 0 or done == len(batch):
                    print(
                        f"[{label}] {done}/{len(batch)}",
                        flush=True
                    )

    run_batch(urls, "PROGRESS")

    additional = (
        discovered
        - set(pages)
        - set(failed)
        - removed
    )

    additional = sorted(additional)[
        :max(0, MAX_PAGES - len(pages))
    ]

    print(
        f"[DISCOVERY] {len(additional)} additional "
        f"internal-link pages",
        flush=True
    )

    if additional:
        run_batch(
            additional,
            "DISCOVERY PROGRESS"
        )

    return {
        "pages": pages,
        "values": values_by_url,
        "failed": failed,
        "removed": removed,
        "duration": round(
            time.time() - started,
            2
        ),
    }


def load_content_store():
    if not CONTENT_FILE.exists():
        return {}

    try:
        with gzip.open(
            CONTENT_FILE,
            "rt",
            encoding="utf-8"
        ) as f:
            x = json.load(f)

        return x if isinstance(x, dict) else {}

    except Exception as e:
        print(
            f"[WARNING] Content store read failed: {e}",
            flush=True
        )
        return {}


def save_content_store(
    pages,
    values_by_url,
    old_content
):
    store = dict(old_content)

    for url, vals in values_by_url.items():
        store[url] = vals.get("content", "")

    store = {
        u: store.get(u, "")
        for u in pages
    }

    with gzip.open(
        CONTENT_FILE,
        "wt",
        encoding="utf-8"
    ) as f:
        json.dump(
            store,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )


def make_diff(
    before,
    after,
    old_content="",
    new_content=""
):
    out = []

    for key in (
        "title",
        "description",
        "canonical",
        "h1",
        "robots"
    ):
        b = (
            before.get("values", {}) or {}
        ).get(key, "")

        a = (
            after.get("values", {}) or {}
        ).get(key, "")

        if b != a:
            out.append({
                "field": key.title(),
                "before": b,
                "after": a
            })

    bprev = (
        old_content
        or before.get("content_preview", "")
    )

    aprev = (
        new_content
        or after.get("content_preview", "")
    )

    if (
        before.get("fingerprints", {}).get("content")
        != after.get("fingerprints", {}).get("content")
    ):
        diff = list(
            difflib.unified_diff(
                bprev.split(),
                aprev.split(),
                fromfile="Before",
                tofile="After",
                lineterm=""
            )
        )

        text = "\n".join(diff)

        if len(text) > MAX_DIFF_CHARS:
            text = (
                text[:MAX_DIFF_CHARS]
                + "\n...[diff truncated]"
            )

        out.append({
            "field": "Content",
            "before": bprev,
            "after": aprev,
            "diff": text
        })

    return out


def compare(
    old,
    new,
    failed,
    removed,
    old_content,
    new_content
):
    changes = []
    diffs = {}

    old_urls = set(old)
    new_urls = set(new)

    for url in sorted(new_urls - old_urls):
        changes.append({
            "type": "new",
            "url": url,
            "field": "Page",
            "details": "New page discovered",
            "priority": "medium"
        })

    for url in sorted(
        (old_urls - new_urls) & removed
    ):
        changes.append({
            "type": "removed",
            "url": url,
            "field": "Page",
            "details": "Page returned 404/410",
            "priority": "high"
        })

    for url in sorted(failed):
        changes.append({
            "type": "failed",
            "url": url,
            "field": "Crawl",
            "details": "Temporary crawl failure",
            "priority": "medium"
        })

    for url in sorted(old_urls & new_urls):
        before = old[url]
        after = new[url]

        for key, label, priority in FIELDS:
            if (
                key in before.get("fingerprints", {})
                and before["fingerprints"].get(key)
                != after.get("fingerprints", {}).get(key)
            ):
                changes.append({
                    "type": "changed",
                    "url": url,
                    "field": label,
                    "details": f"{label} changed",
                    "priority": priority
                })

        if (
            before.get("fingerprints", {}).get("content")
            != after.get("fingerprints", {}).get("content")
        ):
            diffs[url] = make_diff(
                before,
                after,
                old_content.get(url, ""),
                new_content.get(url, "")
            )

    return changes, diffs


def visual_hash_png(png_bytes):
    return hashlib.sha256(png_bytes).hexdigest()


def load_visual():
    if not VISUAL_FILE.exists():
        return {}

    try:
        x = json.loads(
            VISUAL_FILE.read_text(
                encoding="utf-8"
            )
        )

        return (
            x.get("pages", {})
            if isinstance(x, dict)
            else {}
        )

    except Exception:
        return {}


def run_visual_checks(urls, old_visual):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(
            f"[VISUAL] Playwright unavailable: {e}",
            flush=True
        )
        return {}, {}, {}

    targets = sorted(set(urls))

    if not targets:
        return {}, {}, {}

    current = {}
    visual_changes = {}
    errors = {}

    chunk_count = min(
        VISUAL_WORKERS,
        len(targets)
    )

    chunks = [[] for _ in range(chunk_count)]

    for i, url in enumerate(targets):
        chunks[i % chunk_count].append(url)

    def worker(batch):
        local_current = {}
        local_changes = {}
        local_errors = {}

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True
                )

                context = browser.new_context(
                    viewport=VISUAL_VIEWPORT,
                    device_scale_factor=1
                )

                page = context.new_page()

                for url in batch:
                    try:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=VISUAL_TIMEOUT_MS
                        )

                        page.wait_for_timeout(1200)

                        png = page.screenshot(
                            full_page=False,
                            type="png"
                        )

                        h = visual_hash_png(png)

                        local_current[url] = {
                            "hash": h,
                            "checked_at": (
                                datetime.now(
                                    timezone.utc
                                ).isoformat()
                            )
                        }

                        if (
                            url in old_visual
                            and old_visual[url].get("hash")
                            != h
                        ):
                            local_changes[url] = {
                                "type": "visual",
                                "url": url,
                                "field": "Visual",
                                "details": (
                                    "Rendered page screenshot changed"
                                ),
                                "priority": "high"
                            }

                    except Exception as e:
                        local_errors[url] = str(e)

                context.close()
                browser.close()

        except Exception as e:
            for url in batch:
                local_errors[url] = str(e)

        return (
            local_current,
            local_changes,
            local_errors
        )

    with ThreadPoolExecutor(
        max_workers=chunk_count
    ) as ex:

        futures = [
            ex.submit(worker, chunk)
            for chunk in chunks
            if chunk
        ]

        for future in as_completed(futures):
            c, v, e = future.result()

            current.update(c)
            visual_changes.update(v)
            errors.update(e)

    print(
        f"[VISUAL] Checked {len(current)} pages; "
        f"{len(visual_changes)} changed; "
        f"{len(errors)} failed",
        flush=True
    )

    return (
        current,
        visual_changes,
        errors
    )


def save_snapshot(pages):
    data = {
        "version": 3,
        "site": BASE_URL,
        "checked_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "pages": pages
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )


def save_history(
    changes,
    pages,
    failed,
    removed,
    duration,
    visual_count=0
):
    history = []

    if HISTORY_FILE.exists():
        try:
            x = json.loads(
                HISTORY_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(x, list):
                history = x

        except Exception:
            pass

    history.append({
        "checked_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "pages": pages,
        "new": sum(
            c["type"] == "new"
            for c in changes
        ),
        "changed": sum(
            c["type"] in (
                "changed",
                "visual"
            )
            for c in changes
        ),
        "removed": removed,
        "failed": failed,
        "visual": visual_count,
        "duration": duration
    })

    history = history[-365:]

    HISTORY_FILE.write_text(
        json.dumps(
            history,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    return history


def make_dashboard(
    pages,
    changes,
    history,
    duration,
    failed,
    removed,
    diffs,
    visual_errors
):
    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    new_n = sum(
        c["type"] == "new"
        for c in changes
    )

    changed_n = sum(
        c["type"] in (
            "changed",
            "visual"
        )
        for c in changes
    )

    high_n = sum(
        c.get("priority") == "high"
        for c in changes
    )

    counts = {
        label: sum(
            c["type"] == "changed"
            and c["field"] == label
            for c in changes
        )
        for _, label, _ in FIELDS
    }

    counts["Visual"] = sum(
        c["type"] == "visual"
        for c in changes
    )

    rows = []

    for c in changes[:MAX_DASHBOARD_ROWS]:
        url = c["url"]
        detail = c.get("details", "")

        if (
            c["type"] == "changed"
            and url in diffs
        ):
            relevant = [
                d
                for d in diffs[url]
                if d["field"] == c["field"]
            ]

            if relevant:
                d = relevant[0]

                if d.get("diff"):
                    detail += (
                        " | Diff: "
                        + d["diff"][:1200]
                    )

                else:
                    detail += (
                        f" | Before: "
                        f"{d.get('before', '')[:500]} "
                        f"| After: "
                        f"{d.get('after', '')[:500]}"
                    )

        rows.append(
            "<tr>"
            f"<td><b>{escape(c['type'].upper())}</b></td>"
            f"<td>{escape(c.get('priority', '').upper())}</td>"
            f"<td><a target='_blank' rel='noopener' "
            f"href='{escape(url)}'>"
            f"{escape(url)}</a></td>"
            f"<td>{escape(c.get('field', ''))}</td>"
            f"<td><pre>{escape(detail)}</pre></td>"
            "</tr>"
        )

    if not rows:
        rows = [
            "<tr><td colspan='5'>"
            "No changes detected."
            "</td></tr>"
        ]

    history_rows = []

    for h in reversed(history):
        history_rows.append(
            "<tr>"
            f"<td>{escape(str(h.get('checked_at', '')))}</td>"
            f"<td>{h.get('pages', 0)}</td>"
            f"<td>{h.get('new', 0)}</td>"
            f"<td>{h.get('changed', 0)}</td>"
            f"<td>{h.get('removed', 0)}</td>"
            f"<td>{h.get('failed', 0)}</td>"
            f"<td>{h.get('visual', 0)}</td>"
            f"<td>{h.get('duration', 0)}s</td>"
            "</tr>"
        )

    def card(name, value):
        return (
            "<div class='card'>"
            f"<div>{escape(name)}</div>"
            f"<strong>{value}</strong>"
            "</div>"
        )

    overview = "".join([
        card("Pages", len(pages)),
        card("New Pages", new_n),
        card("Changed", changed_n),
        card("Removed", removed),
        card("Failed", failed),
        card("High Priority", high_n)
    ])

    seo = "".join(
        card(f"{k} Changes", v)
        for k, v in counts.items()
    )

    html = f"""<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport'
content='width=device-width,initial-scale=1'>
<title>Website Dashboard</title>
<style>
body{{font-family:Arial,sans-serif;margin:0;
background:#f4f6f8;color:#202124}}
.container{{max-width:1500px;margin:auto;padding:28px}}
h1{{margin-bottom:5px}}
.sub{{color:#667085;margin-bottom:24px}}
.cards{{display:grid;
grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
gap:14px;margin-bottom:18px}}
.card,.section,.health{{background:#fff;padding:18px;
border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07)}}
.card div{{color:#667085}}
.card strong{{font-size:30px;display:block;margin-top:8px}}
.section{{margin-top:20px}}
.wrap{{overflow:auto}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:11px;border-bottom:1px solid #e5e7eb;
text-align:left;vertical-align:top}}
th{{background:#f9fafb}}
a{{color:#0969da;word-break:break-all}}
input,select{{padding:10px;border:1px solid #d0d5dd;
border-radius:8px}}
#search{{width:50%;min-width:250px}}
pre{{white-space:pre-wrap;max-width:700px;
font-family:Arial,sans-serif}}
</style>
</head>
<body>
<div class='container'>
<h1>Website Dashboard</h1>
<div class='sub'>
Page and content overview · Last checked: {now}
</div>

<div class='cards'>{overview}</div>
<div class='cards'>{seo}</div>

<div class='health'>
Pages scanned: <b>{len(pages)}</b> ·
Failed: <b>{failed}</b> ·
Removed: <b>{removed}</b> ·
Visual errors: <b>{len(visual_errors)}</b> ·
Duration: <b>{duration}s</b>
</div>

<div class='section'>
<h2>Current Changes</h2>
<p>
Showing {min(len(changes), MAX_DASHBOARD_ROWS)}
of {len(changes)} change records.
</p>

<input id='search'
placeholder='Search URL, field or details'
oninput='filterRows()'>

<select id='type'
onchange='filterRows()'>
<option value=''>All types</option>
<option>new</option>
<option>changed</option>
<option>visual</option>
<option>removed</option>
<option>failed</option>
</select>

<div class='wrap'>
<table id='changes'>
<thead>
<tr>
<th>Type</th>
<th>Priority</th>
<th>Page</th>
<th>Element</th>
<th>Details / Before → After</th>
</tr>
</thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>
</div>

<div class='section'>
<h2>History</h2>
<div class='wrap'>
<table>
<thead>
<tr>
<th>Date</th>
<th>Pages</th>
<th>New</th>
<th>Changed</th>
<th>Removed</th>
<th>Failed</th>
<th>Visual</th>
<th>Duration</th>
</tr>
</thead>
<tbody>
{''.join(history_rows)}
</tbody>
</table>
</div>
</div>

</div>

<script>
function filterRows(){{
const q=document.getElementById('search')
.value.toLowerCase();

const t=document.getElementById('type')
.value.toLowerCase();

document.querySelectorAll(
'#changes tbody tr'
).forEach(r=>{{
const txt=r.innerText.toLowerCase();
const typ=r.cells[0]?.innerText
.toLowerCase()||'';

r.style.display=(!q||txt.includes(q))
&&(!t||typ===t)?'':'none';
}});
}}
</script>

</body>
</html>"""

    DASHBOARD_FILE.write_text(
        html,
        encoding="utf-8"
    )


def main():
    DATA_DIR.mkdir(exist_ok=True)

    old = load_snapshot()

    print(
        f"[BASELINE] {len(old)} pages",
        flush=True
    )

    result = crawl(old)

    pages = result["pages"]
    failed = result["failed"]
    removed = result["removed"]
    duration = result["duration"]

    for url in failed:
        if url in old and url not in pages:
            pages[url] = old[url]

    old_content = load_content_store()

    new_content = {
        u: v.get("content", "")
        for u, v in result["values"].items()
    }

    changes, diffs = compare(
        old,
        pages,
        failed,
        removed,
        old_content,
        new_content
    )

    old_visual = load_visual()

    if not old_visual:
        visual_targets = list(pages.keys())
        print(
            "[VISUAL] First visual baseline: "
            "checking all pages",
            flush=True
        )
    else:
        visual_targets = sorted({
            c["url"]
            for c in changes
            if c["type"] == "changed"
            and c["url"] in pages
        })

    (
        current_visual,
        visual_changes,
        visual_errors
    ) = run_visual_checks(
        visual_targets,
        old_visual
    )

    for url, c in visual_changes.items():
        changes.append(c)

    all_visual = dict(old_visual)
    all_visual.update(current_visual)

    VISUAL_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "pages": all_visual
            },
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    DIFF_FILE.write_text(
        json.dumps(
            diffs,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    removed_count = sum(
        c["type"] == "removed"
        for c in changes
    )

    save_snapshot(pages)

    save_content_store(
        pages,
        result["values"],
        old_content
    )

    history = save_history(
        changes,
        len(pages),
        len(failed),
        removed_count,
        duration,
        len(visual_changes)
    )

    make_dashboard(
        pages,
        changes,
        history,
        duration,
        len(failed),
        removed_count,
        diffs,
        visual_errors
    )

    print(
        f"[NEW] "
        f"{sum(c['type'] == 'new' for c in changes)} pages",
        flush=True
    )

    print(
        f"[CHANGED] "
        f"{sum(c['type'] in ('changed','visual') for c in changes)} records",
        flush=True
    )

    print(
        f"[VISUAL] "
        f"{len(visual_changes)} visual changes",
        flush=True
    )

    print(
        f"[REMOVED] {removed_count} pages",
        flush=True
    )

    print(
        f"[FAILED] {len(failed)} pages",
        flush=True
    )

    print(
        f"[PAGES] {len(pages)} pages",
        flush=True
    )

    print(
        "[SNAPSHOT] Saved",
        flush=True
    )

    print(
        f"[COMPLETE] Duration: {duration}s",
        flush=True
    )


if __name__ == "__main__":
    main()
