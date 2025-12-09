import time
import re
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st


# =======================
# LOW-LEVEL HELPERS
# =======================

def is_internal_url(url: str, root_domain: str) -> bool:
    try:
        parsed = urlparse(url)
        # Internal if same domain or relative
        return parsed.netloc == "" or parsed.netloc == root_domain
    except Exception:
        return False


EXCLUDE_PATTERNS = [
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".css", ".js", ".ico",
    "/wp-admin", "/wp-login",
    "mailto:", "tel:",
]


def should_skip_url(url: str) -> bool:
    lower = url.lower()
    return any(pat in lower for pat in EXCLUDE_PATTERNS)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "BrandScannerBot/1.0 (+https://example.com)"}
    )
    return session


def fetch_html(session: requests.Session, url: str, timeout: int = 10) -> str | None:
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        if "text/html" not in resp.headers.get("Content-Type", ""):
            return None
        return resp.text
    except requests.RequestException:
        return None


def get_urls_from_sitemap(session: requests.Session, sitemap_url: str) -> set[str]:
    urls = set()
    try:
        resp = session.get(sitemap_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")

        # Sitemap index case
        sitemap_tags = soup.find_all("sitemap")
        if sitemap_tags:
            for sm in sitemap_tags:
                loc = sm.find("loc")
                if loc and loc.text:
                    urls.update(get_urls_from_sitemap(session, loc.text.strip()))
            return urls

        # Regular URL sitemap
        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")
            if loc and loc.text:
                urls.add(loc.text.strip())
    except Exception as e:
        st.warning(f"Error reading sitemap {sitemap_url}: {e}")

    return urls


def crawl_site(session: requests.Session, start_url: str, max_pages: int, delay: float) -> set[str]:
    root_domain = urlparse(start_url).netloc
    to_visit = [start_url]
    seen = set()
    all_urls = set()

    progress_text = st.empty()
    progress_bar = st.progress(0.0)

    while to_visit and len(seen) < max_pages:
        current = to_visit.pop(0)
        if current in seen:
            continue
        seen.add(current)

        if should_skip_url(current):
            continue

        progress_text.text(f"Crawling {len(seen)}/{max_pages}: {current}")
        progress_bar.progress(len(seen) / max_pages)

        html = fetch_html(session, current)
        if not html:
            continue

        all_urls.add(current)

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#"):
                continue

            absolute = urljoin(current, href)
            if not is_internal_url(absolute, root_domain):
                continue

            parsed = urlparse(absolute)
            normalized = parsed._replace(fragment="").geturl()

            if normalized not in seen and not should_skip_url(normalized):
                to_visit.append(normalized)

        time.sleep(delay)

    progress_text.empty()
    progress_bar.empty()

    return all_urls


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # remove non-visible stuff
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def search_terms_in_text(text: str, terms: list[str]) -> dict[str, list[str]]:
    """
    Returns dict: term -> list of snippets.
    """
    results = defaultdict(list)
    lower_text = text.lower()

    for term in terms:
        lower_term = term.lower()
        start = 0
        while True:
            idx = lower_text.find(lower_term, start)
            if idx == -1:
                break
            snippet_start = max(0, idx - 60)
            snippet_end = min(len(text), idx + len(term) + 60)
            snippet = text[snippet_start:snippet_end].strip()
            results[term].append(snippet)
            start = idx + len(lower_term)

    return results


# =======================
# STREAMLIT APP
# =======================

def main():
    st.title("🔍 Site Brand-Term Scanner")

    st.markdown(
        "Paste a site URL and the brand terms/placeholders you want to check for. "
        "The tool will scan pages and show where those terms are still present."
        "<span style='color:red; font-weight:bold; font-size:18px;'>Check off 'Also crawl internal links' if you want the tool to search beyond the site map.</span>"
    )

    # --- Inputs ---
    root_url = st.text_input(
        "Site root URL",
        value="https://www.example.com",
        help="For example: https://www.bestpickreports.com",
    )

    terms_text = st.text_area(
        "Search terms (one per line)",
        value="Old Brand Name\nTemplateBrand\nPLACEHOLDER_BRAND",
        help="Each line will be searched separately, case-insensitive.",
        height=120,
    )

    col1, col2 = st.columns(2)
    with col1:
        use_sitemap = st.checkbox("Use sitemap.xml", value=True)
    with col2:
        use_crawler = st.checkbox("Also crawl internal links", value=False)

    custom_sitemap = st.text_input(
        "Custom sitemap URL (optional)",
        value="",
        help="If blank, will default to [root_url]/sitemap.xml when sitemap is enabled.",
    )

    crawl_limit = 0
    crawl_delay = 0.2
    if use_crawler:
        crawl_limit = st.slider(
            "Max pages to crawl",
            min_value=10,
            max_value=2000,
            value=200,
            step=10,
            help="Safety limit to avoid crawling too many pages.",
        )
        crawl_delay = st.slider(
            "Delay between crawl requests (seconds)",
            min_value=0.0,
            max_value=1.0,
            value=0.2,
            step=0.1,
        )

    run_button = st.button("Run scan")

    if not run_button:
        return

    # --- Validate inputs ---
    if not root_url.strip():
        st.error("Please enter a site root URL.")
        return

    terms = [t.strip() for t in terms_text.splitlines() if t.strip()]
    if not terms:
        st.error("Please enter at least one search term.")
        return

    session = make_session()
    all_urls = set()

    # --- Collect URLs from sitemap ---
    if use_sitemap:
        if custom_sitemap.strip():
            sitemap_url = custom_sitemap.strip()
        else:
            # Simple default: root_url + /sitemap.xml
            if not root_url.endswith("/"):
                sitemap_url = root_url + "/sitemap.xml"
            else:
                sitemap_url = root_url.rstrip("/") + "/sitemap.xml"

        st.info(f"Fetching URLs from sitemap: {sitemap_url}")
        sitemap_urls = get_urls_from_sitemap(session, sitemap_url)
        st.write(f"Found **{len(sitemap_urls)}** URLs in sitemap.")
        all_urls.update(sitemap_urls)

    # --- Collect URLs via crawl ---
    if use_crawler:
        st.info(f"Crawling up to {crawl_limit} pages from {root_url} ...")
        crawled_urls = crawl_site(
            session=session,
            start_url=root_url,
            max_pages=crawl_limit,
            delay=crawl_delay,
        )
        st.write(f"Crawled **{len(crawled_urls)}** URLs.")
        all_urls.update(crawled_urls)

    if not all_urls:
        st.warning("No URLs collected. Check your URL and sitemap settings.")
        return

    st.success(f"Total unique URLs to scan: **{len(all_urls)}**")

    # --- Scan pages for terms ---
    results_rows = []
    scan_progress = st.progress(0.0)
    status_text = st.empty()

    for i, url in enumerate(sorted(all_urls)):
        status_text.text(f"Scanning {i + 1}/{len(all_urls)}: {url}")
        scan_progress.progress((i + 1) / len(all_urls))

        html = fetch_html(session, url)
        if not html:
            continue

        text = extract_visible_text(html)
        matches = search_terms_in_text(text, terms)

        for term, snippets in matches.items():
            for snippet in snippets:
                results_rows.append(
                    {
                        "url": url,
                        "term": term,
                        "snippet": snippet,
                    }
                )

        # Small delay mainly for crawled URLs; sitemap-only runs will still respect it
        time.sleep(0.1)

    scan_progress.empty()
    status_text.empty()

    # --- Show results ---
    if results_rows:
        df = pd.DataFrame(results_rows)
        st.subheader("Matches found")
        st.write(
            f"Found **{len(df)}** matches across **{df['url'].nunique()}** pages."
        )

        st.dataframe(df, use_container_width=True)

        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download results as CSV",
            data=csv_data,
            file_name="brand_term_scan_results.csv",
            mime="text/csv",
        )
    else:
        st.success("✅ No search terms were found on the scanned pages.")


if __name__ == "__main__":
    main()
