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

BASE_URL = "https://www.excelr.com"
MAX_PAGES = 5000
MAX_WORKERS = 8
TIMEOUT = 20
MAX_DASHBOARD_ROWS = 1000

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"
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
        bad = (".jpg",".jpeg",".png",".gif",".webp",".svg",".ico",".pdf",".zip",
               ".mp4",".mp3",".webm",".css",".js",".xml",".json",".woff",".woff2",
               ".ttf",".eot")
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
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ct = r.headers.get("content-type", "").lower()
        if r.status_code == 200 and "text/html" in ct:
            return {"status": "success", "html": r.text, "http_status": 200}
        if r.status_code in (404, 410):
            return {"status": "removed", "html": "", "http_status": r.status_code}
        return {"status": "failed", "html": "", "http_status": r.status_code}
    except requests.RequestException as e:
        return {"status": "failed", "html": "", "http_status": None, "error": str(e)}

def extract(url, html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = (tag.get("content", "") if tag else "").strip()

    tag = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    canonical = (tag.get("href", "") if tag else "").strip()

    h1 = " | ".join(x.get_text(" ", strip=True) for x in soup.find_all("h1"))

    tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots = (tag.get("content", "") if tag else "").strip()

    images = []
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "").strip()
        if src:
            images.append({"src": src, "alt": (img.get("alt") or "").strip()})
    images.sort(key=lambda x: (x["src"], x["alt"]))

    links = set()
    for a in soup.find_all("a", href=True):
        u = clean_url(urljoin(url, a.get("href", "").strip()))
        if u:
            links.add(u)
    links = sorted(links)

    schemas = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
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
    content = main.get_text(" ", strip=True) if main else text_soup.get_text(" ", strip=True)
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
        "fingerprints": {k: stable_hash(v) for k, v in values.items()},
        "content_length": len(content),
    }
    return page, links

def get_sitemap_urls():
    found = set()
    for sitemap_url in (f"{BASE_URL}/sitemap.xml", f"{BASE_URL}/sitemap_index.xml"):
        print(f"[SITEMAP] Checking {sitemap_url}", flush=True)
        try:
            r = requests.get(sitemap_url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "xml")
            locs = [x.get_text(strip=True) for x in soup.find_all("loc")]
            for loc in locs:
                if loc.lower().endswith(".xml"):
                    try:
                        child = requests.get(loc, headers=HEADERS, timeout=TIMEOUT)
                        if child.status_code != 200:
                            continue
                        child_soup = BeautifulSoup(child.text, "xml")
                        locs2 = [x.get_text(strip=True) for x in child_soup.find_all("loc")]
                        for item in locs2:
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
                print(f"[SITEMAP] {len(found)} HTML URLs found", flush=True)
                return found
        except requests.RequestException as e:
            print(f"[SITEMAP ERROR] {e}", flush=True)
    return found

def load_snapshot():
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        raw = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        pages = raw.get("pages", {}) if isinstance(raw, dict) else {}
        out = {}
        for url, data in pages.items():
            if isinstance(data, dict) and isinstance(data.get("fingerprints"), dict):
                out[url] = data
            elif isinstance(data, dict):
                # One-time migration from the old full-content snapshot.
                vals = {k: data.get(k) for k, _, _ in FIELDS if k in data}
                out[url] = {
                    "url": url,
                    "fingerprints": {k: stable_hash(v) for k, v in vals.items()},
                    "content_length": data.get("content_length", len(data.get("content", "") or "")),
                }
        return out
    except Exception as e:
        print(f"[WARNING] Snapshot read failed: {e}", flush=True)
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

    pages, failed, removed, discovered = {}, {}, set(), set()

    def run_batch(batch, label):
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch, u): u for u in batch}
            done = 0
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                    if result["status"] == "success":
                        page, links = extract(url, result["html"])
                        pages[url] = page
                        discovered.update(links)
                    elif result["status"] == "removed":
                        removed.add(url)
                    else:
                        failed[url] = {"http_status": result.get("http_status")}
                except Exception as e:
                    failed[url] = {"http_status": None, "error": str(e)}
                done += 1
                if done % 25 == 0 or done == len(batch):
                    print(f"[{label}] {done}/{len(batch)}", flush=True)

    run_batch(urls, "PROGRESS")

    additional = discovered - set(pages) - set(failed) - removed
    additional = sorted(additional)[:max(0, MAX_PAGES - len(pages))]

    print(f"[DISCOVERY] {len(additional)} additional internal-link pages", flush=True)

    if additional:
        run_batch(additional, "DISCOVERY PROGRESS")

    return {
        "pages": pages,
        "failed": failed,
        "removed": removed,
        "duration": round(time.time() - started, 2),
    }

def compare(old, new, failed, removed):
    changes = []
    old_urls, new_urls = set(old), set(new)

    for url in sorted(new_urls - old_urls):
        changes.append({"type": "new", "url": url, "field": "Page",
                        "details": "New page discovered", "priority": "medium"})

    for url in sorted((old_urls - new_urls) & removed):
        changes.append({"type": "removed", "url": url, "field": "Page",
                        "details": "Page returned 404/410", "priority": "high"})

    for url in sorted(failed):
        changes.append({"type": "failed", "url": url, "field": "Crawl",
                        "details": "Temporary crawl failure", "priority": "medium"})

    for url in sorted(old_urls & new_urls):
        before = old[url].get("fingerprints", {})
        after = new[url].get("fingerprints", {})
        for key, label, priority in FIELDS:
            if key in before and before.get(key) != after.get(key):
                changes.append({"type": "changed", "url": url, "field": label,
                                "details": f"{label} changed", "priority": priority})
    return changes

def save_snapshot(pages):
    data = {
        "version": 2,
        "site": BASE_URL,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }
    SNAPSHOT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )

def save_history(changes, pages, failed, removed, duration):
    history = []
    if HISTORY_FILE.exists():
        try:
            x = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(x, list):
                history = x
        except Exception:
            pass

    history.append({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
        "new": sum(c["type"] == "new" for c in changes),
        "changed": sum(c["type"] == "changed" for c in changes),
        "removed": removed,
        "failed": failed,
        "duration": duration,
    })
    history = history[-365:]
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )
    return history

def make_dashboard(pages, changes, history, duration, failed, removed):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_n = sum(c["type"] == "new" for c in changes)
    changed_n = sum(c["type"] == "changed" for c in changes)
    high_n = sum(c.get("priority") == "high" for c in changes)

    counts = {
        label: sum(c["type"] == "changed" and c["field"] == label for c in changes)
        for _, label, _ in FIELDS
    }

    rows = []
    for c in changes[:MAX_DASHBOARD_ROWS]:
        rows.append(
            "<tr>"
            f"<td><b>{escape(c['type'].upper())}</b></td>"
            f"<td>{escape(c.get('priority','').upper())}</td>"
            f"<td><a target='_blank' rel='noopener' href='{escape(c['url'])}'>{escape(c['url'])}</a></td>"
            f"<td>{escape(c.get('field',''))}</td>"
            f"<td>{escape(c.get('details',''))}</td>"
            "</tr>"
        )
    if not rows:
        rows = ["<tr><td colspan='5'>No changes detected.</td></tr>"]

    history_rows = []
    for h in reversed(history):
        history_rows.append(
            "<tr>"
            f"<td>{escape(str(h.get('checked_at','')))}</td>"
            f"<td>{h.get('pages',0)}</td><td>{h.get('new',0)}</td>"
            f"<td>{h.get('changed',0)}</td><td>{h.get('removed',0)}</td>"
            f"<td>{h.get('failed',0)}</td><td>{h.get('duration',0)}s</td>"
            "</tr>"
        )

    def card(name, value):
        return f"<div class='card'><div>{escape(name)}</div><strong>{value}</strong></div>"

    overview = "".join([
        card("Pages", len(pages)),
        card("New Pages", new_n),
        card("Changed", changed_n),
        card("Removed", removed),
        card("Failed", failed),
        card("High Priority", high_n),
    ])
    seo = "".join(card(f"{k} Changes", v) for k, v in counts.items())

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Website Dashboard</title>
<style>
body{{font-family:Arial,sans-serif;margin:0;background:#f4f6f8;color:#202124}}
.container{{max-width:1500px;margin:auto;padding:28px}} h1{{margin-bottom:5px}}
.sub{{color:#667085;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:18px}}
.card,.section,.health{{background:#fff;padding:18px;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.07)}}
.card div{{color:#667085}} .card strong{{font-size:30px;display:block;margin-top:8px}}
.section{{margin-top:20px}} .wrap{{overflow:auto}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:11px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
th{{background:#f9fafb}} a{{color:#0969da;word-break:break-all}}
input,select{{padding:10px;border:1px solid #d0d5dd;border-radius:8px}}
#search{{width:50%;min-width:250px}}
</style></head><body><div class="container">
<h1>Website Dashboard</h1>
<div class="sub">Page and content overview · Last checked: {now}</div>
<div class="cards">{overview}</div>
<div class="cards">{seo}</div>
<div class="health">Pages scanned: <b>{len(pages)}</b> · Failed: <b>{failed}</b> · Removed: <b>{removed}</b> · Duration: <b>{duration}s</b></div>
<div class="section"><h2>Current Changes</h2>
<p>Showing {min(len(changes), MAX_DASHBOARD_ROWS)} of {len(changes)} change records.</p>
<input id="search" placeholder="Search URL, field or details" oninput="filterRows()">
<select id="type" onchange="filterRows()"><option value="">All types</option><option>new</option><option>changed</option><option>removed</option><option>failed</option></select>
<div class="wrap"><table id="changes"><thead><tr><th>Type</th><th>Priority</th><th>Page</th><th>Element</th><th>Details</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></div>
<div class="section"><h2>History</h2><div class="wrap"><table><thead><tr><th>Date</th><th>Pages</th><th>New</th><th>Changed</th><th>Removed</th><th>Failed</th><th>Duration</th></tr></thead>
<tbody>{''.join(history_rows)}</tbody></table></div></div>
</div>
<script>
function filterRows(){{
 const q=document.getElementById('search').value.toLowerCase();
 const t=document.getElementById('type').value.toLowerCase();
 document.querySelectorAll('#changes tbody tr').forEach(r=>{{
   const txt=r.innerText.toLowerCase();
   const typ=r.cells[0]?.innerText.toLowerCase()||'';
   r.style.display=(!q||txt.includes(q))&&(!t||typ===t)?'':'none';
 }});
}}
</script></body></html>"""
    DASHBOARD_FILE.write_text(html, encoding="utf-8")

def main():
    DATA_DIR.mkdir(exist_ok=True)
    old = load_snapshot()
    print(f"[BASELINE] {len(old)} pages", flush=True)

    result = crawl(old)
    pages = result["pages"]
    failed = result["failed"]
    removed = result["removed"]
    duration = result["duration"]

    # Temporary failures are kept in the baseline so they do not become false removals.
    for url in failed:
        if url in old and url not in pages:
            pages[url] = old[url]

    changes = compare(old, pages, failed, removed)
    removed_count = sum(c["type"] == "removed" for c in changes)

    save_snapshot(pages)
    history = save_history(changes, len(pages), len(failed), removed_count, duration)
    make_dashboard(pages, changes, history, duration, len(failed), removed_count)

    print(f"[NEW] {sum(c['type']=='new' for c in changes)} pages", flush=True)
    print(f"[CHANGED] {sum(c['type']=='changed' for c in changes)} records", flush=True)
    print(f"[REMOVED] {removed_count} pages", flush=True)
    print(f"[FAILED] {len(failed)} pages", flush=True)
    print(f"[PAGES] {len(pages)} pages", flush=True)
    print("[SNAPSHOT] Compact snapshot saved", flush=True)
    print(f"[COMPLETE] Duration: {duration}s", flush=True)

if __name__ == "__main__":
    main()
