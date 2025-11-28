# Doc Diff Dashboard

The Doc Diff Dashboard is a web application that monitors Google Cloud documentation for changes, summarizes them using generative AI, and presents them in a user-friendly interface.

## Features

*   **Web Dashboard**: A Flask-based web application to view, filter, and search for documentation changes.
*   **Automated Scraping**: A script (`diff_scraper.py`) that periodically scrapes Google Cloud documentation pages to detect changes.
*   **AI-Powered Summarization**: A script (`summarize_changes.py`) that uses the Gemini AI model to generate concise summaries of the detected changes.
*   **Importance Ranking**: A script (`backfill_importance.py`) that uses the Gemini AI model to assign an importance level (Low, Medium, High, Critical) to each change.
*   **SQLite Database**: A simple and lightweight SQLite database to store the documentation content, changes, and summaries.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/goog-cmmartin/secops_doc_diff.git
    cd secops_doc_diff
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up the database:**
    The first time you run the scraper, it will create the `doc_content.db` database file and set up the necessary tables.

## Usage

### Running the Web Application

To run the web application, you can use a WSGI server like Gunicorn:

```bash
gunicorn --bind 0.0.0.0:8000 wsgi:app
```

The application will be accessible at `http://localhost:8000`.

### Running the Scraper and Summarizer

The `diff_scraper.py` and `summarize_changes.py` scripts are designed to be run periodically to keep the documentation changes up to date.

*   **`diff_scraper.py`**: This script scrapes the documentation sources defined in `config.py` and stores the changes in the database.
*   **`summarize_changes.py`**: This script retrieves the changes from the database and uses the Gemini AI model to generate summaries.
*   **`backfill_importance.py`**: This script backfills the importance ranking for existing changes in the database.

## Crontab Configuration

To automate the execution of the scraper and summarizer scripts, you can set up cron jobs. Here is an example crontab configuration to run the scraper every hour and the summarizer every two hours:

```crontab
# m h  dom mon dow   command
0 * * * * /path/to/your/venv/bin/python /path/to/your/project/diff_scraper.py
0 */2 * * * /path/to/your/venv/bin/python /path/to/your/project/summarize_changes.py
```

**Note:** Make sure to replace `/path/to/your/venv/bin/python` and `/path/to/your/project/` with the absolute paths to your virtual environment's Python executable and your project directory, respectively.

## Dependencies

The project's dependencies are listed in the `requirements.txt` file. They include:

*   Flask
*   Gunicorn
*   Beautiful Soup
*   google-generativeai
*   and others...
