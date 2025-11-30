"""
RSS 文章抓取並轉換為 Markdown

1. 從 rss.json 讀取系列頁面 URL
2. 爬取系列頁面取得 RSS URL 和系列標題
3. 從 RSS 取得所有文章列表
4. 爬取每篇文章網頁內容
5. 轉換為 Markdown 並儲存到以系列標題命名的目錄
"""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from markdownify import markdownify as md
from playwright.async_api import async_playwright, Playwright


def sanitize_filename(title: str) -> str:
    """
    將標題轉換為合法的檔案/目錄名稱
    移除或替換不合法的字元
    """
    # 移除或替換 Windows/Unix 不合法的檔名字元
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    filename = re.sub(invalid_chars, "_", title)
    # 移除首尾空白和點號
    filename = filename.strip().strip(".")
    # 替換連續的底線為單一底線
    filename = re.sub(r"_+", "_", filename)
    # 限制檔名長度（避免過長）
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def load_rss_json(json_path: Path) -> list[str]:
    """
    從 rss.json 讀取系列頁面 URL 列表

    Args:
        json_path: rss.json 檔案路徑

    Returns:
        系列頁面 URL 列表
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("rss", [])
    except Exception as e:
        print(f"錯誤: 讀取 {json_path} 失敗 - {e}")
        return []


async def fetch_series_info_async(playwright: Playwright, series_url: str) -> dict | None:
    """
    使用 Playwright 爬取系列頁面，取得 RSS URL 和系列標題

    Args:
        playwright: Playwright 實例
        series_url: 系列頁面 URL

    Returns:
        包含 rss_url 和 series_title 的字典，失敗時返回 None
    """
    browser = await playwright.webkit.launch(headless=True)
    try:
        context = await browser.new_context()
        page = await context.new_page()

        response = await page.goto(series_url, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            print(f"  警告: 無法載入頁面 {series_url}")
            return None

        # 取得 RSS 連結 (class="btn-rss btn-no-border")
        rss_btn = page.locator("a.btn-rss.btn-no-border").first
        if await rss_btn.count() == 0:
            # 嘗試其他選擇器
            rss_btn = page.locator('a[href*="/rss/series/"]').first

        if await rss_btn.count() == 0:
            print(f"  警告: 在 {series_url} 中找不到 RSS 連結")
            return None

        rss_url = await rss_btn.get_attribute("href") or ""
        if rss_url and not rss_url.startswith("http"):
            rss_url = "https://ithelp.ithome.com.tw" + rss_url

        # 取得系列標題
        title_selectors = [
            "h3.qa-list__title",
            "h2.ir-profile-content__title",
            ".profile-header__name",
            "h1",
        ]

        series_title = None
        for selector in title_selectors:
            title_elem = page.locator(selector).first
            if await title_elem.count() > 0:
                series_title = await title_elem.inner_text()
                # 清理標題（移除「系列」後綴等）
                series_title = re.sub(r"\s*系列\s*$", "", series_title)
                break

        if not series_title:
            series_title = "Unknown Series"

        return {"rss_url": rss_url, "series_title": series_title}

    except Exception as e:
        print(f"  錯誤: 爬取 {series_url} 失敗 - {e}")
        return None
    finally:
        await browser.close()


async def fetch_rss_content_async(playwright: Playwright, rss_url: str) -> tuple[str, list[dict]]:
    """
    使用 Playwright 從 RSS URL 取得文章列表

    Args:
        playwright: Playwright 實例
        rss_url: RSS URL

    Returns:
        (系列標題, 文章列表) 元組
    """
    articles = []
    series_title = ""

    browser = await playwright.webkit.launch(headless=True)
    try:
        context = await browser.new_context()
        page = await context.new_page()

        # 使用 request 上下文來獲取原始響應
        api_context = context.request
        response = await api_context.get(rss_url)

        if response.status != 200:
            print(f"  警告: 無法載入 RSS {rss_url}, 狀態碼: {response.status}")
            return series_title, articles

        xml_content = await response.text()

        # 解析 XML
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            print(f"  錯誤: 解析 RSS 失敗 - {e}")
            return series_title, articles

        # 取得 channel 資訊
        channel = root.find("channel")
        if channel is None:
            print(f"  警告: RSS 中找不到 channel 元素")
            return series_title, articles

        # 取得系列標題
        title_elem = channel.find("title")
        if title_elem is not None and title_elem.text:
            series_title = title_elem.text
            # 清理標題
            series_title = re.sub(r"\s*::\s*\d+\s*iThome\s*鐵人賽.*$", "", series_title)

        # 遍歷所有 item
        for item in channel.findall("item"):
            item_title_elem = item.find("title")
            link_elem = item.find("link")

            title = item_title_elem.text if item_title_elem is not None else "Untitled"
            link = link_elem.text if link_elem is not None else ""

            # 清理連結（移除 RSS 追蹤參數）
            if link:
                link = link.split("?")[0]

            articles.append({"title": title, "link": link})

        print(f"  從 RSS 解析出 {len(articles)} 篇文章")

    except Exception as e:
        print(f"  錯誤: 處理 RSS 時發生未預期錯誤 - {e}")
    finally:
        await browser.close()

    return series_title, articles


async def fetch_article_content_async(playwright: Playwright, url: str) -> str:
    """
    使用 Playwright 抓取文章網頁的主要內容

    Args:
        playwright: Playwright 實例
        url: 文章 URL

    Returns:
        文章的 HTML 內容
    """
    browser = await playwright.webkit.launch(headless=True)
    try:
        context = await browser.new_context()
        page = await context.new_page()

        response = await page.goto(url, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            print(f"      警告: 無法載入頁面 {url}")
            return ""

        # iThome 文章主要內容的選擇器
        content_selectors = [
            "div.markdown-body",
            "div.qa-markdown",
            "article.article-content",
            "div.article-content",
        ]

        html_content = ""
        for selector in content_selectors:
            try:
                element = page.locator(selector).first
                if await element.count() > 0:
                    html_content = await element.inner_html()
                    break
            except Exception:
                continue

        return html_content

    except Exception as e:
        print(f"      錯誤: 抓取 {url} 時發生錯誤 - {e}")
        return ""
    finally:
        await browser.close()


def convert_html_to_markdown(html_content: str) -> str:
    """
    將 HTML 內容轉換為 Markdown 格式

    Args:
        html_content: HTML 字串

    Returns:
        Markdown 格式的字串
    """
    if not html_content:
        return ""

    # 使用 markdownify 轉換
    markdown_content = md(
        html_content,
        heading_style="ATX",
        strip=["script", "style", "button"],
    )
    return markdown_content.strip()


def save_article_as_markdown(
    title: str, link: str, markdown_content: str, output_dir: Path
) -> bool:
    """
    將單篇文章儲存為 Markdown 檔案

    Args:
        title: 文章標題
        link: 原始連結
        markdown_content: Markdown 格式的內容
        output_dir: 輸出目錄路徑

    Returns:
        是否成功儲存
    """
    # 建立完整的 Markdown 文件（包含標題和原始連結）
    full_content = f"# {title}\n\n"
    if link:
        full_content += f"> 原文連結: {link}\n\n"
    full_content += markdown_content

    # 產生安全的檔案名稱
    filename = sanitize_filename(title) + ".md"
    output_path = output_dir / filename

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(full_content)
        return True
    except Exception as e:
        print(f"錯誤: 儲存 {filename} 失敗 - {e}")
        return False


async def process_series_async(playwright: Playwright, series_url: str, base_output_dir: Path) -> int:
    """
    處理單一系列：取得 RSS、爬取文章、轉換並儲存

    Args:
        playwright: Playwright 實例
        series_url: 系列頁面 URL
        base_output_dir: 基礎輸出目錄

    Returns:
        成功處理的文章數量
    """
    print(f"\n處理系列: {series_url}")

    # Step 1: 取得系列資訊（RSS URL 和系列標題）
    print("  [Step 1] 取得系列資訊...")
    series_info = await fetch_series_info_async(playwright, series_url)
    if not series_info:
        print("  無法取得系列資訊，跳過此系列")
        return 0

    rss_url = series_info["rss_url"]
    series_title = series_info["series_title"]
    print(f"  RSS URL: {rss_url}")
    print(f"  系列標題: {series_title}")

    # Step 2: 從 RSS 取得文章列表
    print("  [Step 2] 取得 RSS 文章列表...")
    rss_series_title, articles = await fetch_rss_content_async(playwright, rss_url)

    # 如果從系列頁面取得的標題不完整，使用 RSS 的標題
    if series_title == "Unknown Series" and rss_series_title:
        series_title = rss_series_title

    if not articles:
        print("  沒有找到任何文章，跳過此系列")
        return 0

    # 建立系列目錄
    series_dir_name = sanitize_filename(series_title)
    output_dir = base_output_dir / series_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"  輸出目錄: {output_dir}")

    # Step 3: 爬取並轉換文章
    print(f"  [Step 3] 爬取並轉換 {len(articles)} 篇文章...")
    success_count = 0

    for i, article in enumerate(articles, 1):
        title = article["title"]
        link = article["link"]

        print(f"    處理中 ({i}/{len(articles)}): {title[:50]}...")

        if not link:
            print(f"      跳過: 沒有連結")
            continue

        # 抓取網頁內容
        html_content = await fetch_article_content_async(playwright, link)

        if not html_content:
            print(f"      警告: 無法取得內容")
            continue

        # 轉換為 Markdown
        markdown_content = convert_html_to_markdown(html_content)

        if not markdown_content:
            print(f"      警告: 轉換後內容為空")
            continue

        # 儲存檔案
        if save_article_as_markdown(title, link, markdown_content, output_dir):
            success_count += 1

    return success_count


async def main():
    """主程式入口"""
    # 設定路徑
    script_dir = Path(__file__).parent
    rss_json_path = script_dir / "rss.json"
    output_dir = script_dir / "articles"

    print("=" * 60)
    print("RSS 文章抓取並轉換為 Markdown")
    print("=" * 60)

    # Step 1: 讀取 rss.json
    print("\n[Phase 1] 讀取 rss.json...")
    series_urls = load_rss_json(rss_json_path)

    if not series_urls:
        print("沒有找到任何系列 URL，程式結束")
        return

    print(f"找到 {len(series_urls)} 個系列")

    # Step 2: 處理每個系列
    print("\n[Phase 2] 處理各系列...")
    total_success = 0

    async with async_playwright() as playwright:
        for series_url in series_urls:
            success_count = await process_series_async(playwright, series_url, output_dir)
            total_success += success_count

    print("\n" + "=" * 60)
    print(f"完成！成功儲存 {total_success} 篇文章")
    print(f"輸出目錄: {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
