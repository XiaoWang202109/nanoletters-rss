import re
import html
import json
import datetime as dt
from email.utils import format_datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


# ============================================================
# 基本设置
# ============================================================

ACS_PAGE = "https://pubs.acs.org/toc/nalefd/0/0"
BASE = "https://pubs.acs.org"

# Nano Letters 的 ISSN
CROSSREF_ISSN = "1530-6984"

FEED_TITLE = "Nano Letters: Latest Articles"
FEED_LINK = ACS_PAGE
FEED_DESCRIPTION = "Latest ASAP articles published in Nano Letters."

MAX_ITEMS = 50

# 第一次建议保持 False，先确认订阅源能正常工作。
# 成功之后，如果你只想要凝聚态/二维材料方向，再改成 True。
CONDENSED_ONLY = False

CONDENSED_KEYWORDS = [
    "wse2", "wse₂",
    "mose2", "mose₂",
    "mote2", "mote₂",
    "ws2", "ws₂",
    "mos2", "mos₂",
    "graphene",
    "twisted",
    "twist",
    "moire",
    "moiré",
    "exciton",
    "trion",
    "polaron",
    "valley",
    "heterostructure",
    "2d",
    "two-dimensional",
    "superconduct",
    "magnet",
    "ferromagnet",
    "antiferromagnet",
    "quantum dot",
    "chern",
    "topological",
    "phonon",
    "raman",
    "crsbr",
    "chromium disulfide",
    "transition metal dichalcogenide",
    "tmd",
]


# ============================================================
# 工具函数
# ============================================================

def normalize_space(s: str) -> str:
    return " ".join((s or "").split())


def is_article_title(title: str) -> bool:
    if not title:
        return False

    title = normalize_space(title)
    low = title.lower()

    bad_titles = {
        "abstract",
        "full text",
        "pdf",
        "supporting info",
        "supporting information",
        "citation",
        "references",
        "cited by",
        "metrics",
        "figures",
        "tables",
    }

    if low in bad_titles:
        return False

    if len(title) < 20:
        return False

    return True


def extract_doi_from_url(url: str):
    m = re.search(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.1021/[^?#]+)", url)
    if not m:
        return None
    return m.group(1)


def is_condensed_article(title: str, text: str = "") -> bool:
    if not CONDENSED_ONLY:
        return True

    full = f"{title} {text}".lower()
    return any(k.lower() in full for k in CONDENSED_KEYWORDS)


def get_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def find_best_parent(a):
    node = a
    best = a

    for _ in range(10):
        if node is None:
            break

        text = node.get_text(" ", strip=True)
        if (
            "Publication Date" in text
            or "ASAP" in text
            or "Nano Letters" in text
            or len(text) > 200
        ):
            best = node

        node = node.parent

    return best


def extract_image(card):
    if card is None:
        return None

    for img in card.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
        )

        if not src:
            continue

        if src.startswith("data:"):
            continue

        src = urljoin(BASE, src)
        low = src.lower()

        if "logo" in low or "spinner" in low or "placeholder" in low:
            continue

        return src

    return None


def extract_pubdate_from_text(text):
    text = normalize_space(text)

    patterns = [
        r"Publication Date\s*\(Web\)\s*:\s*([A-Za-z]+ \d{1,2}, \d{4})",
        r"Published online\s*([A-Za-z]+ \d{1,2}, \d{4})",
        r"([A-Za-z]+ \d{1,2}, \d{4})",
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            try:
                d = dt.datetime.strptime(m.group(1), "%B %d, %Y")
                return d.replace(tzinfo=dt.timezone.utc)
            except Exception:
                pass

    return dt.datetime.now(dt.timezone.utc)


def crossref_date_to_datetime(date_parts):
    try:
        parts = date_parts[0]
        year = parts[0]
        month = parts[1] if len(parts) > 1 else 1
        day = parts[2] if len(parts) > 2 else 1
        return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


# ============================================================
# 优先尝试从 ACS 页面抓取，能拿到图片
# ============================================================

def fetch_articles_from_acs():
    print("Trying ACS page...")

    r = requests.get(ACS_PAGE, headers=get_headers(), timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    articles = []
    seen_doi = set()

    for a in soup.select('a[href*="/doi/"]'):
        title = normalize_space(a.get_text(" ", strip=True))

        if not is_article_title(title):
            continue

        href = urljoin(BASE, a.get("href", ""))
        doi = extract_doi_from_url(href)

        if not doi:
            continue

        if doi in seen_doi:
            continue

        seen_doi.add(doi)

        card = find_best_parent(a)
        card_text = normalize_space(card.get_text(" ", strip=True)) if card else ""

        if not is_condensed_article(title, card_text):
            continue

        article = {
            "title": title,
            "link": f"https://doi.org/{doi}",
            "acs_link": f"{BASE}/doi/{doi}",
            "doi": doi,
            "image": extract_image(card),
            "pubdate": extract_pubdate_from_text(card_text),
            "description": card_text[:900],
            "source": "ACS",
        }

        articles.append(article)

        if len(articles) >= MAX_ITEMS:
            break

    print(f"ACS articles found: {len(articles)}")
    return articles


# ============================================================
# 备用方案：从 Crossref 抓 Nano Letters 最新 DOI
# 如果 ACS 页面被 403 阻止，至少 RSS 还能更新
# 缺点：通常没有 graphical abstract 图片
# ============================================================

def fetch_articles_from_crossref():
    print("Trying Crossref fallback...")

    today = dt.date.today()
    start = today - dt.timedelta(days=180)

    url = f"https://api.crossref.org/journals/{CROSSREF_ISSN}/works"

    params = {
        "filter": f"from-pub-date:{start.isoformat()},type:journal-article",
        "sort": "published",
        "order": "desc",
        "rows": str(MAX_ITEMS),
        "select": "DOI,title,URL,issued,published-online,published-print,abstract",
    }

    headers = {
        "User-Agent": "nanoletters-rss-generator/1.0 (mailto:example@example.com)"
    }

    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()

    data = r.json()
    items = data.get("message", {}).get("items", [])

    articles = []
    seen_doi = set()

    for item in items:
        titles = item.get("title") or []
        if not titles:
            continue

        title = normalize_space(titles[0])
        doi = item.get("DOI")

        if not title or not doi:
            continue

        if doi in seen_doi:
            continue

        seen_doi.add(doi)

        abstract = item.get("abstract") or ""
        abstract = re.sub(r"<[^>]+>", "", abstract)
        abstract = normalize_space(abstract)

        if not is_condensed_article(title, abstract):
            continue

        date_info = (
            item.get("published-online")
            or item.get("published-print")
            or item.get("issued")
            or {}
        )
        pubdate = crossref_date_to_datetime(date_info.get("date-parts", [[today.year, today.month, today.day]]))

        articles.append(
            {
                "title": title,
                "link": f"https://doi.org/{doi}",
                "acs_link": item.get("URL") or f"https://doi.org/{doi}",
                "doi": doi,
                "image": None,
                "pubdate": pubdate,
                "description": abstract if abstract else f"DOI: {doi}",
                "source": "Crossref",
            }
        )

    print(f"Crossref articles found: {len(articles)}")
    return articles


def fetch_articles():
    try:
        articles = fetch_articles_from_acs()
        if articles:
            return articles
    except Exception as e:
        print(f"ACS fetch failed: {repr(e)}")

    articles = fetch_articles_from_crossref()

    if not articles:
        raise RuntimeError("No articles found from ACS or Crossref.")

    return articles


# ============================================================
# 写出 RSS
# ============================================================

def write_rss(articles, out_file="feed.xml"):
    media_ns = "http://search.yahoo.com/mrss/"
    content_ns = "http://purl.org/rss/1.0/modules/content/"

    ET.register_namespace("media", media_ns)
    ET.register_namespace("content", content_ns)

    # 注意：这里不要手动写 xmlns:media / xmlns:content
    # ElementTree 会根据 register_namespace 自动添加，手动添加会导致重复定义
    rss = ET.Element("rss", {"version": "2.0"})

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = FEED_LINK
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        dt.datetime.now(dt.timezone.utc)
    )

    for art in articles:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = art["title"]
        ET.SubElement(item, "link").text = art["link"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = art["doi"]
        ET.SubElement(item, "pubDate").text = format_datetime(art["pubdate"])

        desc = art["description"] or ""
        desc = normalize_space(desc)
        ET.SubElement(item, "description").text = desc

        if art["image"]:
            ET.SubElement(
                item,
                f"{{{media_ns}}}thumbnail",
                {"url": art["image"]},
            )

        content_html = ""

        if art["image"]:
            content_html += f'<p><img src="{html.escape(art["image"])}"></p>\n'

        content_html += f'<p><b>{html.escape(art["title"])}</b></p>\n'
        content_html += f'<p>DOI: {html.escape(art["doi"])}</p>\n'
        content_html += f'<p>Source: {html.escape(art["source"])}</p>\n'
        content_html += f'<p><a href="{html.escape(art["acs_link"])}">Open article page</a></p>\n'

        ET.SubElement(item, f"{{{content_ns}}}encoded").text = content_html

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_file, encoding="utf-8", xml_declaration=True)


def main():
    articles = fetch_articles()
    write_rss(articles)
    print(f"Generated feed.xml with {len(articles)} articles.")


if __name__ == "__main__":
    main()
