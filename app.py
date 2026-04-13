import io
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin

import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Font


# ─── Install Playwright browser on first run ────────────────────────────────
@st.cache_resource(show_spinner="Setting up browser (first run only, ~30s)…")
def _install_playwright():
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, check=False
    )
    subprocess.run(
        [sys.executable, "-m", "playwright", "install-deps", "chromium"],
        capture_output=True, check=False
    )


_install_playwright()

from playwright.sync_api import sync_playwright  # noqa: E402


# ─── Data model ─────────────────────────────────────────────────────────────
@dataclass
class JobRow:
    role: str
    location: str
    title: str
    company: str
    posted: str
    job_url: str
    hiring_name: str
    hiring_link: str
    source_page: int


# ─── Scraping helpers (ported from original script) ─────────────────────────
def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def safe_text(locator, timeout: int = 1500) -> str:
    try:
        return clean_text(locator.first.inner_text(timeout=timeout))
    except Exception:
        return ""


def safe_attr(locator, attr: str, timeout: int = 1500) -> str:
    try:
        return (locator.first.get_attribute(attr, timeout=timeout) or "").strip()
    except Exception:
        return ""


def build_search_url(role: str, location: str, start: int = 0) -> str:
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(role)}&location={quote_plus(location)}"
    )
    if start > 0:
        url += f"&start={start}"
    return url


def detect_jobs_list(page):
    for selector in [
        "ul.scaffold-layout__list-container",
        "ul.jobs-search__results-list",
        "div.jobs-search-results-list",
        "div.scaffold-layout__list",
    ]:
        try:
            loc = page.locator(selector).first
            loc.wait_for(timeout=6000)
            return loc
        except Exception:
            continue
    return None


def get_job_cards(page):
    for selector in [
        "li:has(a.job-card-list__title)",
        "li:has(a.job-card-container__link)",
        "div.job-card-container",
    ]:
        loc = page.locator(selector)
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def extract_posted(page) -> str:
    for selector in [
        "span.jobs-unified-top-card__posted-date",
        "div.job-details-jobs-unified-top-card__tertiary-description-container span",
        "span.posted-time-ago__text",
    ]:
        text = safe_text(page.locator(selector))
        if text:
            return text
    for selector in [
        "div.job-details-jobs-unified-top-card__primary-description-container",
        "div.job-details-jobs-unified-top-card__tertiary-description-container",
        "div.topcard__flavor-row",
    ]:
        text = safe_text(page.locator(selector))
        if text and "·" in text:
            parts = [p.strip() for p in text.split("·")]
            if len(parts) > 1:
                return parts[-1]
    return ""


def extract_hiring_contact(page) -> Tuple[str, str]:
    for container_selector in [
        "div.jobs-poster__container",
        "div.jobs-poster",
        "section:has-text('Meet the hiring team')",
        "section:has-text('Job poster')",
    ]:
        try:
            container = page.locator(container_selector).first
            if container.count() == 0:
                continue
            for link_selector in ["a[href*='/in/']", "a[href*='/recruiter/']"]:
                anchor = container.locator(link_selector).first
                name = safe_text(anchor)
                href = safe_attr(anchor, "href")
                if name and href:
                    return name, href
        except Exception:
            continue
    try:
        anchors = page.locator("a[href*='/in/']")
        for i in range(min(anchors.count(), 20)):
            a = anchors.nth(i)
            name = clean_text(a.inner_text(timeout=1000))
            href = (a.get_attribute("href", timeout=1000) or "").strip()
            if name and href and len(name.split()) <= 5:
                return name, href
    except Exception:
        pass
    return "", ""


def click_next_page(page, current_page_number: int) -> bool:
    n = current_page_number + 1
    for selector in [
        f"button[aria-label='Page {n}']",
        f"button[aria-label='Page {n} of results']",
        f"button:has-text('{n}')",
        "button[aria-label='View next page']",
        "button[aria-label='Next']",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.count() == 0:
                continue
            btn.scroll_into_view_if_needed(timeout=2000)
            btn.click(timeout=3000)
            page.wait_for_timeout(2500)
            return True
        except Exception:
            continue
    return False


def extract_job_from_card(page, card, role, location, source_page) -> Optional[JobRow]:
    try:
        card.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(300)

        title, job_url = "", ""
        for selector in [
            "a.job-card-list__title",
            "a.job-card-container__link",
            "a[href*='/jobs/view/']",
        ]:
            anchor = card.locator(selector).first
            title = safe_text(anchor)
            job_url = safe_attr(anchor, "href")
            if title or job_url:
                break

        if not title:
            title = safe_text(card.locator("a").first)
        if not job_url:
            job_url = safe_attr(card.locator("a[href*='/jobs/view/']").first, "href")

        company = ""
        for selector in [
            ".job-card-container__company-name",
            ".artdeco-entity-lockup__subtitle",
            ".job-card-container__primary-description",
        ]:
            company = safe_text(card.locator(selector).first)
            if company:
                break

        try:
            card.click(timeout=2500)
        except Exception:
            try:
                card.locator("a").first.click(timeout=2500)
            except Exception:
                pass

        page.wait_for_timeout(2500)

        if not job_url:
            job_url = page.url if "/jobs/view/" in page.url else ""

        posted = extract_posted(page)
        hiring_name, hiring_link = extract_hiring_contact(page)

        if job_url and job_url.startswith("/"):
            job_url = urljoin("https://www.linkedin.com", job_url)
        if hiring_link and hiring_link.startswith("/"):
            hiring_link = urljoin("https://www.linkedin.com", hiring_link)

        if not title and not company and not job_url:
            return None

        return JobRow(
            role=role, location=location, title=title, company=company,
            posted=posted, job_url=job_url, hiring_name=hiring_name,
            hiring_link=hiring_link, source_page=source_page,
        )
    except Exception:
        return None


# ─── Excel export ────────────────────────────────────────────────────────────
def jobs_to_excel_bytes(jobs: List[JobRow]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    headers = [
        "Role", "Location", "Title", "Company", "Posted",
        "Job Link", "Hiring Manager / HR", "Hiring Profile Link",
        "Source Page", "Applied", "Notes",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    seen: Set[str] = set()
    for job in jobs:
        if job.job_url in seen:
            continue
        ws.append([
            job.role, job.location, job.title, job.company, job.posted,
            job.job_url, job.hiring_name, job.hiring_link, job.source_page, "", "",
        ])
        seen.add(job.job_url)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Auth ────────────────────────────────────────────────────────────────────
def is_logged_in(page) -> bool:
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        if any(x in page.url.lower() for x in ["login", "checkpoint", "challenge"]):
            return False
        return any(
            page.locator(s).count() > 0
            for s in ["input[placeholder*='Search']", "a[href*='/jobs/']", "a[href*='/mynetwork/']"]
        ) or "feed" in page.url.lower()
    except Exception:
        return False


# ─── Main scraper ─────────────────────────────────────────────────────────────
def run_scraper(
    email: str, password: str,
    roles: List[str], locations: List[str],
    max_jobs: int, max_pages: int,
    log_fn,
) -> List[JobRow]:
    all_jobs: List[JobRow] = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            "/tmp/linkedin_playwright_profile",
            headless=True,
            viewport={"width": 1440, "height": 1000},
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.new_page()

        if not is_logged_in(page):
            if not email or not password:
                log_fn("❌ Missing LinkedIn credentials.")
                context.close()
                return all_jobs
            log_fn("🔑 Logging into LinkedIn…")
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            page.locator("#username").fill(email)
            page.locator("#password").fill(password)
            page.locator("button[type='submit']").click()
            page.wait_for_timeout(4000)

            if any(x in page.url.lower() for x in ["checkpoint", "challenge"]):
                log_fn(
                    "⚠️ LinkedIn requires additional verification (e.g. CAPTCHA or 2FA). "
                    "Please log in manually on linkedin.com first, then try again."
                )
                context.close()
                return all_jobs

            if not is_logged_in(page):
                log_fn("❌ Login failed. Please check your email and password.")
                context.close()
                return all_jobs

        log_fn("✅ Logged in successfully!")

        for role in roles:
            for location in locations:
                current_page = 1
                start_offset = 0
                role_jobs: List[JobRow] = []

                while current_page <= max_pages and len(role_jobs) < max_jobs:
                    search_url = build_search_url(role, location, start=start_offset)
                    log_fn(f"🔍 **{role}** in **{location}** — Page {current_page}")
                    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)

                    jobs_list = detect_jobs_list(page)
                    if jobs_list is None:
                        log_fn("⚠️ Could not find jobs list. Skipping this page.")
                        break

                    for _ in range(3):
                        try:
                            jobs_list.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
                        except Exception:
                            page.mouse.wheel(0, 2200)
                        page.wait_for_timeout(700)

                    cards = get_job_cards(page)
                    if cards is None:
                        log_fn("⚠️ No job cards found on this page.")
                        break

                    to_process = min(cards.count(), max_jobs - len(role_jobs))
                    log_fn(f"📋 Processing up to {to_process} jobs…")

                    for i in range(to_process):
                        row = extract_job_from_card(page, cards.nth(i), role, location, current_page)
                        if row and row.job_url:
                            role_jobs.append(row)
                            log_fn(
                                f"&nbsp;&nbsp;[{len(role_jobs)}] {row.title} "
                                f"@ {row.company}"
                                + (f" · {row.hiring_name}" if row.hiring_name else "")
                            )
                        if len(role_jobs) >= max_jobs:
                            break

                    if len(role_jobs) >= max_jobs:
                        break

                    click_next_page(page, current_page)
                    current_page += 1
                    start_offset += 25

                all_jobs.extend(role_jobs)
                log_fn(f"✅ {len(role_jobs)} jobs found for **{role}** in **{location}**.\n")

        context.close()

    return all_jobs


# ─── Streamlit UI ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LinkedIn Job Search",
    page_icon="💼",
    layout="centered",
)

st.title("💼 LinkedIn Job Search")
st.caption("Search LinkedIn jobs across multiple roles and locations, then download the results as Excel.")

with st.expander("ℹ️ How it works", expanded=False):
    st.markdown(
        """
        1. Enter your LinkedIn email and password (used only to log in — nothing is stored).
        2. Type the job roles and locations you want to search.
        3. Hit **Start Search** and wait for the results.
        4. Download your personalised Excel spreadsheet.

        > **Note:** Use this tool responsibly and in accordance with
        > [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement).
        """
    )

with st.form("search_form"):
    st.subheader("🔐 LinkedIn Login")
    col1, col2 = st.columns(2)
    with col1:
        email = st.text_input("Email", placeholder="you@example.com")
    with col2:
        password = st.text_input("Password", type="password", placeholder="••••••••")

    st.subheader("🎯 Search Settings")
    roles_input = st.text_input(
        "Job Roles",
        placeholder="Software Engineer, Product Manager, Data Analyst",
        help="Separate multiple roles with commas.",
    )
    locations_input = st.text_input(
        "Locations",
        placeholder="San Francisco CA, New York NY, Remote",
        help="Separate multiple locations with commas.",
    )

    col3, col4 = st.columns(2)
    with col3:
        max_jobs = st.number_input("Max jobs per search", min_value=5, max_value=100, value=30, step=5)
    with col4:
        max_pages = st.number_input("Max pages per search", min_value=1, max_value=10, value=3, step=1)

    submitted = st.form_submit_button("🚀 Start Search", use_container_width=True, type="primary")

if submitted:
    roles = [r.strip() for r in roles_input.split(",") if r.strip()]
    locations = [loc.strip() for loc in locations_input.split(",") if loc.strip()]

    if not email or not password:
        st.error("Please enter your LinkedIn email and password.")
    elif not roles:
        st.error("Please enter at least one job role.")
    elif not locations:
        st.error("Please enter at least one location.")
    else:
        log_placeholder = st.empty()
        log_lines: List[str] = []

        def log_fn(msg: str):
            log_lines.append(msg)
            log_placeholder.markdown(
                "<div style='background:#f8f9fa;border-radius:8px;padding:12px 16px;"
                "font-size:0.88rem;line-height:1.7;max-height:380px;overflow-y:auto'>"
                + "<br>".join(log_lines)
                + "</div>",
                unsafe_allow_html=True,
            )

        with st.spinner("Running search — this may take a few minutes…"):
            jobs = run_scraper(
                email, password, roles, locations,
                int(max_jobs), int(max_pages), log_fn,
            )

        if jobs:
            log_fn(f"<br>🎉 <strong>Done! {len(jobs)} unique jobs collected.</strong>")
            excel_bytes = jobs_to_excel_bytes(jobs)
            st.success(f"Search complete — **{len(jobs)} jobs** found!")
            st.download_button(
                label="📥 Download Excel Results",
                data=excel_bytes,
                file_name="linkedin_jobs.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.warning(
                "No jobs found. Double-check your credentials, roles, and locations, then try again."
            )
