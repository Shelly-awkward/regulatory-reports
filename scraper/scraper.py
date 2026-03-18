"""
監理報告自動抓取腳本 v2
針對各機構實際的出版品列表頁面，精確抓取報告
"""

import os
import json
import time
import re
import hashlib
import requests
import anthropic
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

# ─────────────────────────────────────────
#  設定：各來源的精確出版品頁面
# ─────────────────────────────────────────
DEFAULT_SOURCES = [
    {
        "id":       "IOSCO",
        "name":     "IOSCO",
        "fullname": "國際證券事務監察委員會組織",
        "url":      "https://www.iosco.org/v2/publications/?subsection=public_reports",
        "base_url": "https://www.iosco.org",
        "strategy": "iosco",
    },
    {
        "id":       "IESBA",
        "name":     "IESBA",
        "fullname": "國際會計師倫理準則委員會",
        "url":      "https://www.ethicsboard.org/publications",
        "base_url": "https://www.ethicsboard.org",
        "strategy": "generic",
    },
    {
        "id":       "FSB",
        "name":     "FSB",
        "fullname": "金融穩定委員會",
        "url":      "https://www.fsb.org/publications/",
        "base_url": "https://www.fsb.org",
        "strategy": "fsb",
    },
    {
        "id":       "IFAC",
        "name":     "IFAC",
        "fullname": "國際會計師聯合會",
        "url":      "https://www.ifac.org/news-resources?type=publication",
        "base_url": "https://www.ifac.org",
        "strategy": "generic",
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

DATA_PATH            = Path(__file__).parent.parent / "data" / "reports.json"
CUSTOM_SOURCES_PATH  = Path(__file__).parent.parent / "data" / "custom_sources.json"

# ─────────────────────────────────────────
#  報告識別：過濾掉導覽連結、只留真正的報告
# ─────────────────────────────────────────

# 真正報告的關鍵字（標題中含有這些才算）
REPORT_KEYWORDS = [
    "report", "guidance", "recommendation", "consultation", "standard",
    "framework", "principles", "assessment", "review", "survey",
    "statement", "policy", "regulation", "code", "handbook",
    "implementation", "monitoring", "disclosure", "final report",
    "discussion paper", "working paper", "exposure draft",
]

# 導覽頁面的排除關鍵字
NAV_KEYWORDS = [
    "home", "about", "contact", "login", "search", "menu", "cookie",
    "privacy", "sitemap", "subscribe", "newsletter", "follow us",
    "twitter", "linkedin", "facebook", "youtube", "read more",
    "view all", "back to", "return to", "members area", "hub",
    "committee", "who we are", "careers", "events", "annual meeting",
    "investor education", "capacity building", "training",
    "media release", "press release", "news",
]

# IOSCO 報告編號格式：FR/XX/YYYY 或 CR/XX/YYYY
IOSCO_REPORT_PATTERN = re.compile(
    r'\b(FR|CR|MR|ER|OR)\s*[/\-]\s*\d+\s*[/\-]\s*\d{4}\b',
    re.IGNORECASE
)

# 日期格式識別
DATE_PATTERNS = [
    re.compile(r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b', re.I),
    re.compile(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b', re.I),
    re.compile(r'\b(\d{4})[/\-](\d{2})[/\-](\d{2})\b'),
    re.compile(r'\b(\d{4})\b'),
]

MONTH_MAP = {
    'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
    'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12'
}

def parse_date(text: str) -> str:
    """從文字中提取日期，回傳 YYYY-MM-DD 或 YYYY"""
    for pat in DATE_PATTERNS[:3]:
        m = pat.search(text)
        if m:
            g = m.groups()
            if len(g) == 3 and g[0].isdigit() and len(g[0]) == 4:
                return f"{g[0]}-{g[1]}-{g[2]}"
            if len(g) == 3:
                day = g[0].zfill(2)
                mon = MONTH_MAP.get(g[1][:3].lower(), '01')
                return f"{g[2]}-{mon}-{day}"
            if len(g) == 2:
                mon = MONTH_MAP.get(g[0][:3].lower(), '01')
                return f"{g[1]}-{mon}"
    # 只有年份
    m = DATE_PATTERNS[3].search(text)
    if m:
        yr = m.group(1)
        if 2010 <= int(yr) <= 2030:
            return yr
    return ""


def is_likely_report(title: str) -> bool:
    """判斷標題是否為真正的報告（非導覽連結）"""
    t_lower = title.lower()
    if any(k in t_lower for k in NAV_KEYWORDS):
        return False
    if len(title) < 15:
        return False
    # 有報告關鍵字則通過
    if any(k in t_lower for k in REPORT_KEYWORDS):
        return True
    # 有 IOSCO 編號格式
    if IOSCO_REPORT_PATTERN.search(title):
        return True
    # 包含年份且夠長（可能是報告標題）
    if re.search(r'\b20\d{2}\b', title) and len(title) > 25:
        return True
    return False


def make_id(title: str, source: str) -> str:
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]


# ─────────────────────────────────────────
#  各機構的爬取策略
# ─────────────────────────────────────────

def scrape_iosco(html: str, base_url: str) -> list[dict]:
    """IOSCO 專用：識別 FR/CR 編號格式的報告"""
    soup = BeautifulSoup(html, "lxml")
    reports = []
    seen = set()

    # IOSCO 報告通常是 "FR/18/2025 Title — Date" 格式
    # 找所有文字區塊
    full_text = soup.get_text(" ", strip=True)
    lines = [l.strip() for l in full_text.split('\n') if l.strip()]

    # 同時找帶有 iosco 編號的連結
    for a in soup.find_all("a", href=True):
        href  = a["href"].strip()
        title = a.get_text(" ", strip=True)

        # 找包含報告編號的連結
        parent_text = ""
        parent = a.parent
        for _ in range(4):
            if not parent:
                break
            parent_text = parent.get_text(" ", strip=True)
            break

        # 嘗試從父元素文字中找編號
        code_match = IOSCO_REPORT_PATTERN.search(parent_text or title)
        if not code_match and not is_likely_report(title):
            continue
        if title in seen or len(title) < 10:
            continue

        # 組合標題：若父元素有編號，加在標題前
        full_title = title
        if code_match and code_match.group(0) not in title:
            full_title = f"{code_match.group(0)} {title}"

        # 補全 URL
        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            continue

        # 過濾 PDF 以外的無關連結
        if any(x in href for x in ["#", "javascript:", "mailto:"]):
            continue

        seen.add(title)
        date = parse_date(parent_text)

        reports.append({
            "id":       make_id(full_title, "IOSCO"),
            "source":   "IOSCO",
            "title_en": full_title,
            "title_zh": "",
            "intro":    "",
            "url":      href,
            "date_raw": date,
        })

    print(f"  → IOSCO 找到 {len(reports)} 則")
    return reports[:40]


def scrape_fsb(html: str, base_url: str) -> list[dict]:
    """FSB 專用爬取"""
    soup = BeautifulSoup(html, "lxml")
    reports = []
    seen = set()

    # FSB 出版品通常在文章列表中
    for item in soup.select("article, .publication, .post, li"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        href  = a["href"].strip()

        if not title or title in seen:
            continue
        if not is_likely_report(title):
            continue

        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            continue

        seen.add(title)
        date = parse_date(item.get_text(" ", strip=True))

        reports.append({
            "id":       make_id(title, "FSB"),
            "source":   "FSB",
            "title_en": title,
            "title_zh": "",
            "intro":    "",
            "url":      href,
            "date_raw": date,
        })

    print(f"  → FSB 找到 {len(reports)} 則")
    return reports[:40]


def scrape_generic(html: str, source_id: str, base_url: str) -> list[dict]:
    """通用爬取策略"""
    soup = BeautifulSoup(html, "lxml")

    # 移除干擾元素
    for tag in soup.select("nav, footer, header, .nav, .menu, .sidebar, script, style, .cookie-banner"):
        tag.decompose()

    reports = []
    seen = set()

    # 優先找有標題結構的連結
    for sel in ["article a", "h2 a", "h3 a", "h4 a", ".title a", ".publication a",
                ".pub-title a", ".views-row a", "li a", "td a"]:
        for a in soup.select(sel):
            title = a.get_text(strip=True)
            href  = a.get("href", "").strip()

            if not title or title in seen or len(title) < 15:
                continue
            if not is_likely_report(title):
                continue

            if href.startswith("/"):
                href = base_url + href
            elif not href.startswith("http"):
                continue
            if any(x in href for x in ["#", "javascript:", "mailto:"]):
                continue

            seen.add(title)
            parent_text = ""
            p = a.parent
            for _ in range(3):
                if not p: break
                parent_text = p.get_text(" ", strip=True)
                p = p.parent

            reports.append({
                "id":       make_id(title, source_id),
                "source":   source_id,
                "title_en": title,
                "title_zh": "",
                "intro":    "",
                "url":      href,
                "date_raw": parse_date(parent_text),
            })

        if len(reports) >= 10:
            break

    print(f"  → {source_id} 找到 {len(reports)} 則")
    return reports[:40]


def fetch_and_parse(src: dict) -> list[dict]:
    """抓取並解析單一來源"""
    print(f"\n[{src['id']}] 抓取：{src['url']}")
    try:
        resp = requests.get(src["url"], headers=HEADERS, timeout=25)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except Exception as e:
        print(f"  ⚠️  抓取失敗：{e}")
        return []

    strategy = src.get("strategy", "generic")
    if strategy == "iosco":
        return scrape_iosco(html, src["base_url"])
    elif strategy == "fsb":
        return scrape_fsb(html, src["base_url"])
    else:
        return scrape_generic(html, src["id"], src["base_url"])


# ─────────────────────────────────────────
#  Claude 翻譯 + 摘要
# ─────────────────────────────────────────

def translate_batch(reports: list[dict], client: anthropic.Anthropic) -> list[dict]:
    if not reports:
        return reports

    BATCH_SIZE = 10
    for i in range(0, len(reports), BATCH_SIZE):
        batch = reports[i: i + BATCH_SIZE]
        titles_block = "\n".join(
            f"{j+1}. {r['title_en']}" for j, r in enumerate(batch)
        )
        prompt = f"""以下是國際監理機構最新發布的報告或出版品標題（英文）。
請針對每一則：
1. 提供專業的繁體中文翻譯標題
2. 用 2 句繁體中文說明報告主要內容與監理重要性

以 JSON 陣列回覆，每個物件含：index（從1起）、title_zh、intro
只輸出 JSON，不要任何其他文字。

{titles_block}"""

        try:
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end != -1:
                items = json.loads(raw[start:end+1])
                for item in items:
                    idx = item.get("index", 0) - 1
                    if 0 <= idx < len(batch):
                        batch[idx]["title_zh"] = item.get("title_zh", "")
                        batch[idx]["intro"]     = item.get("intro", "")
            print(f"  ✓ 翻譯批次 {i//BATCH_SIZE+1} 完成（{len(batch)} 則）")
        except Exception as e:
            print(f"  ⚠️  翻譯失敗：{e}")
            for r in batch:
                r.setdefault("title_zh", "（翻譯失敗）")
                r.setdefault("intro", "")
        time.sleep(1)

    return reports


# ─────────────────────────────────────────
#  主流程
# ─────────────────────────────────────────

def load_existing() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": "", "sources_meta": {}, "reports": []}


def load_custom_sources() -> list:
    if CUSTOM_SOURCES_PATH.exists():
        try:
            return json.loads(CUSTOM_SOURCES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("❌ 未設定 ANTHROPIC_API_KEY，中止執行")
        return

    client   = anthropic.Anthropic(api_key=api_key)
    existing = load_existing()
    seen_ids = {r["id"] for r in existing.get("reports", [])}
    all_reports = list(existing.get("reports", []))
    sources = DEFAULT_SOURCES + load_custom_sources()
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*55}")
    print(f"  監理報告抓取開始 — {run_time}")
    print(f"  來源數量：{len(sources)}")
    print(f"{'='*55}")

    for src in sources:
        candidates = fetch_and_parse(src)
        new_ones = [r for r in candidates if r["id"] not in seen_ids]
        print(f"  新增 {len(new_ones)} 則（略過 {len(candidates)-len(new_ones)} 則重複）")

        if new_ones:
            new_ones = translate_batch(new_ones, client)
            for r in new_ones:
                seen_ids.add(r["id"])
            all_reports = new_ones + all_reports

    sources_meta = {
        src["id"]: {"name": src["name"], "fullname": src.get("fullname",""), "url": src["url"]}
        for src in sources
    }

    output = {
        "last_updated": run_time,
        "total":        len(all_reports),
        "sources_meta": sources_meta,
        "reports":      all_reports,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*55}")
    print(f"  ✅ 完成！共 {len(all_reports)} 則報告（含歷史累積）")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
