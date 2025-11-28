import sqlite3
import vertexai
from vertexai.generative_models import GenerativeModel
from config import DB_NAME
import time
import logging
import argparse

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- Vertex AI Configuration ---
_model = None
def get_model():
    global _model
    if _model is None:
        vertexai.init(project="webapps-397711", location="us-central1")
        _model = GenerativeModel("gemini-2.5-flash")
    return _model

# --- LLM Prompt Template ---
IMPORTANCE_PROMPT_TEMPLATE = """
You are an expert software documentation analyst. Your task is to review the following change log summary and classify its importance based on the potential impact it has on a user's workflow, integration, or understanding of the system's core capabilities.

Classification Criteria:

* Low: Cosmetic/housekeeping changes, non-breaking documentation updates, renaming of internal features, minor textual clarifications, or adding non-core helper functions.
* Medium: New non-breaking features, adding a new method/resource that doesn't affect existing integrations, introducing a new event type, or important policy/guideline updates.
* High: Changes requiring user action or significant integration impact. Breaking changes to an API, removal of an existing feature/endpoint, or fundamental changes to core product functionality.
* Critical: Immediate and urgent action required. Major security vulnerabilities, core system outages, or changes that could lead to data loss.

Change Log Summary to Rate:
---
{summary}
---

Constraint:
You MUST only output a single word, which must be one of the defined importance levels: Low, Medium, High, or Critical. Do not include any explanations, markdown, or other text.
"""

def get_importance_rating(summary):
    """
    Calls the LLM to get an importance rating for a given summary.
    Includes retry logic for API calls.
    """
    if not summary:
        return None

    model = get_model()
    prompt = IMPORTANCE_PROMPT_TEMPLATE.format(summary=summary)
    retries = 3
    for i in range(retries):
        try:
            response = model.generate_content(prompt)
            rating = response.text.strip()
            if rating in ["Low", "Medium", "High", "Critical"]:
                return rating
            else:
                logging.warning(f"LLM returned unexpected rating '{rating}' for summary: {summary[:100]}...")
                return None # Or set a default like "Unknown"
        except Exception as e:
            logging.error(f"Error calling LLM (attempt {i+1}/{retries}): {e}")
            if i < retries - 1:
                time.sleep(2 ** i) # Exponential backoff
    logging.error(f"Failed to get importance rating after {retries} attempts for summary: {summary[:100]}...")
    return None # Failed after retries

def backfill_importance(dry_run=False):
    """
    Connects to the database and backfills the 'importance' column for existing entries.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()

    cursor.execute("SELECT log_id, summary FROM change_log WHERE importance IS NULL AND summary IS NOT NULL")
    entries_to_backfill = cursor.fetchall()
    
    if not entries_to_backfill:
        logging.info("No change_log entries found needing importance backfill.")
        conn.close()
        return

    log_prefix = "[DRY RUN] " if dry_run else ""
    logging.info(f"{log_prefix}Found {len(entries_to_backfill)} entries to backfill importance.")

    for i, (log_id, summary) in enumerate(entries_to_backfill):
        logging.info(f"Processing entry {i+1}/{len(entries_to_backfill)} (log_id: {log_id})...")
        importance = get_importance_rating(summary)
        if importance:
            if dry_run:
                logging.info(f"{log_prefix}Would update log_id {log_id} with importance: {importance}")
            else:
                cursor.execute("UPDATE change_log SET importance = ? WHERE log_id = ?", (importance, log_id))
                conn.commit()
                logging.info(f"  Updated log_id {log_id} with importance: {importance}")
        else:
            logging.warning(f"  Could not determine importance for log_id {log_id}. Skipping.")
        
        time.sleep(1) # Add a small delay to respect potential API rate limits

    conn.close()
    logging.info(f"{log_prefix}Importance backfill process completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill the 'importance' column in the change_log table.")
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Perform a dry run without committing any changes to the database."
    )
    args = parser.parse_args()
    
    backfill_importance(dry_run=args.dry_run)
