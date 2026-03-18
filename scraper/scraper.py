"""
監理報告自動抓取腳本
每週日由 GitHub Actions 執行，結果寫入 data/reports.json
"""

import os
import json
import time
import hashlib
import requests
import anthropic
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

# ─────────────────────────────────────────
#  設定：預設搜尋來源
# ─────────────────────────────────────────
DEFAULT_SOURCES = [
    {
        "id":       "IOSCO",
        "name":     "IOSCO",
        "fullname": "國際證券事務監察委員會組織",
        "url":      "https://www.iosco.org/publications/",
        "base_url": "https://www.iosco.org",
    },
    {
        "id":       "IESBA",
        "name":     "IESBA",
        "fullname": "國際會計師倫理準則委員會",
        "url":      "https://www.ethicsboard.org/publications",
        "base_url": "https://www.ethicsboard.org",
    },
    {
        "id":       "FSB",
        "name":     "FSB",
        "fullname": "金融穩定委員會",
        "url":      "https://www.fsb.org/publications/",
        "base_url": "https://www.fsb.org",
    },
    {
        "id":       "IFAC",
        "name":     "IFAC",
        "fullname": "國際會計師聯合會",
        "url":      "https://www.ifac.org/news-resources",
        "base_url": "https://www.ifac.org",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DATA_PATH = Path(__file__).parent.parent / "data" / "reports.json"
CUSTOM_SOURCES_PATH = Path(__file__).parent.parent / "data" / "custom_sources.json"

# ─────────────────────────────────────────
#  工具函式
# ─────────────────────────────────────────
def make_id(title: str, source: str) -> str:
    """根據標題+來源產生唯一 ID"""
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]


def load_existing(path: Path) -> dict:
    """載入現有報告資料（避免重複）"""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": "", "sources_meta": {}, "reports": []}


def load_custom_sources() -> list:
    """載入使用者自訂網站清單"""
    if CUSTOM_SOURCES_PATH.exists():
        try:
            return json.loads(CUSTOM_SOURCES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


# ─────────────────────────────────────────
#  爬蟲
# ─────────────────────────────────────────
def fetch_page(url: str) -> str | None:
    """抓取網頁原始 HTML"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception as e:
        print(f"  ⚠️  抓取失敗 {url}：{e}")
        return None


def parse_reports_from_html(html: str, source_id: str, base_url: str) -> list[dict]:
    """從 HTML 解析報告連結清單"""
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    seen = set()

    # 移除導覽列、頁尾等干擾元素
    for tag in soup.select("nav, footer, header, .nav, .menu, .sidebar, .cookie, script, style"):
        tag.decompose()

    # 抓所有 <a> 連結
    for a in soup.find_all("a", href=True):
        href  = a["href"].strip()
        title = a.get_text(strip=True)

        # 過濾條件
        if not title or len(title) < 12:
            continue
        if title in seen:
            continue
        if any(w in title.lower() for w in [
            "home", "about", "contact", "login", "search", "menu",
            "cookie", "privacy", "sitemap", "subscribe", "follow",
            "twitter", "linkedin", "facebook", "youtube", "read more",
            "view all", "back to", "return to"
        ]):
            continue
        # 只要英文或中文標題（過濾純數字、純符號）
        if not any(c.isalpha() for c in title):
            continue

        # 補全 URL
        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            continue

        seen.add(title)

        # 嘗試找日期（找父元素的文字或 time 標籤）
        date_str = ""
        parent = a.parent
        for _ in range(3):  # 往上找三層
            if not parent:
                break
            t = parent.find("time")
            if t:
                date_str = t.get("datetime", t.get_text(strip=True))
                break
            # 尋找包含年份的文字
            text = parent.get_text(" ", strip=True)
            import re
            m = re.search(r"\b(20\d{2})[^\d]", text)
            if m:
                date_str = m.group(1)
                break
            parent = parent.parent

        candidates.append({
            "id":         make_id(title, source_id),
            "source":     source_id,
            "title_en":   title,
            "title_zh":   "",
            "intro":      "",
            "url":        href,
            "date_raw":   date_str,
            "translated": False,
        })

    print(f"  → 找到 {len(candidates)} 個候選項目")
    return candidates[:40]  # 每個來源最多 40 筆送翻譯


# ─────────────────────────────────────────
#  Claude 翻譯 + 摘要
# ─────────────────────────────────────────
def translate_batch(reports: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """呼叫 Claude API 翻譯標題並生成簡介"""
    if not reports:
        return reports

    BATCH_SIZE = 10
    for i in range(0, len(reports), BATCH_SIZE):
        batch = reports[i : i + BATCH_SIZE]
        titles_block = "\n".join(
            f"{j+1}. {r['title_en']}" for j, r in enumerate(batch)
        )

        prompt = f"""以下是國際監理機構的報告標題清單（英文）。
請針對每一則：
1. 提供專業的繁體中文翻譯標題
2. 用 2 句話寫繁體中文簡介，說明主要內容與重要性（若僅憑標題無法判斷，請簡短說明可能的內容方向）

以 JSON 陣列格式回覆，每個物件包含：index（從1開始）、title_zh、intro
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
            start = raw.find("[")
            end   = raw.rfind("]")
            if start != -1 and end != -1:
                items = json.loads(raw[start:end+1])
                for item in items:
                    idx = item.get("index", 0) - 1
                    if 0 <= idx < len(batch):
                        batch[idx]["title_zh"]   = item.get("title_zh", "")
                        batch[idx]["intro"]       = item.get("intro", "")
                        batch[idx]["translated"]  = True
            print(f"  ✓ 翻譯完成批次 {i//BATCH_SIZE + 1}（{len(batch)} 則）")

        except Exception as e:
            print(f"  ⚠️  翻譯失敗：{e}")
            for r in batch:
                r["title_zh"] = r["title_zh"] or "（翻譯失敗）"
                r["intro"]    = r["intro"]    or ""

        time.sleep(1)

    return reports


# ─────────────────────────────────────────
#  主流程
# ─────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("❌ 未設定 ANTHROPIC_API_KEY，中止執行")
        return

    client     = anthropic.Anthropic(api_key=api_key)
    existing   = load_existing(DATA_PATH)
    seen_ids   = {r["id"] for r in existing.get("reports", [])}
    all_reports = list(existing.get("reports", []))

    # 合併預設 + 自訂來源
    sources = DEFAULT_SOURCES + load_custom_sources()
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*55}")
    print(f"  監理報告抓取開始 — {run_time}")
    print(f"  來源數量：{len(sources)}")
    print(f"{'='*55}")

    for src in sources:
        print(f"\n[{src['id']}] 抓取：{src['url']}")
        html = fetch_page(src["url"])
        if not html:
            continue

        candidates = parse_reports_from_html(html, src["id"], src["base_url"])

        # 只處理新的（不在現有資料中的）
        new_ones = [r for r in candidates if r["id"] not in seen_ids]
        print(f"  → 新增 {len(new_ones)} 則（已略過 {len(candidates)-len(new_ones)} 則重複）")

        if new_ones:
            print(f"  → 開始翻譯...")
            new_ones = translate_batch(new_ones, client)
            for r in new_ones:
                seen_ids.add(r["id"])
            all_reports = new_ones + all_reports  # 新的放前面

    # 更新 sources_meta
    sources_meta = {}
    for src in sources:
        sources_meta[src["id"]] = {
            "name":     src["name"],
            "fullname": src.get("fullname", src["name"]),
            "url":      src["url"],
        }

    # 寫出 JSON
    output = {
        "last_updated":  run_time,
        "total":         len(all_reports),
        "sources_meta":  sources_meta,
        "reports":       all_reports,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"\n{'='*55}")
    print(f"  ✅ 完成！共 {len(all_reports)} 則報告（含歷史）")
    print(f"  輸出：{DATA_PATH}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
