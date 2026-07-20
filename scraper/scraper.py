"""
監理報告自動抓取腳本 v3
- 每個來源一個 adapter，優先使用 RSS（FSB/IFIAR），HTML 解析時鎖定主內容區
- 共用驗證閘門過濾導覽連結與按鈕文字，舊資料每次執行也重新驗證
- 抓報告詳細頁的真實摘要（og:description / 首段）再交給 Claude「翻譯」而非「創作」
- 日期一律正規化為 YYYY-MM-DD / YYYY-MM / YYYY，排序穩定
"""

import os
import json
import time
import re
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import anthropic
from bs4 import BeautifulSoup

# ─────────────────────────────────────────
#  來源設定
# ─────────────────────────────────────────

DEFAULT_SOURCES = [
    {
        "id":       "IOSCO",
        "name":     "IOSCO",
        "fullname": "國際證券管理機構組織",
        "url":      "https://www.iosco.org/v2/publications/?subsection=public_reports",
        "base_url": "https://www.iosco.org",
        "strategy": "iosco",
    },
    {
        "id":       "FSB",
        "name":     "FSB",
        "fullname": "金融穩定委員會",
        "url":      "https://www.fsb.org/publications/",
        "feed":     "https://www.fsb.org/feed/",
        "base_url": "https://www.fsb.org",
        "strategy": "wordpress",
    },
    {
        "id":       "IFIAR",
        "name":     "IFIAR",
        "fullname": "國際獨立審計監理機關論壇",
        "url":      "https://www.ifiar.org/publications/",
        "feed":     "https://www.ifiar.org/feed/",
        "base_url": "https://www.ifiar.org",
        "strategy": "wordpress",
    },
    {
        "id":       "IESBA",
        "name":     "IESBA",
        "fullname": "國際會計師道德準則委員會",
        "url":      "https://www.ethicsboard.org/publications",
        "base_url": "https://www.ethicsboard.org",
        "pub_path": "/publications/",
        "strategy": "ifac_platform",
    },
    {
        "id":       "IAASB",
        "name":     "IAASB",
        "fullname": "國際審計與確信準則委員會",
        "url":      "https://www.iaasb.org/publications",
        "base_url": "https://www.iaasb.org",
        "pub_path": "/publications/",
        "strategy": "ifac_platform",
    },
    {
        "id":       "IFAC",
        "name":     "IFAC",
        "fullname": "國際會計師聯合會",
        "url":      "https://www.ifac.org/knowledge-gateway",
        "base_url": "https://www.ifac.org",
        "pub_path": "/knowledge-gateway/",
        "strategy": "ifac_platform",
    },
    {
        "id":       "PCAOB",
        "name":     "PCAOB",
        "fullname": "美國公開發行公司會計監督委員會",
        "url":      "https://pcaobus.org/resources/staff-publications",
        "news_url":  "https://pcaobus.org/news-events/news-releases",
        "base_url": "https://pcaobus.org",
        "strategy": "pcaob",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATA_PATH           = Path(__file__).parent.parent / "data" / "reports.json"
CUSTOM_SOURCES_PATH = Path(__file__).parent.parent / "data" / "custom_sources.json"

MAX_PER_SOURCE = 40
KNOWN_SOURCES  = [s["id"] for s in DEFAULT_SOURCES]
IFAC_FAMILY    = {"IESBA", "IAASB", "IFAC"}   # 同屬 IFAC Drupal 平台，JS 動態渲染

# ─────────────────────────────────────────
#  驗證閘門：過濾導覽連結、按鈕文字、目錄頁
# ─────────────────────────────────────────

# 按鈕／動作字樣：標題等於或以此結尾者不是真標題
BUTTON_TEXTS = [
    "view report", "cover note", "read more", "learn more", "download",
    "view all", "see all", "view details", "click here", "more info",
]

NAV_KEYWORDS = [
    "home", "about", "contact", "login", "search", "menu", "cookie",
    "privacy", "sitemap", "subscribe", "newsletter", "follow us",
    "twitter", "linkedin", "facebook", "youtube",
    "back to", "return to", "members area", "hub",
    "committee", "who we are", "careers", "annual meeting",
    "investor education", "capacity building", "training",
    "media release", "press release", "media room",
    "requests for logo", "permissions and policies", "your language",
    "secretariat", "information repositories", "monitoring group",
    "monitoring board", "task force", "chairs of", "members of",
    "sign up", "log in", "my account", "faq",
]

# 這些 URL 樣式是網站功能頁／目錄頁，不是單篇出版品
URL_BLOCKLIST = [
    "/members_area", "/media_room", "/v2/about", "/v2/media_room",
    "/about/", "/press/", "/careers", "/contact",
    "/work-of-the-fsb/",                 # FSB 主題目錄頁
    "/consultations/",                   # FSB 諮詢目錄頁（單篇諮詢報告走 /YYYY/MM/）
    "/publications/policy-documents", "/publications/progress-reports",
    "/publications/evaluation-reports", "/publications/g20-reports",
    "/publications/peer-review-reports", "/publications/regional-consultative",
    "apps.ifac.org",                     # IFAC 授權申請系統
    "/ifrs/", "/research/", "/information-repositories",
    "?subsection=",                      # IOSCO 分類目錄頁（真報告連到 pubdocs PDF）
    "javascript:", "mailto:", "#",
]

REPORT_KEYWORDS = [
    "report", "guidance", "recommendation", "consultation", "standard",
    "framework", "principles", "assessment", "review", "survey",
    "statement", "policy", "regulation", "code", "handbook",
    "implementation", "monitoring", "disclosure",
    "discussion paper", "working paper", "exposure draft",
    "spotlight", "bulletin", "advisory", "brief", "letter",
    "practices", "findings", "inspection", "alert",
]

IOSCO_CODE_PATTERN = re.compile(
    r"\b(FR|CR|MR|ER|OR)\s*[/\-]\s*\d+\s*[/\-]\s*\d{4}\b", re.IGNORECASE
)


def is_mostly_nonlatin(title: str) -> bool:
    """標題是否以非拉丁文字為主（俄文／中日韓／阿拉伯文等翻譯版準則），
    這類多是英文準則的外語翻譯版，非主要出版品。英文標題中的花引號、重音符不受影響。"""
    latin = nonlatin = 0
    for ch in title:
        o = ord(ch)
        if (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A) or (0xC0 <= o <= 0x24F):
            latin += 1
        elif ((0x0400 <= o <= 0x04FF) or (0x4E00 <= o <= 0x9FFF) or   # 西里爾、CJK
              (0xAC00 <= o <= 0xD7AF) or (0x0600 <= o <= 0x06FF) or   # 諺文、阿拉伯
              (0x0370 <= o <= 0x03FF) or (0x0590 <= o <= 0x05FF) or   # 希臘、希伯來
              (0x3040 <= o <= 0x30FF) or (0x0E00 <= o <= 0x0E7F)):    # 日文假名、泰文
            nonlatin += 1
    return nonlatin > latin


def clean_title(title: str) -> str:
    """去掉標題裡混入的按鈕字樣與多餘空白"""
    t = re.sub(r"\s+", " ", title or "").strip()
    changed = True
    while changed:
        changed = False
        for btn in BUTTON_TEXTS:
            pat = re.compile(re.escape(btn) + r"\s*$", re.IGNORECASE)
            new = pat.sub("", t).rstrip(" -–—|·:")
            if new != t:
                t = new.strip()
                changed = True
    return t


def is_valid_report(title: str, url: str, trusted: bool = False) -> bool:
    """新舊資料共用的驗證閘門：True 才算真正的出版品。
    trusted=True 用於策展型來源（新聞發布、RSS 等）：仍套用所有負向過濾
    （長度、非拉丁、按鈕字樣、導覽關鍵字、URL 黑名單），但略過「必須是報告樣式」
    的關鍵字要求——因新聞標題如「PCAOB Sanctions …」「PCAOB Names …」常不含報告關鍵字。"""
    t = clean_title(title)
    t_lower = t.lower()

    if len(t) < 15:
        return False
    if is_mostly_nonlatin(t):          # 翻譯版準則（俄文／中日韓等）非主要出版品
        return False
    # 標題整個就是按鈕字樣（clean 完剩編號如 "FR/05/2026" 也算沒標題）
    if t_lower in BUTTON_TEXTS:
        return False
    if IOSCO_CODE_PATTERN.fullmatch(t.strip()):
        return False
    if any(k in t_lower for k in NAV_KEYWORDS):
        return False

    u_lower = (url or "").lower()
    if any(b in u_lower for b in URL_BLOCKLIST):
        return False
    # FSB：真正的單篇出版品網址是 /YYYY/MM/slug/，此格式本身即可認定為出版品
    if "fsb.org" in u_lower:
        return bool(re.search(r"fsb\.org/20\d{2}/\d{2}/", u_lower))

    if trusted:            # 策展型來源：已通過所有負向過濾即認定為有效
        return True
    if any(k in t_lower for k in REPORT_KEYWORDS):
        return True
    if IOSCO_CODE_PATTERN.search(t):
        return True
    if re.search(r"\b20\d{2}\b", t) and len(t) > 25:
        return True
    return False


# ─────────────────────────────────────────
#  日期正規化
# ─────────────────────────────────────────

MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_D_FULL   = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+(20\d{2})\b", re.I)
_D_MDY    = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(20\d{2})\b", re.I)
_D_ISO    = re.compile(r"\b(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})\b")
_D_MY     = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", re.I)
_D_YEAR   = re.compile(r"\b(20\d{2})\b")


def normalize_date(text: str) -> str:
    """從文字提取日期 → 'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' / ''"""
    if not text:
        return ""
    text = text.strip()
    m = _D_ISO.search(text)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        if "01" <= mo <= "12" and "01" <= d <= "31":
            return f"{y}-{mo}-{d}"
    m = _D_FULL.search(text)
    if m:
        d, mon, y = m.group(1).zfill(2), MONTH_MAP[m.group(2)[:3].lower()], m.group(3)
        return f"{y}-{mon}-{d}"
    m = _D_MDY.search(text)
    if m:
        mon, d, y = MONTH_MAP[m.group(1)[:3].lower()], m.group(2).zfill(2), m.group(3)
        return f"{y}-{mon}-{d}"
    m = _D_MY.search(text)
    if m:
        return f"{m.group(2)}-{MONTH_MAP[m.group(1)[:3].lower()]}"
    m = _D_YEAR.search(text)
    if m and 2010 <= int(m.group(1)) <= 2035:
        return m.group(1)
    return ""


def sort_key(date_str: str) -> str:
    """排序用：缺月/日視為該年年初，缺日期排最後"""
    if not date_str:
        return "0000-00-00"
    parts = date_str.split("-")
    while len(parts) < 3:
        parts.append("01")
    return "-".join(parts)


def make_id(source: str, url: str) -> str:
    return hashlib.md5(f"{source}:{url}".encode()).hexdigest()[:12]


def fetch(url: str, timeout: int = 25) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# ─────────────────────────────────────────
#  各來源 adapter（回傳統一 schema，尚未含翻譯）
# ─────────────────────────────────────────

def scrape_wordpress_rss(src: dict) -> list[dict]:
    """FSB / IFIAR：WordPress RSS，標題與日期最乾淨"""
    xml_text = fetch(src["feed"])
    # 移除非法控制字元，避免 ElementTree 解析失敗
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    root = ET.fromstring(xml_text)
    reports = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        if not title or not link:
            continue
        date = ""
        if pub:
            try:
                date = datetime.strptime(pub[:16].strip(), "%a, %d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                date = normalize_date(pub)
        summary = BeautifulSoup(desc, "lxml").get_text(" ", strip=True) if desc else ""
        reports.append({
            "source": src["id"], "title_en": clean_title(title), "url": link,
            "date": date, "summary_en": summary[:600],
        })
    return reports


def scrape_wordpress_html(src: dict) -> list[dict]:
    """FSB / IFIAR 的 RSS 失敗時退回列表頁解析"""
    soup = BeautifulSoup(fetch(src["url"]), "lxml")
    for tag in soup.select("nav, footer, header, script, style"):
        tag.decompose()
    reports, seen = [], set()
    for a in soup.select("main a[href], article a[href], .post a[href], li a[href]"):
        href  = urljoin(src["base_url"], a.get("href", "").strip())
        title = clean_title(a.get_text(" ", strip=True))
        if not title or href in seen:
            continue
        seen.add(href)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        reports.append({
            "source": src["id"], "title_en": title, "url": href,
            "date": normalize_date(parent_text), "summary_en": "",
        })
    return reports


def scrape_iosco(src: dict) -> list[dict]:
    """IOSCO public_reports：從整列文字取真標題，連結取同列的 pubdocs PDF"""
    soup = BeautifulSoup(fetch(src["url"]), "lxml")
    reports, seen = [], set()

    # 出版品列表通常是 table row 或 list item，一列＝一份報告
    rows = soup.select("tr")
    if len(rows) < 3:
        rows = soup.select("li, .views-row, article, .row")

    for row in rows:
        a = None
        for cand in row.find_all("a", href=True):
            if "pubdocs" in cand["href"] or cand["href"].lower().endswith(".pdf"):
                a = cand
                break
        if a is None:
            continue
        href = urljoin(src["base_url"], a["href"].strip())
        if href in seen:
            continue

        row_text = row.get_text(" ", strip=True)
        code_m   = IOSCO_CODE_PATTERN.search(row_text)
        date     = normalize_date(row_text)

        # 真標題＝整列文字去掉編號、日期、按鈕字樣後剩下的主體
        title = row_text
        if code_m:
            title = title.replace(code_m.group(0), " ")
        for btn in BUTTON_TEXTS:
            title = re.sub(re.escape(btn), " ", title, flags=re.IGNORECASE)
        title = re.sub(r"\b\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}\b", " ", title, flags=re.I)
        title = re.sub(r"\b20\d{2}[/\-]\d{1,2}[/\-]\d{1,2}\b", " ", title)
        title = clean_title(title)
        if code_m and title:
            title = f"{code_m.group(0)} {title}"

        if not title or len(clean_title(re.sub(IOSCO_CODE_PATTERN, '', title))) < 15:
            continue

        seen.add(href)
        reports.append({
            "source": "IOSCO", "title_en": title, "url": href,
            "date": date, "summary_en": "",
        })
    return reports


def scrape_ifac_platform(src: dict) -> list[dict]:
    """IESBA / IAASB / IFAC（IFAC Drupal 平台）：以 URL 樣式（如 /publications/）辨識
    出版品連結，掃整頁而非僅 main，較能承受不同版型。動態 JS 載入的清單抓不到（已知限制）。"""
    soup = BeautifulSoup(fetch(src["url"]), "lxml")
    for tag in soup.select("nav, footer, header, script, style, .cookie-banner"):
        tag.decompose()

    site_host = urlparse(src["base_url"]).netloc.replace("www.", "")
    pub_path  = src.get("pub_path", "/publications/")
    reports, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = urljoin(src["base_url"], a["href"].strip())
        p    = urlparse(href)
        if site_host not in p.netloc:          # 僅本站內容頁
            continue
        if pub_path not in p.path:             # 必須是出版品路徑
            continue
        if p.path.rstrip("/").endswith(pub_path.rstrip("/")):  # 排除清單頁本身
            continue
        title = clean_title(a.get_text(" ", strip=True))
        if not title or href in seen:
            continue
        seen.add(href)
        # 日期：往上找最多 3 層取含日期的容器文字
        ctx, node = "", a
        for _ in range(3):
            if node.parent is None:
                break
            node = node.parent
            ctx = node.get_text(" ", strip=True)
            if normalize_date(ctx):
                break
        date = normalize_date(ctx)
        # 這些站多為 JS 動態渲染，靜態 HTML 僅剩導覽標籤（無日期）；
        # 有日期者才是真實出版品／文章，藉此濾掉分類導覽雜訊
        if not date:
            continue
        reports.append({
            "source": src["id"], "title_en": title, "url": href,
            "date": date, "summary_en": "",
        })
    return reports


def scrape_pcaob_news(news_url: str, base_url: str) -> list[dict]:
    """PCAOB 新聞發布（news releases）：策展型列表。往上找容器內日期，
    要求有日期（新聞必有日期，同時藉此濾掉導覽雜訊）；標記 trusted 略過報告關鍵字要求。"""
    raw = fetch(news_url)
    soup = BeautifulSoup(raw, "lxml")

    # ── 一次性診斷（驗證後移除）──
    if os.environ.get("DEBUG_PCAOB"):
        _t = soup.find("title")
        print(f"  [DBG] html len={len(raw)} title={_t.get_text(strip=True)[:80] if _t else '?'}")
        all_a = soup.find_all("a", href=True)
        newsish = [a for a in all_a if re.search(r"news-release|/detail/|/news-events/", a.get("href",""), re.I)]
        has_year = bool(re.search(r"20\d\d", raw))
        print(f"  [DBG] 全部 <a>={len(all_a)}；news-ish <a>={len(newsish)}")
        print(f"  [DBG] html 含 'news-release' 字串: {'news-release' in raw.lower()}；含年份樣式: {has_year}")
        for a in newsish[:8]:
            t = clean_title(a.get_text(' ', strip=True))
            node, ctx = a, ""
            for _ in range(3):
                if node.parent is None: break
                node = node.parent; ctx = node.get_text(' ', strip=True)
                if normalize_date(ctx.replace(t,' ')): break
            print(f"  [DBG] href={a['href'][:70]} | text={t[:45]!r} | date={normalize_date(ctx.replace(t,' '))!r}")

    for tag in soup.select("nav, footer, header, script, style"):
        tag.decompose()
    main = soup.select_one("main") or soup

    reports, seen = [], set()
    for a in main.find_all("a", href=True):
        href  = urljoin(base_url, a["href"].strip())
        title = clean_title(a.get_text(" ", strip=True))
        if not title or href in seen or len(title) < 15:
            continue
        if "pcaobus.org" not in urlparse(href).netloc and "pcaobus.org" not in href:
            continue
        seen.add(href)
        # 日期：往上找最多 3 層，取第一個含日期的容器（新聞列表日期常在鄰近元素）
        date, node = "", a
        for _ in range(3):
            if node.parent is None:
                break
            node = node.parent
            d = normalize_date(node.get_text(" ", strip=True).replace(title, " "))
            if d:
                date = d
                break
        if not date:            # 新聞必有日期；無日期者為導覽雜訊，剔除
            continue
        reports.append({
            "source": "PCAOB", "title_en": title, "url": href,
            "date": date, "summary_en": "", "trusted": True,
        })
    return reports


def scrape_pcaob(src: dict) -> list[dict]:
    """PCAOB：新聞發布（news releases）＋幕僚出版品（staff publications）。
    新聞排前面，避免被每來源上限截斷；兩者同掛 source=PCAOB。"""
    news = []
    if src.get("news_url"):
        try:
            news = scrape_pcaob_news(src["news_url"], src["base_url"])
            print(f"  PCAOB 新聞發布：{len(news)} 則")
        except Exception as e:
            print(f"  ⚠️  PCAOB 新聞發布抓取失敗：{e}")

    # ── 幕僚出版品：維持原有日期解析（緊鄰容器＋標題年份備援），不動 ──
    soup = BeautifulSoup(fetch(src["url"]), "lxml")
    for tag in soup.select("nav, footer, header, script, style"):
        tag.decompose()
    main = soup.select_one("main") or soup

    staff, seen = [], {r["url"] for r in news}
    for a in main.find_all("a", href=True):
        href  = urljoin(src["base_url"], a["href"].strip())
        title = clean_title(a.get_text(" ", strip=True))
        if not title or href in seen or len(title) < 15:
            continue
        if "pcaobus.org" not in urlparse(href).netloc and "pcaobus.org" not in href:
            continue
        seen.add(href)

        # 日期：僅信任緊鄰容器（不往上爬太多層，避免抓到頁面角落不相干日期）
        ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
        date = normalize_date(ctx.replace(title, " "))
        if not date:
            m = re.search(r"\b(20\d{2})\b", title)
            date = m.group(1) if m else ""

        staff.append({
            "source": "PCAOB", "title_en": title, "url": href,
            "date": date, "summary_en": "",
        })
    return news + staff


def scrape_custom(src: dict) -> list[dict]:
    """使用者自訂來源：通用保守解析"""
    soup = BeautifulSoup(fetch(src["url"]), "lxml")
    for tag in soup.select("nav, footer, header, .menu, .sidebar, script, style"):
        tag.decompose()
    base = src.get("base_url") or f"{urlparse(src['url']).scheme}://{urlparse(src['url']).netloc}"
    reports, seen = [], set()
    for a in (soup.select_one("main") or soup).find_all("a", href=True):
        href  = urljoin(base, a["href"].strip())
        title = clean_title(a.get_text(" ", strip=True))
        if not title or href in seen:
            continue
        seen.add(href)
        ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
        reports.append({
            "source": src["id"], "title_en": title, "url": href,
            "date": normalize_date(ctx), "summary_en": "",
        })
    return reports


STRATEGIES = {
    "iosco":         scrape_iosco,
    "ifac_platform": scrape_ifac_platform,
    "pcaob":         scrape_pcaob,
}


def fetch_source(src: dict) -> list[dict]:
    """抓取單一來源：驗證閘門 → 截斷數量。任何失敗回傳空清單，不中斷整體。"""
    print(f"\n[{src['id']}] 抓取：{src.get('feed') or src['url']}")
    candidates: list[dict] = []
    try:
        if src.get("strategy") == "wordpress":
            try:
                candidates = scrape_wordpress_rss(src)
                print(f"  RSS 取得 {len(candidates)} 則")
            except Exception as e:
                print(f"  RSS 失敗（{e}），改用 HTML")
                candidates = scrape_wordpress_html(src)
        else:
            fn = STRATEGIES.get(src.get("strategy"), scrape_custom)
            candidates = fn(src)
    except Exception as e:
        print(f"  ⚠️  抓取失敗：{e}")
        return []

    valid = [r for r in candidates
             if is_valid_report(r["title_en"], r["url"], r.get("trusted", False))]
    print(f"  → 原始 {len(candidates)} 則，通過驗證 {len(valid)} 則")
    for r in valid:
        r["id"] = make_id(r["source"], r["url"])
    return valid[:MAX_PER_SOURCE]


# ─────────────────────────────────────────
#  真實摘要：抓報告詳細頁的 og:description / 首段
# ─────────────────────────────────────────

def fetch_summary(url: str) -> str:
    if url.lower().endswith(".pdf"):
        return ""
    try:
        soup = BeautifulSoup(fetch(url, timeout=20), "lxml")
    except Exception:
        return ""
    for sel, attr in [("meta[property='og:description']", "content"),
                      ("meta[name='description']", "content")]:
        tag = soup.select_one(sel)
        if tag and tag.get(attr, "").strip():
            return tag[attr].strip()[:600]
    main = soup.select_one("main, article, .content, #content") or soup
    for p in main.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) > 80:
            return text[:600]
    return ""


# ─────────────────────────────────────────
#  Claude 翻譯（翻譯真實內容，不創作）
# ─────────────────────────────────────────

def translate_batch(reports: list[dict], client: anthropic.Anthropic) -> None:
    """就地填入 title_zh / intro。intro 一律是 summary_en 的翻譯；無摘要則為空。"""
    BATCH_SIZE = 8
    for i in range(0, len(reports), BATCH_SIZE):
        batch = reports[i: i + BATCH_SIZE]
        items_block = "\n".join(
            json.dumps(
                {"index": j + 1, "title_en": r["title_en"], "summary_en": r.get("summary_en", "")},
                ensure_ascii=False,
            )
            for j, r in enumerate(batch)
        )
        prompt = f"""你是金融監理領域的專業譯者，請將以下國際監理機構出版品的標題與摘要翻譯成繁體中文（台灣金管會體例用語）。

規則：
1. title_zh：title_en 的專業翻譯。
2. intro：summary_en 的翻譯（可精簡為兩句以內）。若 summary_en 是空字串，intro 必須回空字串——嚴禁根據標題想像或創作內容。
3. 機構縮寫（FSB、IOSCO、G20、G-SIBs 等）保留原文。

以 JSON 陣列回覆，每個物件含 index、title_zh、intro。只輸出 JSON。

{items_block}"""
        try:
            resp = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=3000,
                thinking={"type": "disabled"},   # 純翻譯不需思考；Sonnet 5 預設會開 adaptive thinking
                messages=[{"role": "user", "content": prompt}],
            )
            # Sonnet 5 回應可能含 thinking block，需明確取 text block（不能假設 content[0]）
            raw = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            start, end = raw.find("["), raw.rfind("]")
            items = json.loads(raw[start:end + 1])
            for item in items:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    batch[idx]["title_zh"] = item.get("title_zh", "").strip()
                    intro = item.get("intro", "").strip()
                    # 防護：模型違規在無摘要時創作 → 丟棄
                    if not batch[idx].get("summary_en"):
                        intro = ""
                    batch[idx]["intro"] = intro
            print(f"  ✓ 翻譯批次 {i // BATCH_SIZE + 1}（{len(batch)} 則）")
        except Exception as e:
            print(f"  ⚠️  翻譯失敗：{e}")
            for r in batch:
                r.setdefault("title_zh", "")
                r.setdefault("intro", "")
        time.sleep(1)


# ─────────────────────────────────────────
#  主流程
# ─────────────────────────────────────────

def load_existing() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"reports": []}


def load_custom_sources() -> list:
    if CUSTOM_SOURCES_PATH.exists():
        try:
            return json.loads(CUSTOM_SOURCES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def revalidate_existing(reports: list[dict]) -> list[dict]:
    """舊資料重新過驗證閘門＋日期正規化（含 v1 schema 遷移）"""
    kept = []
    for r in reports:
        title   = clean_title(r.get("title_en", ""))
        url     = r.get("url", "")
        trusted = bool(r.get("trusted"))
        if not is_valid_report(title, url, trusted):
            continue
        date = normalize_date(r.get("date") or r.get("date_raw") or "")
        # PCAOB 幕僚出版品舊資料日期污染：與標題年份矛盾時改用標題年份。
        # 新聞發布（trusted）有真實日期，不可被標題內的年份覆蓋。
        if r.get("source") == "PCAOB" and not trusted:
            m = re.search(r"\b(20\d{2})\b", title)
            if m and date[:4] != m.group(1):
                date = m.group(1)
        # IFAC 平台站（JS 渲染）：僅保留有日期者，濾掉靜態 HTML 殘留的導覽標籤
        if r.get("source") in IFAC_FAMILY and not date:
            continue
        kept.append({
            "id":         make_id(r.get("source", ""), url),
            "source":     r.get("source", ""),
            "title_en":   title,
            "title_zh":   r.get("title_zh", ""),
            "summary_en": r.get("summary_en", ""),
            "summary_checked": bool(r.get("summary_checked")),
            "trusted":    trusted,
            "intro":      r.get("intro", ""),
            "url":        url,
            "date":       date,
            "first_seen": r.get("first_seen", ""),
        })
    return kept


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("❌ 未設定 ANTHROPIC_API_KEY，中止執行")
        return

    client   = anthropic.Anthropic(api_key=api_key)
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing = revalidate_existing(load_existing().get("reports", []))
    by_url   = {r["url"]: r for r in existing}

    sources = DEFAULT_SOURCES + load_custom_sources()
    print(f"\n{'=' * 55}\n  監理報告抓取 v3 — {run_time}\n  來源：{len(sources)} 個\n{'=' * 55}")

    new_reports = []
    for src in sources:
        for r in fetch_source(src):
            if r["url"] in by_url:
                # 已存在：用新抓到的標題／日期更新（修復舊的壞標題），保留翻譯
                old = by_url[r["url"]]
                old["title_en"] = r["title_en"]
                if r["date"]:
                    old["date"] = r["date"]
                if r.get("summary_en") and not old.get("summary_en"):
                    old["summary_en"] = r["summary_en"]
                continue
            r["first_seen"] = today
            new_reports.append(r)
            by_url[r["url"]] = r

    # 補抓摘要：新報告，以及既有但從未檢查過摘要的
    to_check = [r for r in by_url.values()
                if not r.get("summary_en") and not r.get("summary_checked")]
    if to_check:
        print(f"\n{len(to_check)} 則需要抓取摘要…")
        for r in to_check:
            r["summary_en"] = fetch_summary(r["url"])
            r["summary_checked"] = True
            time.sleep(0.5)

    # 補翻譯：缺中文標題的，或有真實摘要但還沒翻譯的
    pending = [r for r in by_url.values()
               if not r.get("title_zh") or (r.get("summary_en") and not r.get("intro"))]
    if pending:
        print(f"新報告 {len(new_reports)} 則；共 {len(pending)} 則送翻譯…")
        translate_batch(pending, client)
        for r in pending:
            r.setdefault("title_zh", "")
            r.setdefault("intro", "")

    all_reports = list(by_url.values())
    all_reports.sort(key=lambda r: (sort_key(r.get("date", "")), r.get("first_seen", "")), reverse=True)

    sources_meta = {
        src["id"]: {"name": src["name"], "fullname": src.get("fullname", ""), "url": src["url"]}
        for src in sources
    }

    output = {
        "schema_version": 2,
        "last_updated":   run_time,
        "last_batch":     today if new_reports else load_existing().get("last_batch", ""),
        "total":          len(all_reports),
        "sources_meta":   sources_meta,
        "reports":        all_reports,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 55}\n  ✅ 完成！新增 {len(new_reports)} 則，總計 {len(all_reports)} 則\n{'=' * 55}\n")


if __name__ == "__main__":
    main()
