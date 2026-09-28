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


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.excelr.com"

MAX_PAGES = 5000
MAX_WORKERS = 8
TIMEOUT = 20

MAX_DASHBOARD_ROWS = 1000
MAX_DIFF_CHARS = 8000

DATA_DIR = Path("data")

SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"

CONTENT_FILE = DATA_DIR / "content.json.gz"

# New compressed store for:
# images
# internal links
# schema
# plus metadata
DETAILS_FILE = DATA_DIR / "details.json.gz"

DIFF_FILE = DATA_DIR / "latest_diffs.json"

DASHBOARD_FILE = Path("dashboard.html")


# ============================================================
# REQUEST HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; WebsiteAudit/1.0; +https://github.com/)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# FIELDS
# ============================================================

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


# ============================================================
# URL CLEANING
# ============================================================

def clean_url(url):

    try:

        p = urlparse(url)

        if p.scheme not in ("http", "https"):
            return None

        if p.netloc.lower() != urlparse(BASE_URL).netloc.lower():
            return None

        path = p.path or "/"

        bad = (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".ico",
            ".pdf",
            ".zip",
            ".mp4",
            ".mp3",
            ".webm",
            ".css",
            ".js",
            ".xml",
            ".json",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
        )

        if path.lower().endswith(bad):
            return None

        out = f"{p.scheme}://{p.netloc}{path}"

        if path != "/" and out.endswith("/"):
            out = out[:-1]

        return out

    except Exception:
        return None


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(value):

    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


# ============================================================
# HASH
# ============================================================

def stable_hash(value):

    if not isinstance(value, str):

        value = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    return hashlib.sha256(
        value.encode(
            "utf-8",
            errors="ignore"
        )
    ).hexdigest()


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

        content_type = (
            response
            .headers
            .get("content-type", "")
            .lower()
        )

        if (
            response.status_code == 200
            and "text/html" in content_type
        ):

            return {
                "status": "success",
                "html": response.text,
                "http_status": 200,
                "final_url": (
                    clean_url(response.url)
                    or response.url
                ),
            }


        if response.status_code in (404, 410):

            return {
                "status": "removed",
                "html": "",
                "http_status": response.status_code,
                "final_url": (
                    clean_url(response.url)
                    or response.url
                ),
            }


        return {
            "status": "failed",
            "html": "",
            "http_status": response.status_code,
            "final_url": (
                clean_url(response.url)
                or response.url
            ),
        }


    except requests.RequestException as error:

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "error": str(error),
        }


# ============================================================
# EXTRACT PAGE
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
    # META DESCRIPTION
    # --------------------------------------------------------

    description_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^description$",
                re.I
            )
        },
    )

    description = (
        description_tag.get("content", "")
        if description_tag
        else ""
    ).strip()


    # --------------------------------------------------------
    # CANONICAL
    # --------------------------------------------------------

    canonical_tag = soup.find(
        "link",
        attrs={
            "rel": lambda value:
                value
                and "canonical" in value
        },
    )

    canonical = (
        canonical_tag.get("href", "")
        if canonical_tag
        else ""
    ).strip()


    # --------------------------------------------------------
    # H1
    # --------------------------------------------------------

    h1 = " | ".join(
        x.get_text(
            " ",
            strip=True
        )
        for x in soup.find_all("h1")
    )


    # --------------------------------------------------------
    # ROBOTS
    # --------------------------------------------------------

    robots_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^robots$",
                re.I
            )
        },
    )

    robots = (
        robots_tag.get("content", "")
        if robots_tag
        else ""
    ).strip()


    # --------------------------------------------------------
    # IMAGES
    # --------------------------------------------------------

    images = []

    for img in soup.find_all("img"):

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        ).strip()

        if src:

            images.append(
                {
                    "src": src,
                    "alt": (
                        img.get("alt")
                        or ""
                    ).strip(),
                }
            )


    images.sort(
        key=lambda item: (
            item["src"],
            item["alt"]
        )
    )


    # --------------------------------------------------------
    # INTERNAL LINKS
    # --------------------------------------------------------

    links = set()

    for anchor in soup.find_all(
        "a",
        href=True
    ):

        target = clean_url(
            urljoin(
                url,
                anchor.get(
                    "href",
                    ""
                ).strip(),
            )
        )

        if target:

            links.add(target)


    internal_links = sorted(links)


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
        },
    ):

        raw = (
            script.string
            or script.get_text()
            or ""
        ).strip()

        if not raw:
            continue

        try:

            schemas.append(
                json.loads(raw)
            )

        except Exception:

            schemas.append(raw)


    # --------------------------------------------------------
    # MAIN CONTENT
    # --------------------------------------------------------

    text_soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in text_soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
        ]
    ):

        tag.decompose()


    main = text_soup.find("main")


    content = (
        main.get_text(
            " ",
            strip=True
        )
        if main
        else text_soup.get_text(
            " ",
            strip=True
        )
    )


    content = normalize_text(content)


    # --------------------------------------------------------
    # VALUES
    # --------------------------------------------------------

    values = {

        "title":
            normalize_text(title),

        "description":
            normalize_text(description),

        "canonical":
            normalize_text(canonical),

        "h1":
            normalize_text(h1),

        "robots":
            normalize_text(robots),

        "content":
            content,

        "images":
            images,

        "internal_links":
            internal_links,

        "schema":
            schemas,
    }


    # --------------------------------------------------------
    # PAGE SNAPSHOT
    #
    # Keep snapshot compact.
    # Detailed images / links / schema are stored separately
    # in details.json.gz.
    # --------------------------------------------------------

    page = {

        "url": url,

        "fingerprints": {
            key: stable_hash(value)
            for key, value in values.items()
        },

        "content_length":
            len(content),

        "values": {

            "title":
                values["title"],

            "description":
                values["description"],

            "canonical":
                values["canonical"],

            "h1":
                values["h1"],

            "robots":
                values["robots"],
        },

        "content_preview":
            content[:2000],
    }


    return (
        page,
        links,
        values
    )


# ============================================================
# SITEMAP
# ============================================================

def get_sitemap_urls():

    found = set()

    for sitemap_url in (
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml",
    ):

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


            soup = BeautifulSoup(
                response.text,
                "xml"
            )


            locs = [
                node.get_text(strip=True)
                for node in soup.find_all("loc")
            ]


            for loc in locs:

                if loc.lower().endswith(".xml"):

                    try:

                        child = requests.get(
                            loc,
                            headers=HEADERS,
                            timeout=TIMEOUT,
                        )

                        if child.status_code != 200:
                            continue


                        child_soup = BeautifulSoup(
                            child.text,
                            "xml"
                        )


                        child_locs = [
                            node.get_text(strip=True)
                            for node in child_soup.find_all(
                                "loc"
                            )
                        ]


                        for item in child_locs:

                            cleaned = clean_url(item)

                            if cleaned:

                                found.add(cleaned)


                    except requests.RequestException:

                        pass


                else:

                    cleaned = clean_url(loc)

                    if cleaned:

                        found.add(cleaned)


            if found:

                print(
                    f"[SITEMAP] {len(found)} HTML URLs found",
                    flush=True
                )

                return found


        except requests.RequestException as error:

            print(
                f"[SITEMAP ERROR] {error}",
                flush=True
            )


    return found


# ============================================================
# LOAD SNAPSHOT
# ============================================================

def load_snapshot():

    if not SNAPSHOT_FILE.exists():

        return {}


    try:

        raw = json.loads(
            SNAPSHOT_FILE.read_text(
                encoding="utf-8"
            )
        )


        pages = (
            raw.get("pages", {})
            if isinstance(raw, dict)
            else {}
        )


        output = {}


        for url, data in pages.items():

            if not isinstance(data, dict):
                continue


            if isinstance(
                data.get("fingerprints"),
                dict
            ):

                output[url] = data

                continue


            # ------------------------------------------------
            # Legacy snapshot migration
            # ------------------------------------------------

            values = {

                key:
                    data.get(key)

                for key, _, _ in FIELDS

                if key in data

            }


            output[url] = {

                "url": url,

                "fingerprints": {
                    key: stable_hash(value)
                    for key, value in values.items()
                },

                "content_length":
                    data.get(
                        "content_length",
                        len(
                            data.get(
                                "content",
                                ""
                            ) or ""
                        ),
                    ),

                "values": {

                    key:
                        data.get(
                            key,
                            ""
                        )

                    for key in (
                        "title",
                        "description",
                        "canonical",
                        "h1",
                        "robots",
                    )

                },

                "content_preview":
                    (
                        data.get(
                            "content",
                            ""
                        )
                        or ""
                    )[:2000],

            }


        return output


    except Exception as error:

        print(
            f"[WARNING] Snapshot read failed: {error}",
            flush=True
        )

        return {}


# ============================================================
# LOAD CONTENT STORE
# ============================================================

def load_content_store():

    if not CONTENT_FILE.exists():

        return {}


    try:

        with gzip.open(
            CONTENT_FILE,
            "rt",
            encoding="utf-8"
        ) as file:

            data = json.load(file)


        return (
            data
            if isinstance(data, dict)
            else {}
        )


    except Exception as error:

        print(
            f"[WARNING] Content store read failed: {error}",
            flush=True
        )

        return {}


# ============================================================
# SAVE CONTENT STORE
# ============================================================

def save_content_store(
    pages,
    values_by_url,
    old_content
):

    store = dict(old_content)


    for url, values in values_by_url.items():

        store[url] = values.get(
            "content",
            ""
        )


    store = {

        url:
            store.get(
                url,
                ""
            )

        for url in pages

    }


    with gzip.open(
        CONTENT_FILE,
        "wt",
        encoding="utf-8"
    ) as file:

        json.dump(
            store,
            file,
            ensure_ascii=False,
            separators=(",", ":")
        )


# ============================================================
# LOAD DETAILS STORE
# ============================================================

def load_details_store():

    if not DETAILS_FILE.exists():

        return {}


    try:

        with gzip.open(
            DETAILS_FILE,
            "rt",
            encoding="utf-8"
        ) as file:

            data = json.load(file)


        return (
            data
            if isinstance(data, dict)
            else {}
        )


    except Exception as error:

        print(
            f"[WARNING] Details store read failed: {error}",
            flush=True
        )

        return {}


# ============================================================
# SAVE DETAILS STORE
# ============================================================

def save_details_store(
    pages,
    values_by_url,
    old_details
):

    store = dict(old_details)


    for url, values in values_by_url.items():

        store[url] = {

            "title":
                values.get(
                    "title",
                    ""
                ),

            "description":
                values.get(
                    "description",
                    ""
                ),

            "canonical":
                values.get(
                    "canonical",
                    ""
                ),

            "h1":
                values.get(
                    "h1",
                    ""
                ),

            "robots":
                values.get(
                    "robots",
                    ""
                ),

            "images":
                values.get(
                    "images",
                    []
                ),

            "internal_links":
                values.get(
                    "internal_links",
                    []
                ),

            "schema":
                values.get(
                    "schema",
                    []
                ),
        }


    store = {

        url:
            store.get(
                url,
                {}
            )

        for url in pages

    }


    with gzip.open(
        DETAILS_FILE,
        "wt",
        encoding="utf-8"
    ) as file:

        json.dump(
            store,
            file,
            ensure_ascii=False,
            separators=(",", ":")
        )


# ============================================================
# CRAWL
# ============================================================

def crawl(old_pages):

    started = time.time()


    sitemap = get_sitemap_urls()


    home = clean_url(BASE_URL)

    if home:

        sitemap.add(home)


    urls = set(sitemap)

    urls.update(
        old_pages.keys()
    )


    urls = sorted(
        url
        for url in urls
        if url
    )[:MAX_PAGES]


    print(
        f"[START] {len(urls)} pages",
        flush=True
    )


    pages = {}

    values_by_url = {}

    failed = {}

    removed = set()

    discovered = set()


    def run_batch(
        batch,
        label
    ):

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:


            futures = {

                executor.submit(
                    fetch,
                    url
                ): url

                for url in batch

            }


            done = 0


            for future in as_completed(
                futures
            ):

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

                        discovered.update(
                            links
                        )


                    elif result["status"] == "removed":

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

                        "error":
                            str(error),

                    }


                done += 1


                if (
                    done % 25 == 0
                    or done == len(batch)
                ):

                    print(
                        f"[{label}] "
                        f"{done}/{len(batch)}",
                        flush=True
                    )


    run_batch(
        urls,
        "PROGRESS"
    )


    additional = (
        discovered
        - set(pages)
        - set(failed)
        - removed
    )


    additional = sorted(
        additional
    )[
        :max(
            0,
            MAX_PAGES - len(pages)
        )
    ]


    print(
        f"[DISCOVERY] "
        f"{len(additional)} additional "
        f"internal-link pages",
        flush=True
    )


    if additional:

        run_batch(
            additional,
            "DISCOVERY PROGRESS"
        )


    return {

        "pages":
            pages,

        "values":
            values_by_url,

        "failed":
            failed,

        "removed":
            removed,

        "duration":
            round(
                time.time() - started,
                2
            ),

    }


# ============================================================
# CONTENT DIFF
# ============================================================

def content_diff(
    before,
    after
):

    diff = list(
        difflib.unified_diff(
            str(before or "").split(),
            str(after or "").split(),
            fromfile="Before",
            tofile="After",
            lineterm="",
        )
    )


    text = "\n".join(diff)


    if len(text) > MAX_DIFF_CHARS:

        text = (
            text[:MAX_DIFF_CHARS]
            + "\n...[diff truncated]"
        )


    return text


# ============================================================
# GENERAL VALUE DIFF
# ============================================================

def make_diff(
    before_page,
    after_page,
    old_content,
    new_content,
    old_details,
    new_details,
):
    output = []


    # --------------------------------------------------------
    # Basic metadata
    # --------------------------------------------------------

    basic_fields = (
        "title",
        "description",
        "canonical",
        "h1",
        "robots",
    )


    for key in basic_fields:

        before_value = (
            before_page
            .get("values", {})
            .get(key, "")
        )


        after_value = (
            after_page
            .get("values", {})
            .get(key, "")
        )


        if before_value != after_value:

            output.append({

                "field":
                    key.title(),

                "before":
                    before_value,

                "after":
                    after_value,

            })


    # --------------------------------------------------------
    # CONTENT
    # --------------------------------------------------------

    before_content = (
        old_content
        or before_page.get(
            "content_preview",
            ""
        )
    )


    after_content = (
        new_content
        or after_page.get(
            "content_preview",
            ""
        )
    )


    if (
        before_page
        .get("fingerprints", {})
        .get("content")
        !=
        after_page
        .get("fingerprints", {})
        .get("content")
    ):

        output.append({

            "field":
                "Content",

            "before":
                before_content,

            "after":
                after_content,

            "diff":
                content_diff(
                    before_content,
                    after_content
                ),

        })


    # --------------------------------------------------------
    # DETAILED FIELDS
    # --------------------------------------------------------

    detailed_fields = (
        "images",
        "internal_links",
        "schema",
    )


    for key in detailed_fields:

        # If old details don't exist yet,
        # this is the first baseline for this field.
        # Do NOT report a false change.
        if key not in old_details:

            continue


        before_value = old_details.get(
            key,
            []
        )


        after_value = new_details.get(
            key,
            []
        )


        if before_value != after_value:

            label = {

                "images":
                    "Images",

                "internal_links":
                    "Internal Links",

                "schema":
                    "Schema",

            }.get(
                key,
                key.title()
            )


            output.append({

                "field":
                    label,

                "before":
                    before_value,

                "after":
                    after_value,

            })


    return output


# ============================================================
# COMPARE
# ============================================================

def compare(
    old,
    new,
    failed,
    removed,
    old_content,
    new_content,
    old_details,
    new_details,
):

    changes = []

    diffs = {}


    old_urls = set(old)

    new_urls = set(new)


    # --------------------------------------------------------
    # NEW PAGES
    # --------------------------------------------------------

    for url in sorted(
        new_urls - old_urls
    ):

        changes.append({

            "type":
                "new",

            "url":
                url,

            "field":
                "Page",

            "details":
                "New page discovered",

            "priority":
                "medium",

        })


    # --------------------------------------------------------
    # REMOVED PAGES
    # --------------------------------------------------------

    for url in sorted(
        (old_urls - new_urls)
        & removed
    ):

        changes.append({

            "type":
                "removed",

            "url":
                url,

            "field":
                "Page",

            "details":
                "Page returned 404/410",

            "priority":
                "high",

        })


    # --------------------------------------------------------
    # FAILED
    # --------------------------------------------------------

    for url in sorted(failed):

        changes.append({

            "type":
                "failed",

            "url":
                url,

            "field":
                "Crawl",

            "details":
                "Temporary crawl failure",

            "priority":
                "medium",

        })


    # --------------------------------------------------------
    # EXISTING PAGES
    # --------------------------------------------------------

    for url in sorted(
        old_urls & new_urls
    ):

        before = old[url]

        after = new[url]


        changed_records = make_diff(

            before_page=before,

            after_page=after,

            old_content=
                old_content.get(
                    url,
                    ""
                ),

            new_content=
                new_content.get(
                    url,
                    ""
                ),

            old_details=
                old_details.get(
                    url,
                    {}
                ),

            new_details=
                new_details.get(
                    url,
                    {}
                ),

        )


        if changed_records:

            diffs[url] = changed_records


            for record in changed_records:

                priority = "low"


                if record["field"] in (
                    "Canonical",
                    "H1",
                    "Robots",
                    "Schema",
                    "Title",
                ):

                    priority = "high"


                elif record["field"] in (
                    "Description",
                    "Images",
                    "Internal Links",
                ):

                    priority = "medium"


                changes.append({

                    "type":
                        "changed",

                    "url":
                        url,

                    "field":
                        record["field"],

                    "details":
                        f"{record['field']} changed",

                    "priority":
                        priority,

                })


    return (
        changes,
        diffs
    )


# ============================================================
# SAVE SNAPSHOT
# ============================================================

def save_snapshot(pages):

    data = {

        "version":
            4,

        "site":
            BASE_URL,

        "checked_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "pages":
            pages,

    }


    SNAPSHOT_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )


# ============================================================
# SAVE HISTORY
# ============================================================

def save_history(
    changes,
    pages,
    failed,
    removed,
    duration,
):

    history = []


    if HISTORY_FILE.exists():

        try:

            existing = json.loads(
                HISTORY_FILE.read_text(
                    encoding="utf-8"
                )
            )


            if isinstance(
                existing,
                list
            ):

                history = existing


        except Exception:

            pass


    history.append({

        "checked_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "pages":
            pages,

        "new":
            sum(
                c["type"] == "new"
                for c in changes
            ),

        "changed":
            sum(
                c["type"] == "changed"
                for c in changes
            ),

        "removed":
            removed,

        "failed":
            failed,

        "duration":
            duration,

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


# ============================================================
# DASHBOARD HELPERS
# ============================================================

def json_for_html(value):

    return escape(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2
        )
    )


def make_dashboard(
    pages,
    changes,
    history,
    duration,
    failed,
    removed,
    diffs,
):
    """Generate a lightweight dashboard.

    The old dashboard embedded full before/after content for every change into
    dashboard.html and ran an expensive browser-side word diff for all rows.
    With hundreds of content changes this could exhaust Chrome memory.

    This version embeds only lightweight change metadata. Full details are
    written to one small JSON file per changed URL and loaded only when the
    user opens a change.
    """

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    new_count = sum(c.get("type") == "new" for c in changes)
    changed_count = sum(c.get("type") == "changed" for c in changes)
    high_count = sum(c.get("priority") == "high" for c in changes)

    field_counts = {
        label: sum(
            c.get("type") == "changed" and c.get("field") == label
            for c in changes
        )
        for _, label, _ in FIELDS
    }

    # ------------------------------------------------------------
    # Store detailed change data outside dashboard.html.
    # One file per changed URL keeps the dashboard itself tiny.
    # ------------------------------------------------------------
    details_dir = DATA_DIR / "diff_details"
    details_dir.mkdir(parents=True, exist_ok=True)

    wanted_files = set()

    for url, records in diffs.items():
        filename = hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json"
        wanted_files.add(filename)
        detail_path = details_dir / filename
        detail_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "records": records,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    # Remove old detail files that are no longer part of the current crawl.
    for old_file in details_dir.glob("*.json"):
        if old_file.name not in wanted_files:
            try:
                old_file.unlink()
            except OSError:
                pass

    # Keep a tiny index so the directory always has a tracked file.
    (details_dir / "index.json").write_text(
        json.dumps(
            {
                "generated_at": now,
                "files": sorted(wanted_files),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # ------------------------------------------------------------
    # Only lightweight data goes into the HTML.
    # ------------------------------------------------------------
    dashboard_changes = []

    for change in changes[:MAX_DASHBOARD_ROWS]:
        item = {
            "type": change.get("type", ""),
            "url": change.get("url", ""),
            "field": change.get("field", ""),
            "details": change.get("details", ""),
            "priority": change.get("priority", "low"),
        }

        url = item["url"]
        if item["type"] == "changed" and url in diffs:
            item["detail_file"] = hashlib.sha256(
                url.encode("utf-8")
            ).hexdigest() + ".json"

        dashboard_changes.append(item)

    changes_json = json.dumps(
        dashboard_changes,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    history_json = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    fields_json = json.dumps(
        [label for _, label, _ in FIELDS],
        ensure_ascii=False,
        separators=(",", ":"),
    )

    field_cards = "".join(
        f"""
        <div class="card">
            <div class="card-label">{escape(label)}</div>
            <div class="card-number">{field_counts.get(label, 0)}</div>
        </div>
        """
        for _, label, _ in FIELDS
    )

    history_rows = "".join(
        f"""
        <tr>
            <td>{escape(str(item.get("checked_at", "")))}</td>
            <td>{item.get("pages", 0)}</td>
            <td>{item.get("new", 0)}</td>
            <td>{item.get("changed", 0)}</td>
            <td>{item.get("removed", 0)}</td>
            <td>{item.get("failed", 0)}</td>
            <td>{item.get("duration", 0)} sec</td>
        </tr>
        """
        for item in reversed(history)
    )

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ExcelR Website Monitoring Dashboard</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f6f8;color:#202124}
.container{max-width:1500px;margin:auto;padding:30px}
.header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:25px}
h1{margin:0 0 6px;font-size:32px}.subtitle{color:#6b7280;font-size:14px}
.status{background:#e8f5e9;color:#166534;padding:9px 14px;border-radius:20px;font-size:13px;font-weight:bold}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:17px;box-shadow:0 2px 7px rgba(0,0,0,.04)}
.card-label{font-size:11px;color:#6b7280;font-weight:bold;text-transform:uppercase}.card-number{font-size:28px;font-weight:800;margin-top:7px}
.section{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:20px;margin-bottom:22px}
.section h2{margin:0 0 15px;font-size:21px}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:15px}
input,select{padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;background:#fff;font-size:14px}
input{flex:1;min-width:280px}
.change-card{border:1px solid #e5e7eb;border-radius:12px;margin-bottom:12px;overflow:hidden;background:#fff}
.change-summary{padding:15px;cursor:pointer;list-style:none}.change-summary::-webkit-details-marker{display:none}
.summary-row{display:flex;justify-content:space-between;gap:12px}.summary-left{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.badge,.priority{padding:5px 9px;border-radius:20px;font-size:10px;font-weight:bold}
.badge.changed{background:#fff7ed;color:#9a3412}.badge.new{background:#ecfdf5;color:#047857}.badge.removed{background:#fef2f2;color:#b91c1c}.badge.failed{background:#fef3c7;color:#92400e}
.priority-high{background:#fee2e2;color:#b91c1c}.priority-medium{background:#fef3c7;color:#92400e}.priority-low{background:#f3f4f6;color:#4b5563}
.field{font-weight:800}.url{font-size:13px;color:#374151;word-break:break-all}
.change-body{padding:0 15px 20px}.loading{padding:15px;color:#6b7280}.error{padding:15px;color:#b91c1c;background:#fff1f2;border-radius:8px}
.comparison{display:grid;grid-template-columns:1fr 40px 1fr;gap:12px}.value-box{border-radius:10px;padding:14px;min-width:0}
.old-box{background:#fff1f2;border:1px solid #fecdd3}.new-box{background:#ecfdf5;border:1px solid #a7f3d0}
.box-title{font-size:11px;font-weight:900;margin-bottom:9px}.old-title{color:#be123c}.new-title{color:#047857}
.value{white-space:pre-wrap;word-break:break-word;line-height:1.55;font-size:13px;max-height:450px;overflow:auto}
.arrow{display:flex;justify-content:center;align-items:center;font-size:24px;color:#6b7280}
.exact{margin-top:18px}.exact-title{font-size:14px;font-weight:800;margin-bottom:9px}
.diff-box{background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:12px;max-height:400px;overflow:auto}
.diff-box pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;line-height:1.55}
.meta{margin-top:10px;color:#6b7280;font-size:13px}.empty{text-align:center;padding:40px;color:#6b7280}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:11px;border-bottom:1px solid #e5e7eb;text-align:left}th{background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase}
.load-button{padding:9px 12px;border:1px solid #d1d5db;border-radius:8px;background:#fff;cursor:pointer;font-weight:700}.load-button:hover{background:#f9fafb}
@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.comparison{grid-template-columns:1fr}.arrow{transform:rotate(90deg);height:25px}}
@media(max-width:600px){.container{padding:15px}.cards{grid-template-columns:repeat(2,1fr)}.header{display:block}.status{display:inline-block;margin-top:10px}}
</style>
</head>
<body>
<div class="container">
<div class="header"><div><h1>ExcelR Website Monitoring Dashboard</h1><div class="subtitle">SEO changes, page changes and crawl history</div></div><div class="status">Monitoring data loaded</div></div>

<div class="cards">
<div class="card"><div class="card-label">Pages</div><div class="card-number">__PAGES__</div></div>
<div class="card"><div class="card-label">New</div><div class="card-number">__NEW__</div></div>
<div class="card"><div class="card-label">Changed</div><div class="card-number">__CHANGED__</div></div>
<div class="card"><div class="card-label">Removed</div><div class="card-number">__REMOVED__</div></div>
<div class="card"><div class="card-label">Failed</div><div class="card-number">__FAILED__</div></div>
<div class="card"><div class="card-label">High Priority</div><div class="card-number">__HIGH__</div></div>
</div>

<div class="section">
<h2>Current Changes</h2>
<div class="subtitle" style="margin-bottom:12px">Details are loaded only when you open a change, so the dashboard stays fast even with hundreds of content changes.</div>
<div class="filters">
<input id="search" placeholder="Search URL, field or details...">
<select id="typeFilter"><option value="">All types</option><option value="changed">Changed</option><option value="new">New</option><option value="removed">Removed</option><option value="failed">Failed</option></select>
<select id="fieldFilter"><option value="">All fields</option></select>
</div>
<div id="changesContainer"></div>
</div>

<div class="section"><h2>Field Changes</h2><div class="cards">__FIELD_CARDS__</div></div>

<div class="section"><h2>Crawl History</h2><div class="table-wrap"><table><thead><tr><th>Date</th><th>Pages</th><th>New</th><th>Changed</th><th>Removed</th><th>Failed</th><th>Duration</th></tr></thead><tbody>__HISTORY_ROWS__</tbody></table></div></div>

<div class="subtitle">Last checked: __NOW__</div>
</div>

<script>
const changes = __CHANGES_JSON__;
const fieldNames = __FIELDS_JSON__;

function escapeHTML(value){
    return String(value ?? '').replace(/[&<>"']/g, function(char){
        const map={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'};
        return map[char];
    });
}

function formatValue(value){
    if(value !== null && typeof value === 'object') return JSON.stringify(value,null,2);
    return String(value ?? '');
}

function badge(type){return '<span class="badge '+escapeHTML(type)+'">'+escapeHTML(type.toUpperCase())+'</span>';}
function priority(value){const v=value||'low';return '<span class="priority priority-'+escapeHTML(v)+'">'+escapeHTML(v.toUpperCase())+'</span>';}

async function loadDetails(card,item){
    if(!item.detail_file){
        card.querySelector('.change-body').innerHTML='<div class="meta">No detailed before/after data for this change.</div>';
        return;
    }

    const body=card.querySelector('.change-body');
    if(item.loaded){body.innerHTML=item.html;return;}
    body.innerHTML='<div class="loading">Loading exact before/after details...</div>';

    try{
        const response=await fetch('data/diff_details/'+encodeURIComponent(item.detail_file),{cache:'no-store'});
        if(!response.ok) throw new Error('HTTP '+response.status);
        const data=await response.json();
        const record=(data.records||[]).find(r=>r.field===item.field) || (data.records||[])[0];

        if(!record){
            body.innerHTML='<div class="meta">No detailed record found.</div>';
            return;
        }

        let html='';
        html+='<div class="comparison">';
        html+='<div class="value-box old-box"><div class="box-title old-title">🔴 OLD / BEFORE</div><div class="value">'+escapeHTML(formatValue(record.before))+'</div></div>';
        html+='<div class="arrow">→</div>';
        html+='<div class="value-box new-box"><div class="box-title new-title">🟢 NEW / AFTER</div><div class="value">'+escapeHTML(formatValue(record.after))+'</div></div>';
        html+='</div>';

        if(record.diff){
            html+='<div class="exact"><div class="exact-title">📍 EXACT CHANGE</div><div class="diff-box"><pre>'+escapeHTML(record.diff)+'</pre></div></div>';
        }

        html+='<div class="meta">'+escapeHTML(item.details||'')+'</div>';
        item.html=html;
        item.loaded=true;
        body.innerHTML=html;
    }catch(error){
        body.innerHTML='<div class="error">Could not load change details: '+escapeHTML(error.message)+'</div>';
    }
}

function render(){
    const search=document.getElementById('search').value.toLowerCase().trim();
    const type=document.getElementById('typeFilter').value;
    const field=document.getElementById('fieldFilter').value;

    const filtered=changes.filter(function(item){
        const text=(item.url+' '+item.field+' '+item.details).toLowerCase();
        return (!search || text.includes(search)) && (!type || item.type===type) && (!field || item.field===field);
    });

    const container=document.getElementById('changesContainer');
    if(!filtered.length){container.innerHTML='<div class="empty">No matching changes detected.</div>';return;}

    container.innerHTML=filtered.map(function(item,index){
        const id='change_'+index;
        let body='';
        if(item.type==='changed'){
            body='<div class="change-body"><button class="load-button" data-id="'+id+'">Load OLD / NEW details</button></div>';
        }else if(item.type==='new'){
            body='<div class="change-body"><div class="meta">🟢 New page discovered.</div></div>';
        }else if(item.type==='removed'){
            body='<div class="change-body"><div class="meta">🔴 Page returned 404/410.</div></div>';
        }else{
            body='<div class="change-body"><div class="meta">⚠️ '+escapeHTML(item.details||'')+'</div></div>';
        }
        return '<details class="change-card" data-index="'+changes.indexOf(item)+'"><summary class="change-summary"><div class="summary-row"><div class="summary-left">'+badge(item.type)+' <span class="field">'+escapeHTML(item.field)+'</span> <span class="url">'+escapeHTML(item.url)+'</span></div>'+priority(item.priority)+'</div></summary>'+body+'</details>';
    }).join('');

    container.querySelectorAll('.load-button').forEach(function(button){
        button.addEventListener('click',function(event){
            event.preventDefault();
            const details=button.closest('details');
            const itemIndex=Number(details.dataset.index);
            loadDetails(details,changes[itemIndex]);
        });
    });
}

function buildFieldFilter(){
    const select=document.getElementById('fieldFilter');
    const fields=[...new Set(changes.map(item=>item.field).filter(Boolean))].sort();
    fields.forEach(function(field){const option=document.createElement('option');option.value=field;option.textContent=field;select.appendChild(option);});
}

document.getElementById('search').addEventListener('input',render);
document.getElementById('typeFilter').addEventListener('change',render);
document.getElementById('fieldFilter').addEventListener('change',render);
buildFieldFilter();
render();
</script>
</body>
</html>
"""

    html = html.replace("__PAGES__", str(len(pages)))
    html = html.replace("__NEW__", str(new_count))
    html = html.replace("__CHANGED__", str(changed_count))
    html = html.replace("__REMOVED__", str(removed))
    html = html.replace("__FAILED__", str(failed))
    html = html.replace("__HIGH__", str(high_count))
    html = html.replace("__FIELD_CARDS__", field_cards)
    html = html.replace("__HISTORY_ROWS__", history_rows)
    html = html.replace("__NOW__", escape(now))
    html = html.replace("__CHANGES_JSON__", changes_json)
    html = html.replace("__FIELDS_JSON__", fields_json)

    DASHBOARD_FILE.write_text(html, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main():

    DATA_DIR.mkdir(
        exist_ok=True
    )


    old = load_snapshot()

    old_content = load_content_store()

    old_details = load_details_store()


    print(
        f"[BASELINE] {len(old)} pages",
        flush=True
    )


    result = crawl(old)


    pages = result["pages"]

    failed = result["failed"]

    removed = result["removed"]

    duration = result["duration"]


    # --------------------------------------------------------
    # Preserve failed old pages
    # --------------------------------------------------------

    for url in failed:

        if (
            url in old
            and url not in pages
        ):

            pages[url] = old[url]


    # --------------------------------------------------------
    # Current content
    # --------------------------------------------------------

    new_content = {

        url:
            values.get(
                "content",
                ""
            )

        for url, values
        in result["values"].items()

    }


    # --------------------------------------------------------
    # Current details
    # --------------------------------------------------------

    new_details = {

        url: {

            "title":
                values.get(
                    "title",
                    ""
                ),

            "description":
                values.get(
                    "description",
                    ""
                ),

            "canonical":
                values.get(
                    "canonical",
                    ""
                ),

            "h1":
                values.get(
                    "h1",
                    ""
                ),

            "robots":
                values.get(
                    "robots",
                    ""
                ),

            "images":
                values.get(
                    "images",
                    []
                ),

            "internal_links":
                values.get(
                    "internal_links",
                    []
                ),

            "schema":
                values.get(
                    "schema",
                    []
                ),

        }

        for url, values
        in result["values"].items()

    }


    # --------------------------------------------------------
    # Compare
    # --------------------------------------------------------

    changes, diffs = compare(

        old,

        pages,

        failed,

        removed,

        old_content,

        new_content,

        old_details,

        new_details,

    )


    # --------------------------------------------------------
    # Save latest diffs
    # --------------------------------------------------------

    DIFF_FILE.write_text(

        json.dumps(
            diffs,
            ensure_ascii=False,
            separators=(",", ":")
        ),

        encoding="utf-8"

    )


    # --------------------------------------------------------
    # Save snapshot
    # --------------------------------------------------------

    save_snapshot(
        pages
    )


    # --------------------------------------------------------
    # Save content
    # --------------------------------------------------------

    save_content_store(

        pages,

        result["values"],

        old_content

    )


    # --------------------------------------------------------
    # Save detailed metadata
    # --------------------------------------------------------

    save_details_store(

        pages,

        result["values"],

        old_details

    )


    # --------------------------------------------------------
    # Removed count
    # --------------------------------------------------------

    removed_count = sum(

        c["type"] == "removed"

        for c in changes

    )


    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    history = save_history(

        changes,

        len(pages),

        len(failed),

        removed_count,

        duration,

    )


    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------

    make_dashboard(

        pages,

        changes,

        history,

        duration,

        len(failed),

        removed_count,

        diffs,

    )


    # --------------------------------------------------------
    # LOGS
    # --------------------------------------------------------

    new_count = sum(

        c["type"] == "new"

        for c in changes

    )


    changed_count = sum(

        c["type"] == "changed"

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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
