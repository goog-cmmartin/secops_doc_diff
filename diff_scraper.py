import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
import time
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from config import DB_NAME, DOC_SOURCES, EXCLUDED_PATTERNS, LLMS_TXT_SOURCES

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def create_session_with_retries(accept_markdown=False):
    """Creates a requests.Session with a robust retry strategy and browser user-agent."""
    session = requests.Session()
    accept_header = (
        "text/markdown,text/plain;q=0.9,text/html;q=0.8,*/*;q=0.7"
        if accept_markdown
        else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": accept_header,
        "Accept-Language": "en-US,en;q=0.9"
    })
    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def setup_database():
    """Initializes the database and creates/updates tables, FTS, and indexes."""
    conn = sqlite3.connect(DB_NAME, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT NOT NULL,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, source_tag TEXT,
            summary TEXT, llm_generated_tags TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(pages)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'content_hash' not in columns:
        cursor.execute('ALTER TABLE pages ADD COLUMN content_hash TEXT')
        cursor.execute("SELECT url, content FROM pages WHERE content_hash IS NULL")
        rows_to_update = cursor.fetchall()
        if rows_to_update:
            for url, content in rows_to_update:
                cleaned_content = clean_content(content)
                content_hash = calculate_hash(cleaned_content)
                cursor.execute("UPDATE pages SET content_hash = ? WHERE url = ?", (content_hash, url))
            conn.commit()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pages_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
            content TEXT NOT NULL, archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS change_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, scrape_date DATE NOT NULL,
            url TEXT NOT NULL, change_type TEXT NOT NULL, content_hash TEXT,
            summary TEXT, source_tag TEXT, importance TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS broken_links (
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scrape_date DATE NOT NULL,
            source_url TEXT NOT NULL,
            target_url TEXT NOT NULL
        )
    ''')

    # --- FTS5 Virtual Table for Search ---
    cursor.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS change_log_fts USING fts5(
            summary,
            content='change_log',
            content_rowid='log_id'
        );
    ''')

    # Trigger to keep FTS index up to date
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS t_change_log_summary_update AFTER UPDATE OF summary ON change_log
        BEGIN
            INSERT INTO change_log_fts(change_log_fts, rowid) VALUES('delete', old.log_id);
            INSERT INTO change_log_fts(rowid, summary) VALUES (new.log_id, new.summary);
        END;
    ''')

    # --- Performance Indexes ---
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_change_log_date_type ON change_log(scrape_date, change_type);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_change_log_source_date ON change_log(source_tag, scrape_date);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_change_log_importance ON change_log(importance);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_change_log_summary ON change_log(summary);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_archive_url_archived ON pages_archive(url, archived_at DESC);")

    conn.commit()
    conn.close()
    logging.info(f"Database '{DB_NAME}' is ready.")

def parse_frontmatter_and_clean_md(text):
    """Extracts optional updatedAt date (YYYY-MM-DD) from YAML frontmatter and returns (cleaned_markdown, updated_at)."""
    if not text:
        return "", None
    updated_at = None
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n*", text, flags=re.DOTALL)
    if match:
        frontmatter = match.group(1)
        um = re.search(r"updatedAt:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", frontmatter)
        if um:
            updated_at = um.group(1)
        text = text[match.end():]
    return clean_content(text), updated_at

def clean_content(text):
    if not text:
        return ""
    text = re.sub(r"^---\r?\n.*?\r?\n---\r?\n*", "", text, flags=re.DOTALL)
    return re.sub(r"Last updated \d{4}-\d{2}-\d{2} UTC", "", text).strip()

def calculate_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def is_excluded(url, patterns):
    """Checks if a given URL matches any of the exclusion patterns."""
    for pattern in patterns:
        if pattern.endswith('/') and url.startswith(pattern):
            return True
        elif pattern.startswith('%') and pattern.endswith('%'):
            regex_pattern = pattern.replace('%', '.*')
            if re.search(regex_pattern, url):
                return True
        elif url.startswith(pattern):
            return True
    return False

def normalize_doc_link(link, base_url):
    """Normalizes equivalent cloud.google.com and docs.cloud.google.com hostnames to match base_url."""
    if base_url.startswith("https://cloud.google.com/") and link.startswith("https://docs.cloud.google.com/"):
        return "https://cloud.google.com/" + link[len("https://docs.cloud.google.com/"):]
    if base_url.startswith("https://docs.cloud.google.com/") and link.startswith("https://cloud.google.com/"):
        return "https://docs.cloud.google.com/" + link[len("https://cloud.google.com/"):]
    return link

def canonicalize_llms_url(url, base_url):
    """Converts a .md URL (e.g. https://gtidocs.readme.io/docs/foo.md) into its canonical web URL under base_url."""
    parsed = urlparse(url)
    path = parsed.path.lstrip('/')
    if path.endswith('.md'):
        path = path[:-3]
    return urljoin(base_url.rstrip('/') + '/', path)

def to_markdown_fetch_url(url):
    """Ensures a ReadMe/GTI URL points to the raw .md endpoint."""
    clean_url = url.split('#')[0].split('?')[0].rstrip('/')
    if not clean_url.endswith('.md'):
        clean_url += '.md'
    return clean_url

def fetch_markdown_page(md_url, canonical_url, session, index_url):
    """Fetches a single ReadMe .md endpoint, strips YAML frontmatter, and computes its hash."""
    try:
        with session.get(md_url, timeout=12) as response:
            response.raise_for_status()
            raw_text = response.text
            cleaned_md, updated_at = parse_frontmatter_and_clean_md(raw_text)
            if not cleaned_md or cleaned_md.startswith("Page not found. The full index is at"):
                logging.warning(f"Markdown page not found placeholder at {md_url}")
                return canonical_url, "", "", "", None, (index_url, canonical_url)
            content_hash = calculate_hash(cleaned_md)
            return canonical_url, cleaned_md, cleaned_md, content_hash, updated_at, None
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logging.warning(f"Broken link in llms.txt: {md_url}")
            return canonical_url, "", "", "", None, (index_url, canonical_url)
        logging.warning(f"HTTP error fetching markdown {md_url}: {e}")
    except requests.exceptions.RequestException as e:
        logging.warning(f"Request exception fetching markdown {md_url}: {e}")
    return canonical_url, "", "", "", None, None

def crawl_llms_txt_source(source_tag, base_url, llms_txt_url, max_workers=4):
    """Discovers all documentation pages via llms.txt and fetches their raw .md versions concurrently."""
    logging.info(f"Crawling llms.txt source: {source_tag} ({llms_txt_url}) with {max_workers} threads...")
    scraped_data = {}
    broken_links = set()
    updated_at_map = {}

    session = create_session_with_retries(accept_markdown=True)
    try:
        resp = session.get(llms_txt_url, timeout=15)
        resp.raise_for_status()
        index_text = resp.text
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch llms.txt index for {source_tag} at {llms_txt_url}: {e}")
        session.close()
        return scraped_data, broken_links, updated_at_map
    finally:
        session.close()

    md_urls = list(dict.fromkeys(re.findall(r'https?://[^\s\)\]>]+\.md', index_text)))
    logging.info(f"  Discovered {len(md_urls)} .md endpoints in {llms_txt_url} for {source_tag}.")

    url_pairs = []
    for md_url in md_urls:
        canonical_url = canonicalize_llms_url(md_url, base_url)
        if not is_excluded(canonical_url, EXCLUDED_PATTERNS) and not is_excluded(md_url, EXCLUDED_PATTERNS):
            url_pairs.append((md_url, canonical_url))

    thread_local = threading.local()

    def get_thread_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = create_session_with_retries(accept_markdown=True)
        return thread_local.session

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_markdown_page, md_url, canonical_url, get_thread_session(), llms_txt_url): canonical_url
            for md_url, canonical_url in url_pairs
        }
        for future in as_completed(futures):
            canonical_url, raw_content, cleaned_content, content_hash, updated_at, broken_link = future.result()
            completed += 1
            if broken_link:
                broken_links.add(broken_link)
            if raw_content:
                scraped_data[canonical_url] = (raw_content, cleaned_content, content_hash, source_tag)
                if updated_at:
                    updated_at_map[canonical_url] = updated_at
            if completed % 100 == 0:
                logging.info(f"  Fetched {completed}/{len(url_pairs)} markdown pages for {source_tag}...")

    logging.info(f"Crawl for {source_tag} complete: {len(scraped_data)} markdown pages retrieved.")
    return scraped_data, broken_links, updated_at_map

def fetch_page_and_links(url, base_url, session, source_page_url=None):
    """Fetches a page, extracts its links, and parses its clean text content in a single HTTP request."""
    links = set()
    broken_link = None
    raw_content = ""
    cleaned_content = ""
    content_hash = ""

    try:
        with session.get(url, timeout=12) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract links
            for a_tag in soup.find_all('a', href=True):
                raw_link = urljoin(url, a_tag['href']).split('#')[0]
                link = normalize_doc_link(raw_link, base_url)
                if link.startswith(base_url) and not is_excluded(link, EXCLUDED_PATTERNS):
                    links.add(link)
            
            # Extract content
            content_area = soup.find('div', class_='devsite-article-body') or soup.find('article') or soup.find('main')
            if content_area:
                raw_content = content_area.get_text(separator=' ', strip=True)
                cleaned_content = clean_content(raw_content)
                content_hash = calculate_hash(cleaned_content)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logging.warning(f"Broken link found: {url} (from {source_page_url or 'start'})")
            broken_link = (source_page_url or 'start', url)
        else:
            logging.warning(f"HTTP error fetching {url}: {e}")
    except requests.exceptions.RequestException as e:
        logging.warning(f"Request exception for {url}: {e}")

    return url, raw_content, cleaned_content, content_hash, links, broken_link

def scrape_single_url(url, session):
    """Scrapes a single URL for content (supports both standard HTML and ReadMe .md endpoints)."""
    if "gtidocs.readme.io" in url or "gtidocs.virustotal.com" in url or url.endswith(".md"):
        md_url = to_markdown_fetch_url(url)
        try:
            with session.get(md_url, headers={"Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.8"}, timeout=12) as response:
                response.raise_for_status()
                cleaned_md, _ = parse_frontmatter_and_clean_md(response.text)
                return cleaned_md
        except requests.exceptions.RequestException as e:
            logging.warning(f"Could not scrape markdown from {md_url}: {e}")
            return ""
    try:
        with session.get(url, timeout=12) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            content_area = soup.find('div', class_='devsite-article-body') or soup.find('article') or soup.find('main')
            return content_area.get_text(separator=' ', strip=True) if content_area else ""
    except requests.exceptions.RequestException as e:
        logging.warning(f"Could not scrape text from {url}: {e}")
        return ""

def crawl_source(source_tag, base_url, max_workers=4):
    """Crawls a doc source concurrently in a single pass extracting both links and content."""
    logging.info(f"Crawling source: {source_tag} with {max_workers} threads...")
    
    urls_to_visit = {base_url: 'start'}
    visited_urls = set()
    scraped_data = {} # {url: (raw_content, cleaned_content, content_hash, source_tag)}
    broken_links = set()
    
    # Thread-local sessions for safe concurrency
    thread_local = threading.local()

    def get_thread_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = create_session_with_retries(accept_markdown=False)
        return thread_local.session

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while urls_to_visit:
            # Prepare batch of URLs to fetch concurrently
            batch = {}
            while urls_to_visit and len(batch) < max_workers * 4:
                url, source_url = urls_to_visit.popitem()
                if url not in visited_urls:
                    visited_urls.add(url)
                    batch[url] = source_url

            if not batch:
                break

            future_to_url = {
                executor.submit(fetch_page_and_links, url, base_url, get_thread_session(), source_url): url
                for url, source_url in batch.items()
            }

            for future in as_completed(future_to_url):
                url, raw_content, cleaned_content, content_hash, new_links, broken_link = future.result()
                
                if broken_link:
                    broken_links.add(broken_link)
                
                if raw_content:
                    scraped_data[url] = (raw_content, cleaned_content, content_hash, source_tag)
                
                for link in new_links:
                    if link not in visited_urls and link not in urls_to_visit:
                        urls_to_visit[link] = url

            if len(visited_urls) % 50 < max_workers * 4 and len(visited_urls) >= 50:
                logging.info(f"  Discovered & fetched {len(visited_urls)} pages for {source_tag}...")

    logging.info(f"Crawl for {source_tag} complete: {len(scraped_data)} pages retrieved.")
    return scraped_data, broken_links

def main():
    parser = argparse.ArgumentParser(description="Scrape websites and log changes to a database.")
    parser.add_argument(
        '--setup-only',
        action='store_true',
        help="Run only the database setup function and exit."
    )
    parser.add_argument(
        '--url',
        type=str,
        help="Optional. Scrape a single URL."
    )
    parser.add_argument(
        '--source',
        type=str,
        help="Optional. Crawl only a specific source tag from DOC_SOURCES."
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=4,
        help="Number of concurrent worker threads for crawling (default: 4)."
    )
    args = parser.parse_args()

    if args.setup_only:
        setup_database()
        return

    setup_database()
    scrape_date = datetime.now().date()
    logging.info(f"--- Starting scrape for {scrape_date} ---")

    if args.url:
        target_url = args.url
        if "gtidocs.readme.io" in target_url or "gtidocs.virustotal.com" in target_url:
            target_url = canonicalize_llms_url(target_url, DOC_SOURCES.get("Google Threat Intelligence", "https://gtidocs.virustotal.com/"))
        logging.info(f"Scraping single URL: {target_url}")
        session = create_session_with_retries()
        content = scrape_single_url(args.url, session)
        if content:
            cleaned_content = clean_content(content)
            new_hash = calculate_hash(cleaned_content)
            source_tag = next((tag for tag, base in DOC_SOURCES.items() if target_url.startswith(base)), "Unknown")
            
            conn = sqlite3.connect(DB_NAME, timeout=60.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=60000;")
            cursor = conn.cursor()
            
            cursor.execute("SELECT content_hash FROM pages WHERE url=?", (target_url,))
            db_hash = cursor.fetchone()
            if db_hash and db_hash[0] == new_hash:
                logging.info("Content has not changed.")
            else:
                logging.info("Content has changed.")
                if db_hash:
                    cursor.execute("SELECT content FROM pages WHERE url=?", (target_url,))
                    old_content_row = cursor.fetchone()
                    if old_content_row and old_content_row[0] != content:
                        cursor.execute("INSERT INTO pages_archive (url, content) VALUES (?, ?)", (target_url, old_content_row[0]))
                    cursor.execute("UPDATE pages SET content=?, content_hash=?, scraped_at=CURRENT_TIMESTAMP WHERE url=?", (content, new_hash, target_url))
                else:
                    cursor.execute("INSERT INTO pages (url, content, content_hash, source_tag) VALUES (?, ?, ?, ?)", (target_url, content, new_hash, source_tag))
                
                # Check for existing change_log entry today
                cursor.execute("SELECT log_id FROM change_log WHERE scrape_date=? AND url=? AND change_type='updated'", (scrape_date, target_url))
                existing = cursor.fetchone()
                if existing:
                    cursor.execute("UPDATE change_log SET content_hash=? WHERE log_id=?", (new_hash, existing[0]))
                else:
                    cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (scrape_date, target_url, 'updated', new_hash, source_tag))
            conn.commit()
            conn.close()
        session.close()
        return

    # Determine which sources to crawl
    sources_to_crawl = DOC_SOURCES
    if args.source:
        if args.source not in DOC_SOURCES:
            logging.error(f"Unknown source '{args.source}'. Available sources: {list(DOC_SOURCES.keys())}")
            return
        sources_to_crawl = {args.source: DOC_SOURCES[args.source]}

    # Fetch existing state from DB
    conn = sqlite3.connect(DB_NAME, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    cursor = conn.cursor()
    
    cursor.execute("SELECT url, content_hash, source_tag FROM pages")
    db_rows = cursor.fetchall()
    db_state = {row[0]: row[1] for row in db_rows if row[2] in sources_to_crawl}
    db_urls_by_source = {}
    for url, _, source_tag in db_rows:
        if source_tag in sources_to_crawl:
            db_urls_by_source.setdefault(source_tag, set()).add(url)
    logging.info(f"Found {len(db_state)} pages in the local database for target sources.")

    # Crawl selected sources
    all_scraped_pages = {}
    all_broken_links = set()
    all_updated_at_map = {}

    for source_tag, base_url in sources_to_crawl.items():
        if source_tag in LLMS_TXT_SOURCES:
            source_pages, source_broken, source_updated_at = crawl_llms_txt_source(
                source_tag, base_url, LLMS_TXT_SOURCES[source_tag], max_workers=args.workers
            )
            all_updated_at_map.update(source_updated_at)
        else:
            source_pages, source_broken = crawl_source(source_tag, base_url, max_workers=args.workers)
        all_scraped_pages.update(source_pages)
        all_broken_links.update(source_broken)

    logging.info(f"All target sources crawled. Total live URLs fetched: {len(all_scraped_pages)}.")

    # Insert broken links
    if all_broken_links:
        logging.info(f"Found {len(all_broken_links)} broken links. Inserting into database...")
        for source_url, target_url in all_broken_links:
            cursor.execute(
                "INSERT INTO broken_links (scrape_date, source_url, target_url) VALUES (?, ?, ?)",
                (scrape_date, source_url, target_url)
            )
        conn.commit()

    # Track which sources are being seeded for the first time (0 pages previously in DB)
    initial_seed_sources = {
        tag for tag in sources_to_crawl
        if len(db_urls_by_source.get(tag, set())) == 0
    }
    if initial_seed_sources:
        logging.info(f"Initial baseline onboarding detected for source(s): {initial_seed_sources}")

    # Diff calculation per source with safety guard against partial crawl/rate limit drops
    all_live_urls = set(all_scraped_pages.keys())
    db_urls = set(db_state.keys())
    
    new_urls = all_live_urls - db_urls
    existing_urls = all_live_urls.intersection(db_urls)
    
    # Calculate removed URLs per source with safety threshold
    removed_urls = set()
    for source_tag, base_url in sources_to_crawl.items():
        source_db_urls = db_urls_by_source.get(source_tag, set())
        source_scraped_urls = {u for u, data in all_scraped_pages.items() if data[3] == source_tag}
        
        # Safety guard: if a source fetched fewer than 50% of known DB pages, skip removals to avoid false positives
        if len(source_db_urls) > 0 and len(source_scraped_urls) < (len(source_db_urls) * 0.5):
            logging.warning(
                f"Source '{source_tag}' crawl retrieved only {len(source_scraped_urls)}/{len(source_db_urls)} pages. "
                "Skipping removal detection for this source to prevent false removals."
            )
            continue
        
        removed_urls.update(source_db_urls - source_scraped_urls)

    logging.info(f"Diff Summary: {len(new_urls)} new, {len(removed_urls)} removed, {len(existing_urls)} existing pages.")

    # Process removed URLs: archive final snapshot and remove from active 'pages' table so they are not re-logged daily
    for url in removed_urls:
        source_tag = next((tag for tag, base in DOC_SOURCES.items() if url.startswith(base)), "Unknown")
        cursor.execute("SELECT content FROM pages WHERE url=?", (url,))
        old_content_row = cursor.fetchone()
        if old_content_row:
            cursor.execute("INSERT INTO pages_archive (url, content) VALUES (?, ?)", (url, old_content_row[0]))
        cursor.execute("DELETE FROM pages WHERE url=?", (url,))
        cursor.execute("SELECT log_id FROM change_log WHERE scrape_date=? AND url=? AND change_type='removed'", (scrape_date, url))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, source_tag) VALUES (?, ?, ?, ?)", (scrape_date, url, 'removed', source_tag))
    conn.commit()

    # Process new URLs
    logged_new_count = 0
    recent_cutoff = str(scrape_date - timedelta(days=14))
    for url in new_urls:
        raw_content, cleaned_content, new_hash, source_tag = all_scraped_pages[url]
        cursor.execute("INSERT OR REPLACE INTO pages (url, content, content_hash, source_tag) VALUES (?, ?, ?, ?)", (url, raw_content, new_hash, source_tag))
        
        # If this source is being seeded for the very first time, avoid flooding change_log with hundreds of baseline pages;
        # only log pages whose frontmatter updatedAt is within the last 14 days (or the latest changelog entry).
        if source_tag in initial_seed_sources:
            page_updated_at = all_updated_at_map.get(url)
            is_recent_doc = page_updated_at and page_updated_at >= recent_cutoff
            is_latest_changelog = "/changelog/october-13th-2026-" in url
            if not (is_recent_doc or is_latest_changelog):
                continue
            log_date = page_updated_at if is_recent_doc else "2026-10-13"
        else:
            log_date = scrape_date

        logged_new_count += 1
        cursor.execute("SELECT log_id FROM change_log WHERE scrape_date=? AND url=? AND change_type='new'", (log_date, url))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("UPDATE change_log SET content_hash=? WHERE log_id=?", (new_hash, existing[0]))
        else:
            cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (log_date, url, 'new', new_hash, source_tag))
    conn.commit()

    # Process existing URLs (only log 'updated' changes; do not bloat DB with 'unchanged')
    updated_count = 0
    for url in existing_urls:
        raw_content, cleaned_content, new_hash, source_tag = all_scraped_pages[url]
        old_hash = db_state.get(url)
        
        if new_hash != old_hash:
            updated_count += 1
            logging.info(f"  Change detected for: {url}")
            cursor.execute("SELECT content FROM pages WHERE url=?", (url,))
            old_content_row = cursor.fetchone()
            if old_content_row and old_content_row[0] != raw_content:
                cursor.execute("INSERT INTO pages_archive (url, content) VALUES (?, ?)", (url, old_content_row[0]))
            cursor.execute("UPDATE pages SET content=?, content_hash=?, scraped_at=CURRENT_TIMESTAMP WHERE url=?", (raw_content, new_hash, url))
            
            cursor.execute("SELECT log_id FROM change_log WHERE scrape_date=? AND url=? AND change_type='updated'", (scrape_date, url))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("UPDATE change_log SET content_hash=? WHERE log_id=?", (new_hash, existing[0]))
            else:
                cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (scrape_date, url, 'updated', new_hash, source_tag))

    conn.commit()
    conn.close()
    logging.info(f"--- Scrape complete: {len(new_urls)} discovered ({logged_new_count} logged to change_log), {updated_count} updated, {len(removed_urls)} removed. ---")

if __name__ == "__main__":
    main()
