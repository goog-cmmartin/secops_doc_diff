import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import logging
import time
import sqlite3
import hashlib
import re
from datetime import datetime
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from config import DB_NAME, DOC_SOURCES, EXCLUDED_PATTERNS

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def create_session_with_retries():
    """Creates a requests.Session with a robust retry strategy."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
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

def clean_content(text):
    if not text: return ""
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

def fetch_page_and_links(url, base_url, session, source_page_url=None):
    """Fetches a page, extracts its links, and parses its clean text content in a single HTTP request."""
    links = set()
    broken_link = None
    raw_content = ""
    cleaned_content = ""
    content_hash = ""

    try:
        with session.get(url, timeout=10) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract links
            for a_tag in soup.find_all('a', href=True):
                link = urljoin(url, a_tag['href']).split('#')[0]
                if link.startswith(base_url) and not is_excluded(link, EXCLUDED_PATTERNS):
                    links.add(link)
            
            # Extract content
            content_area = soup.find('div', class_='devsite-article-body') or soup.find('article') or soup.find('main')
            if content_area:
                raw_content = content_area.get_text(separator=' ', strip=True)
                cleaned_content = clean_content(raw_content)
                content_hash = calculate_hash(cleaned_content)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logging.warning(f"Broken link found: {url} (from {source_page_url or 'start'})")
            broken_link = (source_page_url or 'start', url)
        else:
            logging.warning(f"HTTP error fetching {url}: {e}")
    except requests.exceptions.RequestException as e:
        logging.warning(f"Request exception for {url}: {e}")

    return url, raw_content, cleaned_content, content_hash, links, broken_link

def scrape_single_url(url, session):
    """Scrapes a single URL for content."""
    try:
        with session.get(url, timeout=10) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            content_area = soup.find('div', class_='devsite-article-body') or soup.find('article') or soup.find('main')
            return content_area.get_text(separator=' ', strip=True) if content_area else ""
    except requests.exceptions.RequestException as e:
        logging.warning(f"Could not scrape text from {url}: {e}")
        return ""

def crawl_source(source_tag, base_url, max_workers=8):
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
            thread_local.session = create_session_with_retries()
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
        '--workers',
        type=int,
        default=8,
        help="Number of concurrent worker threads for crawling (default: 8)."
    )
    args = parser.parse_args()

    if args.setup_only:
        setup_database()
        return

    setup_database()
    scrape_date = datetime.now().date()
    logging.info(f"--- Starting scrape for {scrape_date} ---")

    if args.url:
        logging.info(f"Scraping single URL: {args.url}")
        session = create_session_with_retries()
        content = scrape_single_url(args.url, session)
        if content:
            cleaned_content = clean_content(content)
            new_hash = calculate_hash(cleaned_content)
            source_tag = next((tag for tag, base in DOC_SOURCES.items() if args.url.startswith(base)), "Unknown")
            
            conn = sqlite3.connect(DB_NAME, timeout=60.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=60000;")
            cursor = conn.cursor()
            
            cursor.execute("SELECT content_hash FROM pages WHERE url=?", (args.url,))
            db_hash = cursor.fetchone()
            if db_hash and db_hash[0] == new_hash:
                logging.info("Content has not changed.")
            else:
                logging.info("Content has changed.")
                if db_hash:
                    cursor.execute("SELECT content FROM pages WHERE url=?", (args.url,))
                    old_content_row = cursor.fetchone()
                    if old_content_row:
                        cursor.execute("INSERT INTO pages_archive (url, content) VALUES (?, ?)", (args.url, old_content_row[0]))
                    cursor.execute("UPDATE pages SET content=?, content_hash=?, scraped_at=CURRENT_TIMESTAMP WHERE url=?", (content, new_hash, args.url))
                else:
                    cursor.execute("INSERT INTO pages (url, content, content_hash, source_tag) VALUES (?, ?, ?, ?)", (args.url, content, new_hash, source_tag))
                cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (scrape_date, args.url, 'updated', new_hash, source_tag))
            conn.commit()
            conn.close()
        session.close()
        return

    # Fetch existing state from DB
    conn = sqlite3.connect(DB_NAME, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    cursor = conn.cursor()
    
    cursor.execute("SELECT url, content_hash FROM pages")
    db_state = {row[0]: row[1] for row in cursor.fetchall()}
    logging.info(f"Found {len(db_state)} pages in the local database.")

    # Crawl all sources concurrently
    all_scraped_pages = {}
    all_broken_links = set()

    for source_tag, base_url in DOC_SOURCES.items():
        source_pages, source_broken = crawl_source(source_tag, base_url, max_workers=args.workers)
        all_scraped_pages.update(source_pages)
        all_broken_links.update(source_broken)

    logging.info(f"All sources crawled. Total live URLs fetched: {len(all_scraped_pages)}.")

    # Insert broken links
    if all_broken_links:
        logging.info(f"Found {len(all_broken_links)} broken links. Inserting into database...")
        for source_url, target_url in all_broken_links:
            cursor.execute(
                "INSERT INTO broken_links (scrape_date, source_url, target_url) VALUES (?, ?, ?)",
                (scrape_date, source_url, target_url)
            )
        conn.commit()

    # Diff calculation
    all_live_urls = set(all_scraped_pages.keys())
    db_urls = set(db_state.keys())
    
    new_urls = all_live_urls - db_urls
    removed_urls = db_urls - all_live_urls
    existing_urls = all_live_urls.intersection(db_urls)

    logging.info(f"Diff Summary: {len(new_urls)} new, {len(removed_urls)} removed, {len(existing_urls)} existing pages.")

    # Process removed URLs
    for url in removed_urls:
        source_tag = next((tag for tag, base in DOC_SOURCES.items() if url.startswith(base)), "Unknown")
        cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, source_tag) VALUES (?, ?, ?, ?)", (scrape_date, url, 'removed', source_tag))
    conn.commit()

    # Process new URLs
    for url in new_urls:
        raw_content, cleaned_content, new_hash, source_tag = all_scraped_pages[url]
        cursor.execute("INSERT INTO pages (url, content, content_hash, source_tag) VALUES (?, ?, ?, ?)", (url, raw_content, new_hash, source_tag))
        cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (scrape_date, url, 'new', new_hash, source_tag))
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
            if old_content_row:
                cursor.execute("INSERT INTO pages_archive (url, content) VALUES (?, ?)", (url, old_content_row[0]))
            cursor.execute("UPDATE pages SET content=?, content_hash=?, scraped_at=CURRENT_TIMESTAMP WHERE url=?", (raw_content, new_hash, url))
            cursor.execute("INSERT INTO change_log (scrape_date, url, change_type, content_hash, source_tag) VALUES (?, ?, ?, ?, ?)", (scrape_date, url, 'updated', new_hash, source_tag))

    conn.commit()
    conn.close()
    logging.info(f"--- Scrape complete: {len(new_urls)} new, {updated_count} updated, {len(removed_urls)} removed. ---")

if __name__ == "__main__":
    main()
