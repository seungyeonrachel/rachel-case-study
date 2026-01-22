# scraper.py
# PartSelect “real scraping” module:
# - Robust fetch with requests.Session + headless Selenium fallback on 403 / bot blocks
# - PS number lookup via Search.ashx redirect -> canonical SEO slug page
# - Part page parsing: name, price, MPN, image, product description, installation, troubleshooting, etc.
# - Model compatibility: checks whether PS appears in the *Parts* block of the model page
# - Model symptoms: scrapes the "Common Symptoms" list and symptom pages to list fixing parts
#
# Enable debug logs:
#   SCRAPER_DEBUG=1
# Optional: save HTML dumps:
#   SCRAPER_SAVE_HTML=1  (default dir: /tmp, configurable with SCRAPER_SAVE_DIR)

from __future__ import annotations

import hashlib
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
import sys

BASE = "https://www.partselect.com/"

DEBUG = os.getenv("SCRAPER_DEBUG", "0") == "1"
SAVE_HTML = os.getenv("SCRAPER_SAVE_HTML", "0") == "1"
SAVE_DIR = os.getenv("SCRAPER_SAVE_DIR", "/tmp")


def dprint(*args):
    if DEBUG:
        print("[SCRAPER]", *args, file=sys.stderr, flush=True)


def _save_html(tag: str, url: str, html: str) -> None:
    if not SAVE_HTML:
        return
    try:
        h = hashlib.md5((url or "").encode("utf-8")).hexdigest()[:10]
        fname = f"partselect_{tag}_{h}.html"
        path = os.path.join(SAVE_DIR, fname)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(html or "")
        dprint("saved html:", path)
    except Exception as e:
        dprint("save html failed:", repr(e))


# -----------------------------
# TTL cache
# -----------------------------
class TTLCache:
    def __init__(self, ttl_seconds: int = 15 * 60):
        self.ttl = ttl_seconds
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        item = self._store.get(key)
        if not item:
            return None
        ts, val = item
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)


_html_cache = TTLCache(ttl_seconds=15 * 60)
_final_url_cache = TTLCache(ttl_seconds=15 * 60)


# -----------------------------
# Requests session
# -----------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE,
}

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)


# -----------------------------
# Selenium fallback
# -----------------------------
# -----------------------------
# Selenium fallback (undetected_chromedriver)
# -----------------------------
_selenium_driver = None

def _get_selenium_driver():
    """
    Lazy-init ONE driver and reuse it.
    Use SCRAPER_HEADLESS=0 to run non-headless (recommended for Akamai).
    Use SCRAPER_CHROME_PROFILE to persist cookies/session.
    """
    global _selenium_driver
    if _selenium_driver is not None:
        return _selenium_driver

    import undetected_chromedriver as uc
    headless = os.getenv("SCRAPER_HEADLESS", "1") == "1"
    profile_dir = os.getenv("SCRAPER_CHROME_PROFILE", "/tmp/partselect_uc_profile")

    options = uc.ChromeOptions()

    # ✅ Prefer headed for Akamai
    if headless:
        # UC headless works sometimes, but Akamai often kills it
        options.add_argument("--headless=new")

    # ✅ Persist cookies / localStorage
    options.add_argument(f"--user-data-dir={profile_dir}")

    options.add_argument("--window-size=1400,900")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=en-US,en")

    # keep your UA
    options.add_argument(f'--user-agent={DEFAULT_HEADERS["User-Agent"]}')

    driver = uc.Chrome(options=options, use_subprocess=True)
    _selenium_driver = driver
    dprint("uc driver initialized", "headless=", headless, "profile=", profile_dir)
    return driver


def _jitter_sleep(base: float = 0.8) -> None:
    time.sleep(base + random.random())


# -----------------------------
# Blocked / bot detection
# -----------------------------
def is_blocked_page(html: str) -> bool:
    if not html:
        return True
    low = html.lower()
    needles = [
        "access denied",
        "captcha",
        "verify you are a human",
        "unusual traffic",
        "enable javascript",
        "/cdn-cgi/",
        "cloudflare",
        "datadome",
        "incapsula",
        "akamai",
        "request blocked",
        "bot detection",
        "security check",
        "please enable cookies",
        "forbidden",
        "temporarily unavailable",
    ]
    return any(n in low for n in needles)


def is_not_found_page(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    h1_el = soup.select_one("h1")
    h1 = (h1_el.get_text(" ", strip=True) if h1_el else "").lower()

    if "page not found" in title:
        return True
    if h1.strip() == "page not found":
        return True
    if soup.select_one(".nf__part") or soup.select_one(".not-found") or soup.select_one("#notFound"):
        return True
    return False


# -----------------------------
# Text helpers
# -----------------------------
def _abs(page_url: str, href: str) -> str:
    return urljoin(page_url, href)


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s2 = re.sub(r"\s+", " ", s).strip()
    return s2 or None


def _text(el) -> Optional[str]:
    if not el:
        return None
    return _clean(el.get_text(" ", strip=True))

def infer_appliance_type_from_breadcrumbs(soup: BeautifulSoup) -> Optional[str]:
    """
    PartSelect breadcrumbs reliably contain the appliance category for valid pages.
    Returns: "refrigerator", "dishwasher", or None.
    """
    if not soup:
        return None

    bc = soup.select_one("ol.breadcrumbs")
    if not bc:
        return None

    for a in bc.select("a"):
        txt = (a.get_text(strip=True) or "").lower()
        if txt == "refrigerator":
            return "refrigerator"
        if txt == "dishwasher":
            return "dishwasher"

    return None



def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _first_regex(patterns: List[re.Pattern], haystack: str) -> Optional[str]:
    for p in patterns:
        m = p.search(haystack)
        if not m:
            continue
        try:
            return m.group(1).strip()
        except IndexError:
            return m.group(0).strip()
    return None


def _extract_price(text: str) -> Optional[str]:
    m = re.search(r"(\$\s?\d{1,5}(?:,\d{3})*(?:\.\d{2})?)", text)
    return m.group(1).replace(" ", "") if m else None


# -----------------------------
# Fetching
# -----------------------------
def fetch_html_with_final_url(url: str, timeout: int = 20) -> Tuple[str, str]:
    cached_html = _html_cache.get(url)
    cached_final = _final_url_cache.get(url)
    if cached_html and cached_final:
        dprint("CACHE HIT:", url, "final:", cached_final, "len:", len(cached_html or ""))
        return cached_html, cached_final

    
    if "Search" in url:
    # Search pages are more aggressively blocked; go straight to Selenium
        try:
            driver = _get_selenium_driver()
            driver.set_page_load_timeout(timeout)
            dprint("SELENIUM (forced for search) GET:", url)
            driver.get(url)

            html = driver.page_source or ""
            final_url = driver.current_url
            
            # This will print to your terminal/console where the server is running
            print(f"\n[DEBUG] URL Attempted: {url}")
            print(f"[DEBUG] Final URL: {final_url}")
            
            if is_blocked_page(html):
                soup_debug = BeautifulSoup(html, "lxml")
                page_title = soup_debug.title.string if soup_debug.title else "No Title"
                print(f"!!! BLOCK DETECTED !!!")
                print(f"Page Title: {page_title}")
                # This helps identify if it's Cloudflare or a 403 error
                print(f"Snippet: {html[:500].strip()}")


            html = driver.page_source or ""
            final_url = driver.current_url
            if is_blocked_page(html):
                _save_html("blocked_selenium_search", final_url or url, html)
                return "", final_url or url
            _html_cache.set(url, html)
            _final_url_cache.set(url, final_url)
            return html, final_url
        except Exception as e:
            dprint("SELENIUM ERROR (forced search):", repr(e))
            # fall back to normal logic below


    # warm-up
    try:
        SESSION.get(BASE, timeout=timeout)
    except Exception as e:
        dprint("warmup failed:", repr(e))

    # requests attempts
    for attempt in range(2):
        try:
            if attempt > 0:
                _jitter_sleep(1.5)

            resp = SESSION.get(url, timeout=timeout, allow_redirects=True)

            dprint("REQUESTS GET:", url)
            dprint("  status:", resp.status_code)
            dprint("  final :", resp.url)
            dprint("  len   :", len(resp.text or ""))

            if resp.status_code == 403:
                dprint("  BLOCKED: 403 -> selenium")
                _save_html("blocked_requests_403", resp.url or url, resp.text or "")
                break

            resp.raise_for_status()
            html, final_url = resp.text, resp.url

            blocked = is_blocked_page(html)
            not_found = is_not_found_page(html)

            dprint("  blocked?:", blocked, "not_found?:", not_found)
            dprint("  head:", (html[:200] if html else "").replace("\n", " "))

            if blocked:
                _save_html("blocked_requests", final_url or url, html)
                break

            # soft-404s for search / ps
            if not_found and ("Search.ashx" in url or re.search(r"/PS\d+", url, re.I)):
                _save_html("soft404_requests", final_url or url, html)
                break

            _html_cache.set(url, html)
            _final_url_cache.set(url, final_url)
            _save_html("ok_requests", final_url or url, html)
            return html, final_url

        except Exception as e:
            dprint("REQUESTS ERROR:", repr(e))
            _jitter_sleep(1.0)

    # selenium fallback
    try:
        driver = _get_selenium_driver()
        driver.set_page_load_timeout(timeout)

        dprint("SELENIUM GET:", url)
        driver.get(url)

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        # Wait for *part page-ish* markers
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "h1, [data-testid='product-title'], .product-title, .pd__img, .nf__part")
                )
            )
        except Exception as e:
            dprint("SELENIUM wait failed:", repr(e), "(continuing)")

        html = driver.page_source or ""
        final_url = driver.current_url

        dprint("  selenium final:", final_url)
        dprint("  selenium len  :", len(html))
        dprint("  selenium blocked?:", is_blocked_page(html), "not_found?:", is_not_found_page(html))

        if is_blocked_page(html):
            _save_html("blocked_selenium", final_url or url, html)
            return "", final_url or url

        _html_cache.set(url, html)
        _final_url_cache.set(url, final_url)
        _save_html("ok_selenium", final_url or url, html)
        return html, final_url

    except Exception as e:
        dprint("SELENIUM ERROR:", repr(e))
        return "", url


def fetch_html(url: str, timeout: int = 20) -> str:
    html, _ = fetch_html_with_final_url(url, timeout=timeout)
    return html


# -----------------------------
# Section extraction
# -----------------------------
def _find_section_root_by_id_or_heading(
    soup: BeautifulSoup,
    section_id_candidates: List[str],
    heading_regex: re.Pattern,
    ascend_levels: int = 7,
) -> Optional[Any]:
    """
    Try:
      1) exact id match (e.g., id="Parts", id="QuestionsAndAnswers")
      2) find a heading string and walk up to a container
    """
    # 1) id match
    for sid in section_id_candidates:
        node = soup.find(id=sid)
        if node:
            return node

    # 2) heading match
    header = soup.find(string=heading_regex)
    if header:
        node = header.parent
        for _ in range(ascend_levels):
            if node and getattr(node, "parent", None):
                node = node.parent
        return node

    return None


def _extract_ps_links_from_root(root: Any, limit: int = 200) -> List[Dict[str, str]]:
    """
    Extract PS links from an element root.
    Returns [{ps, title, url}]
    """
    if root is None:
        return []

    parts: List[Dict[str, str]] = []
    for a in root.select("a[href*='/PS']"):
        href = a.get("href") or ""
        m = re.search(r"/(PS\d{6,})-", href, re.I)
        if not m:
            continue
        ps = m.group(1).upper()
        title = _clean(a.get_text(" ", strip=True))

        # If anchor text is empty/short, expand to nearby text
        if not title or len(title) < 4:
            parent = a.parent
            if parent:
                title = _clean(parent.get_text(" ", strip=True))

        parts.append({"ps": ps, "title": (title or "")[:180], "url": urljoin(BASE, href)})

        if len(parts) >= limit:
            break

    # de-dupe by ps
    seen = set()
    out = []
    for p in parts:
        if p["ps"] in seen:
            continue
        seen.add(p["ps"])
        out.append(p)
    return out


def _get_section_root_by_jump(soup: BeautifulSoup, label: str) -> Optional[Any]:
    target = _norm_label(label)

    # jump-to nav links first
    for a in soup.select("a[href^='#']"):
        txt = _norm_label(a.get_text(" ", strip=True))
        if txt == target:
            frag = (a.get("href") or "").lstrip("#")
            if frag:
                el = soup.find(id=frag)
                if el:
                    return el

    # fallback: headings
    for h in soup.find_all(["h2", "h3", "h4"]):
        ht = _norm_label(h.get_text(" ", strip=True))
        if ht == target:
            return h
    return None


def _extract_section_text(section_root: Any, max_chars: int = 3500) -> Optional[str]:
    if not section_root:
        return None

    start = section_root
    if getattr(start, "name", None) in ("h2", "h3", "h4"):
        start = start.find_next(["div", "section", "article", "p", "ul", "ol"]) or start

    chunks: List[str] = []
    node = start
    for _ in range(25):
        if not node:
            break
        if node is not start and getattr(node, "name", None) in ("h2", "h3"):
            break
        if getattr(node, "get_text", None):
            t = _clean(node.get_text(" ", strip=True))
            if t:
                chunks.append(t)
        node = node.find_next_sibling()

    text = _clean(" ".join(chunks))
    return text[:max_chars] if text else None

def list_parts_for_model(model_number: str, keyword: Optional[str] = None, timeout: int = 20) -> Tuple[Dict[str, Any], Source]:
    """
    Scrape the model page and extract PS part links from the #Parts section.
    Returns payload + Source, consistent with your other APIs.
    """
    model_number = (model_number or "").upper()
    url = f"{BASE}Models/{model_number}/"
    html, final_url = fetch_html_with_final_url(url, timeout=timeout)
    blocked = is_blocked_page(html)

    if not html or blocked:
        return (
            {
                "model_number": model_number,
                "model_url": final_url or url,
                "appliance_type": None,
                "blocked": bool(blocked),
                "parts": [],
            },
            Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
        )

    soup = BeautifulSoup(html, "lxml")
    appliance_type = infer_appliance_type_from_breadcrumbs(soup)


    # Prefer the explicit Parts section root
    root = _find_section_root_by_id_or_heading(
        soup,
        section_id_candidates=["Parts", "parts", "ModelParts", "modelParts", "partsSection"],
        heading_regex=re.compile(r"\bParts\b", re.I),
        ascend_levels=9,
    )

    parts = _extract_ps_links_from_root(root or soup, limit=400)

    if keyword:
        kw = keyword.strip().lower()
        parts = [p for p in parts if kw in (p.get("title") or "").lower()]

    return (
        {
            "model_number": model_number,
            "model_url": final_url or url,
            "appliance_type": appliance_type,
            "blocked": False,
            "parts": parts,
        },
        Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
    )


def list_qna_for_model(model_number: str, keyword: Optional[str] = None, limit: int = 10, timeout: int = 20) -> Tuple[Dict[str, Any], Source]:
    """
    Scrape the model page and extract Q&A from the #QuestionsAndAnswers section.
    Returns payload + Source, consistent with your other APIs.
    """
    model_number = (model_number or "").upper()
    url = f"{BASE}Models/{model_number}/"
    html, final_url = fetch_html_with_final_url(url, timeout=timeout)
    blocked = is_blocked_page(html)

    if not html or blocked:
        return (
            {
                "model_number": model_number,
                "model_url": final_url or url,
                "blocked": bool(blocked),
                "appliance_type": None,
                "qna": [],
            },
            Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
        )

    soup = BeautifulSoup(html, "lxml")
    appliance_type = infer_appliance_type_from_breadcrumbs(soup)


    root = _find_section_root_by_id_or_heading(
        soup,
        section_id_candidates=["QuestionsAndAnswers", "questionsandanswers", "QandA", "qanda"],
        heading_regex=re.compile(r"questions\s*and\s*answers|q\s*&\s*a", re.I),
        ascend_levels=10,
    )

    qas = _extract_qna(root) if root else []
    # Convert dataclasses -> dict and apply keyword filter
    out: List[Dict[str, Any]] = []
    kw = keyword.strip().lower() if keyword else None

    for qa in qas:
        q = (qa.question or "").strip()
        a = (qa.answer or "").strip() if qa.answer else ""
        if not q:
            continue
        if kw and (kw not in q.lower() and kw not in a.lower()):
            continue
        out.append(asdict(qa))
        if len(out) >= limit:
            break

    return (
        {
            "model_number": model_number,
            "model_url": final_url or url,
            "appliance_type": appliance_type,
            "blocked": False,
            "qna": out,
        },
        Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
    )


def _extract_list_items(section_root: Any) -> List[str]:
    if not section_root:
        return []
    root = section_root
    if getattr(root, "name", None) in ("h2", "h3", "h4"):
        root = root.find_next(["div", "section", "article"]) or root

    items: List[str] = []
    for li in root.select("li"):
        t = _clean(li.get_text(" ", strip=True))
        if t and 5 <= len(t) <= 180:
            items.append(t)

    # de-dupe
    out = []
    for s in items:
        if s not in out:
            out.append(s)
    return out[:40]


def _extract_reviews(section_root: Any) -> List["ReviewItem"]:
    if not section_root:
        return []

    root = section_root
    if getattr(root, "name", None) in ("h2", "h3", "h4"):
        root = root.find_next(["div", "section", "article"]) or root

    reviews: List[ReviewItem] = []
    candidates = root.select("[itemprop='review'], .review, .ps-review, .customer-review, article")
    if not candidates:
        candidates = root.find_all(["div", "article"], limit=50)

    for c in candidates:
        text = _clean(c.get_text(" ", strip=True))
        if not text or len(text) < 40:
            continue

        rating = None
        m = re.search(r"(\d(?:\.\d)?)\s*/\s*5", text)
        if m:
            try:
                rating = float(m.group(1))
            except Exception:
                rating = None

        title = None
        h = c.find(["h3", "h4"])
        if h:
            title = _clean(h.get_text(" ", strip=True))

        paras = [p.get_text(" ", strip=True) for p in c.find_all(["p", "div"], limit=8)]
        paras = [_clean(p) for p in paras if _clean(p)]
        body = max(paras, key=lambda x: len(x)) if paras else text[:800]

        reviews.append(ReviewItem(rating=rating, title=title, body=body))
        if len(reviews) >= 8:
            break

    return reviews


def _extract_qna(section_root: Any) -> List["QAItem"]:
    if not section_root:
        return []

    root = section_root
    if getattr(root, "name", None) in ("h2", "h3", "h4"):
        root = root.find_next(["div", "section", "article"]) or root

    qas: List[QAItem] = []
    blocks = root.find_all(["div", "article", "li"], limit=120)
    for b in blocks:
        t = _clean(b.get_text(" ", strip=True))
        if not t:
            continue
        if "question" in t.lower() and "answer" in t.lower():
            qm = re.search(r"question[:\s]+(.+?)(?:answer[:\s]+|$)", t, re.I)
            am = re.search(r"answer[:\s]+(.+)$", t, re.I)
            q = _clean(qm.group(1)) if qm else None
            a = _clean(am.group(1)) if am else None
            if q:
                qas.append(QAItem(question=q, answer=a))
        if len(qas) >= 8:
            break
    return qas


def _extract_symptoms(soup: BeautifulSoup) -> Optional[List[str]]:
    symptoms: List[str] = []

    # explicit headings
    for heading in soup.find_all(["h2", "h3", "h4"]):
        ht = (heading.get_text(" ", strip=True) or "").lower()
        if any(k in ht for k in ["common problems", "symptoms", "this part fixes", "fixes these symptoms"]):
            ul = heading.find_next("ul")
            if ul:
                for li in ul.find_all("li"):
                    t = _clean(li.get_text(" ", strip=True))
                    if t and len(t) <= 120:
                        symptoms.append(t)

    if not symptoms:
        for a in soup.select('a[href*="Symptoms/"], a[href*="/Symptoms/"]'):
            t = _clean(a.get_text(" ", strip=True))
            if t and len(t) <= 120:
                symptoms.append(t)

    symptoms = [s for s in symptoms if s and "see more" not in s.lower()]
    symptoms = list(dict.fromkeys(symptoms))
    return symptoms or None


def _extract_image_url(soup: BeautifulSoup, url: str) -> Optional[str]:
    img = (
        soup.select_one('img[itemprop="image"]')
        or soup.select_one('[data-testid="product-image"] img')
        or soup.select_one(".product-image img")
        or soup.select_one(".gallery img")
    )
    if img and img.get("src"):
        return _abs(url, img["src"])

    for im in soup.select("img"):
        src = im.get("src")
        if not src:
            continue
        low = src.lower()
        if any(x in low for x in ["logo", "sprite", "icon", "data:image"]):
            continue
        return _abs(url, src)

    return None


# -----------------------------
# Types
# -----------------------------
@dataclass
class Source:
    title: str
    url: str


@dataclass
class SymptomItem:
    name: str
    url: str


@dataclass
class SymptomPart:
    name: Optional[str] = None
    ps_number: Optional[str] = None
    mpn: Optional[str] = None
    price: Optional[str] = None
    fix_rate: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ReviewItem:
    rating: Optional[float] = None
    title: Optional[str] = None
    body: Optional[str] = None


@dataclass
class StoryItem:
    title: Optional[str] = None
    body: Optional[str] = None


@dataclass
class QAItem:
    question: str
    answer: Optional[str] = None


@dataclass
class PartBundle:
    ps_number: str
    url: str
    appliance_type: Optional[str] = None
    name: Optional[str] = None
    price: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    image_url: Optional[str] = None

    # text sections
    product_description: Optional[str] = None
    installation: Optional[str] = None  # ✅ NEW: "Install Videos!" / "Installation" text
    description: Optional[str] = None
    troubleshooting: Optional[List[str]] = None
    symptoms: Optional[List[str]] = None
    customer_reviews: Optional[List[ReviewItem]] = None
    customer_repair_stories: Optional[List[StoryItem]] = None
    questions_and_answers: Optional[List[QAItem]] = None
    model_cross_reference: Optional[List[str]] = None

    # scrape status
    blocked: bool = False
    not_found: bool = False


# -----------------------------
# Core parsing
# -----------------------------
def parse_part_bundle(html: str, url: str, ps_number: str) -> PartBundle:
    ps_number = ps_number.upper()

    if not html:
        dprint("parse_part_bundle: empty html")
        return PartBundle(ps_number=ps_number, url=url, blocked=True)

    if is_blocked_page(html):
        dprint("parse_part_bundle: blocked page detected")
        return PartBundle(ps_number=ps_number, url=url, blocked=True)

    if is_not_found_page(html):
        dprint("parse_part_bundle: not found")
        return PartBundle(ps_number=ps_number, url=url, not_found=True)

    soup = BeautifulSoup(html, "lxml")
    appliance_type = infer_appliance_type_from_breadcrumbs(soup)

    # title
    title = (
        _text(soup.select_one("h1"))
        or _text(soup.select_one('[data-testid="product-title"]'))
        or _text(soup.select_one(".product-title"))
        or _text(soup.select_one(".title"))
    )

    # price
    price = (
        _text(soup.select_one('[itemprop="price"]'))
        or _text(soup.select_one('[data-testid="product-price"]'))
        or _text(soup.select_one(".price"))
        or _text(soup.select_one(".ps-price"))
    )
    if not price:
        price = _extract_price(soup.get_text(" ", strip=True))

    # mpn
    page_text = soup.get_text("\n", strip=True)
    mpn = _first_regex(
        [
            re.compile(r"Manufacturer Part Number[:\s]+([A-Z0-9\-]+)", re.I),
            re.compile(r"Mfr Part Number[:\s]+([A-Z0-9\-]+)", re.I),
            re.compile(r"Manufacturer Part No\.?[:\s]+([A-Z0-9\-]+)", re.I),
            re.compile(r"\bWPW?\d{7,10}\b", re.I),
        ],
        page_text,
    )
    mpn = _clean(mpn)

    # generic "description" blocks (sometimes separate from Product Description section)
    description = None
    for sel in (
        "#product-description",
        '[data-testid="product-description"]',
        ".product-description",
        "#description",
        ".description",
        ".product-details",
    ):
        description = _text(soup.select_one(sel))
        if description:
            break

    # jump-to extraction
    jump_labels = [_norm_label(a.get_text(" ", strip=True)) for a in soup.select("a[href^='#']")]
    dprint("jump-to labels sample:", jump_labels[:15])

    prod_root = _get_section_root_by_jump(soup, "Product Description")
    install_root = (
        _get_section_root_by_jump(soup, "Install Videos!")
        or _get_section_root_by_jump(soup, "Install Videos")
        or _get_section_root_by_jump(soup, "Installation")
        or _get_section_root_by_jump(soup, "Instructions")
    )
    trouble_root = _get_section_root_by_jump(soup, "Troubleshooting")
    reviews_root = _get_section_root_by_jump(soup, "Customer Reviews")
    stories_root = _get_section_root_by_jump(soup, "Customer Repair Stories")
    qna_root = _get_section_root_by_jump(soup, "Questions and Answers")
    model_xref_root = _get_section_root_by_jump(soup, "Model Cross Reference")

    product_description = _extract_section_text(prod_root, max_chars=3500)
    installation = _extract_section_text(install_root, max_chars=2500) if install_root else None
    troubleshooting = _extract_list_items(trouble_root) if trouble_root else []
    customer_reviews = _extract_reviews(reviews_root) if reviews_root else []
    qna = _extract_qna(qna_root) if qna_root else []
    model_xref = _extract_list_items(model_xref_root) if model_xref_root else []

    # repair stories: treat as a single blob if we can extract text
    repair_stories: List[StoryItem] = []
    if stories_root:
        story_text = _extract_section_text(stories_root, max_chars=4000)
        if story_text:
            repair_stories = [StoryItem(title="Customer repair stories", body=story_text)]

    symptoms = _extract_symptoms(soup)
    image_url = _extract_image_url(soup, url)

    # normalize empties -> None
    troubleshooting = troubleshooting or None
    customer_reviews = customer_reviews or None
    repair_stories = repair_stories or None
    qna = qna or None
    model_xref = model_xref or None

    dprint("bundle parsed:")
    dprint("  title:", title)
    dprint("  price:", price)
    dprint("  mpn:", mpn)
    dprint("  product_description_len:", len(product_description or ""))
    dprint("  installation_len:", len(installation or ""))
    dprint("  description_len:", len(description or ""))
    dprint("  troubleshooting:", len(troubleshooting or []))
    dprint("  reviews:", len(customer_reviews or []))
    dprint("  qna:", len(qna or []))
    dprint("  stories:", len(repair_stories or []))
    dprint("  symptoms:", len(symptoms or []))

    return PartBundle(
        ps_number=ps_number,
        url=url,
        appliance_type=appliance_type,
        name=title,
        price=price,
        manufacturer_part_number=mpn,
        image_url=image_url,
        product_description=product_description,
        installation=installation,
        description=description,
        troubleshooting=troubleshooting,
        symptoms=symptoms,
        customer_reviews=customer_reviews,
        customer_repair_stories=repair_stories,
        questions_and_answers=qna,
        model_cross_reference=model_xref,
        blocked=False,
        not_found=False,
    )


# -----------------------------
# Search resolution
# -----------------------------
def extract_first_search_result_url(html: str, base_url: str, ps_number: str) -> Optional[str]:
    if not html:
        return None
    ps_number = ps_number.upper()
    soup = BeautifulSoup(html, "lxml")

    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        text = (a.get_text(" ", strip=True) or "").upper()

        if "partselect.com" in href:
            full = href
        elif href.startswith("/"):
            full = _abs(base_url, href)
        else:
            continue

        if ps_number in full.upper() or ps_number in text:
            return full

    return None


def resolve_part_url(ps_number: str, timeout: int = 20) -> Tuple[str, str]:
    ps_number = ps_number.upper()

    cached = _final_url_cache.get(f"ps:{ps_number}")
    if cached:
        dprint("resolve_part_url cache hit:", ps_number, "->", cached)
        html = fetch_html(cached, timeout=timeout)
        return html, cached

    search_url = f"{BASE}Search.ashx?SearchTerm={quote_plus(ps_number)}"
    dprint("resolve_part_url search:", search_url)

    html, final_url = fetch_html_with_final_url(search_url, timeout=timeout)
    dprint("resolve_part_url final:", final_url, "len:", len(html or ""))

    if final_url and final_url != search_url and html and not is_blocked_page(html) and not is_not_found_page(html):
        _final_url_cache.set(f"ps:{ps_number}", final_url)
        return html, final_url

    first = extract_first_search_result_url(html, BASE, ps_number)
    dprint("resolve_part_url first result:", first)

    if first:
        html2, final2 = fetch_html_with_final_url(first, timeout=timeout)
        if final2 and html2 and not is_blocked_page(html2) and not is_not_found_page(html2):
            _final_url_cache.set(f"ps:{ps_number}", final2)
            return html2, final2

    return html, final_url or search_url


# -----------------------------
# Public API
# -----------------------------
def get_part_bundle(ps_number: str, timeout: int = 20) -> Tuple[PartBundle, Source]:
    ps_number = ps_number.upper()
    html, final_url = resolve_part_url(ps_number, timeout=timeout)
    url = final_url or f"{BASE}{ps_number}.htm"

    bundle = parse_part_bundle(html, url, ps_number)

    source_url = final_url or f"{BASE}Search.ashx?SearchTerm={quote_plus(ps_number)}"
    source_title = f"PartSelect part page ({ps_number})"
    if bundle.blocked:
        source_title = f"PartSelect (blocked) ({ps_number})"
    elif bundle.not_found:
        source_title = f"PartSelect (not found) ({ps_number})"

    return bundle, Source(title=source_title, url=source_url)


# -----------------------------
# Model + Symptom scraping helpers
# -----------------------------
_PS_RE = re.compile(r"\bPS\d{6,10}\b", re.I)


def _extract_model_parts_block(html: str) -> str:
    """
    Heuristic: slice the model overview page down to the "Parts for the <MODEL>"
    block to avoid false positives from Q&A / Symptoms / etc.
    """
    if not html:
        return ""
    h = html
    start_markers = [
        "Parts for the ",
        'id="parts"',
        "id='parts'",
        "# Parts for the",
    ]
    end_markers = [
        "Questions And Answers",
        "Questions & Answers",
        "Common Symptoms",
        "Videos related",
        "Instructions",
        "Back to Top",
    ]

    start = -1
    for m in start_markers:
        i = h.find(m)
        if i != -1:
            start = i
            break
    if start == -1:
        return h  # fallback

    end = -1
    for m in end_markers:
        j = h.find(m, start + 1)
        if j != -1:
            end = j
            break

    return h[start:end] if end != -1 else h[start:]


def _extract_ps_numbers(text: str) -> List[str]:
    if not text:
        return []
    return sorted(set(m.group(0).upper() for m in _PS_RE.finditer(text)))


def parse_model_common_symptoms(html: str, base_url: str, model_number: str) -> List[SymptomItem]:
    """
    Parse the 'Common Symptoms of the <MODEL>' section on the model overview page.
    Returns symptom names + absolute URLs.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    header = None
    for h in soup.find_all(["h2", "h3"]):
        t = (h.get_text(" ", strip=True) or "")
        if "Common Symptoms" in t and model_number.upper() in t.upper():
            header = h
            break
        if t.strip().lower().startswith("common symptoms"):
            header = h
            break

    items: List[SymptomItem] = []
    if header:
        node = header
        for _ in range(12):
            node = node.find_next()
            if not node:
                break
            if node.name in ("h2", "h3") and node is not header:
                break
            for a in node.find_all("a", href=True):
                name = a.get_text(" ", strip=True)
                href = a.get("href", "")
                if not name:
                    continue
                if "/Symptoms/" not in href:
                    continue

                # ✅ Clean symptom label noise PartSelect sometimes includes
                name = re.sub(r"\s*Fixed\s+by\s+these\s+parts.*$", "", name, flags=re.I).strip()
                name = re.sub(r"\s*Show\s+All\s*$", "", name, flags=re.I).strip()

                items.append(SymptomItem(name=name, url=urljoin(base_url, href)))

        dedup = {}
        for it in items:
            key = (it.name.strip(), it.url.strip())
            dedup[key] = it
        return list(dedup.values())

    for m in re.finditer(r'href="([^"]+/Symptoms/[^"]+/)"[^>]*>([^<]+)</a>', html, re.I):
        href, name = m.group(1), m.group(2)
        name = name.strip()
        name = re.sub(r"\s*Fixed\s+by\s+these\s+parts.*$", "", name, flags=re.I).strip()
        name = re.sub(r"\s*Show\s+All\s*$", "", name, flags=re.I).strip()
        items.append(SymptomItem(name=name, url=urljoin(base_url, href)))

    return items


def parse_symptom_page_parts(html: str, base_url: str) -> List[SymptomPart]:
    """
    Parse a model symptom page like /Models/<MODEL>/Symptoms/<slug>/ and return
    the parts that 'fix' that symptom.

    Robust parser:
    - Each part is in a block: div.symptoms__redesign
    - Extract: name/url, Fixes Symptom %, description, PS#, MPN, price
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    parts: List[SymptomPart] = []

    cards = soup.select("div.symptoms__redesign")
    if not cards:
        cards = soup.select("div.symptoms.d-flex")

    for card in cards:
        # name + url
        a = card.select_one(".symptoms__header a[href]")
        name = _clean(a.get_text(" ", strip=True)) if a else None
        link = urljoin(base_url, a["href"]) if a and a.get("href") else None

        # Fixes Symptom XX% of time
        fix_rate = None
        pct_text = (card.select_one(".symptoms__percent") or card).get_text(" ", strip=True)
        m_pct = re.search(r"Fixes\s+Symptom\s+(\d+)%\s+of\s+time", pct_text, re.I)
        if m_pct:
            fix_rate = f"Fixes Symptom {m_pct.group(1)}% of time"

        # description
        desc_el = card.select_one("p.mb-4")
        desc = _clean(desc_el.get_text(" ", strip=True)) if desc_el else None

        # PS number
        ps = None
        ps_el = card.select_one('[itemprop="productID"]')
        if ps_el:
            ps = _clean(ps_el.get_text(" ", strip=True))
        if not ps:
            m_ps = re.search(r"\bPS\d{6,10}\b", card.get_text(" ", strip=True), re.I)
            ps = m_ps.group(0).upper() if m_ps else None

        # MPN
        mpn = None
        mpn_el = card.select_one('[itemprop="mpn"]')
        if mpn_el:
            mpn = _clean(mpn_el.get_text(" ", strip=True))

        # price
        price = None
        price_el = card.select_one('[itemprop="price"]')
        if price_el:
            txt = _clean(price_el.get_text(" ", strip=True))
            if txt and "$" in txt:
                price = txt
            else:
                content = price_el.get("content")
                if content:
                    try:
                        price = f"${float(content):.2f}"
                    except Exception:
                        pass
        if not price:
            m_price = re.search(r"\$\s?\d+(?:\.\d{2})?", card.get_text(" ", strip=True))
            if m_price:
                price = m_price.group(0).replace(" ", "")

        if not (ps or name or link):
            continue

        parts.append(
            SymptomPart(
                name=name,
                ps_number=ps,
                mpn=mpn,
                price=price,
                fix_rate=fix_rate,
                url=link,
                description=desc,
            )
        )

    # de-dupe by PS
    dedup: Dict[str, SymptomPart] = {}
    for p in parts:
        if p.ps_number:
            dedup[p.ps_number] = p
    return list(dedup.values()) if dedup else parts



def get_model_symptoms(model_number: str, timeout: int = 20) -> Tuple[Dict[str, Any], Source]:
    model_number = (model_number or "").upper()
    url = f"{BASE}Models/{model_number}/"
    html, final_url = fetch_html_with_final_url(url, timeout=timeout)
    blocked = is_blocked_page(html)
    appliance_type = None
    if html and not blocked:
        soup = BeautifulSoup(html, "lxml")
        appliance_type = infer_appliance_type_from_breadcrumbs(soup)

    items = parse_model_common_symptoms(html, BASE, model_number) if html and not blocked else []

    return (
        {
            "model_number": model_number,
            "model_url": final_url or url,
            "appliance_type": appliance_type,
            "blocked": bool(blocked),
            "symptoms": [asdict(x) for x in items],
        },
        Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
    )


def get_symptom_fix_parts(model_number: str, symptom_url: str, timeout: int = 20) -> Tuple[Dict[str, Any], Source]:
    model_number = (model_number or "").upper()
    url = urljoin(BASE, symptom_url or "")
    html, final_url = fetch_html_with_final_url(url, timeout=timeout)
    blocked = is_blocked_page(html)
    parts = parse_symptom_page_parts(html, BASE) if html and not blocked else []

    return (
        {
            "model_number": model_number,
            "symptom_url": final_url or url,
            "blocked": bool(blocked),
            "parts": [asdict(p) for p in parts],
        },
        Source(title=f"PartSelect symptom page ({model_number})", url=final_url or url),
    )


def check_compatibility(model_number: str, ps_number: str, timeout: int = 20) -> Tuple[Dict[str, Any], Source]:
    model_number = (model_number or "").upper()
    ps_number = (ps_number or "").upper()
    url = f"{BASE}Models/{model_number}/"

    html, final_url = fetch_html_with_final_url(url, timeout=timeout)
    blocked = is_blocked_page(html)
    appliance_type = None
    if html and not blocked:
        soup = BeautifulSoup(html, "lxml")
        appliance_type = infer_appliance_type_from_breadcrumbs(soup)

    parts_block = _extract_model_parts_block(html or "")
    parts_ps = _extract_ps_numbers(parts_block)
    compatible = ps_number in parts_ps

    dprint("COMPAT CHECK:")
    dprint("  model:", model_number)
    dprint("  url:", final_url or url)
    dprint("  len:", len(html or ""))
    dprint("  blocked?:", blocked)
    dprint("  compatible?:", compatible)

    return (
        {
            "model_number": model_number,
            "ps_number": ps_number,
            "compatible": bool(compatible),
            "appliance_type": appliance_type,
            "blocked": bool(blocked),
            "model_url": final_url or url,
            "parts_count": len(parts_ps),
            "matched_in_parts": bool(compatible),
        },
        Source(title=f"PartSelect model page ({model_number})", url=final_url or url),
    )


def bundle_to_dict(bundle: PartBundle) -> Dict[str, Any]:
    return asdict(bundle)
