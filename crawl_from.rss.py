"""
RSS 文章抓取並轉換為 Markdown

Phase 1: 從 ./rss 目錄讀取所有 RSS XML 檔案，解析文章標題與連結
Phase 2: 從文章 URL 抓取真實網頁內容，轉換為 Markdown 格式並存檔
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from markdownify import markdownify as md
from playwright.async_api import async_playwright, Playwright


def sanitize_filename(title: str) -> str:
    """
    將標題轉換為合法的檔案名稱
    移除或替換不合法的檔案名稱字元
    """
    # 移除或替換 Windows/Unix 不合法的檔名字元
    invalid_chars = r'[<>:"/\\|?*]'
    filename = re.sub(invalid_chars, "_", title)
    # 移除首尾空白
    filename = filename.strip()
    # 限制檔名長度（避免過長）
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def parse_rss_file(rss_path: Path) -> list[dict]:
    """
    解析單一 RSS XML 檔案，提取所有文章的標題與連結

    Args:
        rss_path: RSS XML 檔案路徑

    Returns:
        包含 title, link 的字典列表
    """
    articles = []

    try:
        tree = ET.parse(rss_path)
        root = tree.getroot()

        # 取得 channel 資訊
        channel = root.find("channel")
        if channel is None:
            print(f"警告: {rss_path} 中找不到 channel 元素")
            return articles

        # 遍歷所有 item
        for item in channel.findall("item"):
            title_elem = item.find("title")
            link_elem = item.find("link")

            title = title_elem.text if title_elem is not None else "Untitled"
            link = link_elem.text if link_elem is not None else ""

            # 清理連結（移除 RSS 追蹤參數）
            if link:
                link = link.split("?")[0]

            articles.append({"title": title, "link": link})

        print(f"從 {rss_path.name} 解析出 {len(articles)} 篇文章")

    except ET.ParseError as e:
        print(f"錯誤: 解析 {rss_path} 失敗 - {e}")
    except Exception as e:
        print(f"錯誤: 處理 {rss_path} 時發生未預期錯誤 - {e}")

    return articles


def scan_rss_directory(rss_dir: Path) -> list[dict]:
    """
    掃描 RSS 目錄，解析所有 XML 檔案

    Args:
        rss_dir: RSS 目錄路徑

    Returns:
        所有文章的列表
    """
    all_articles = []

    if not rss_dir.exists():
        print(f"錯誤: RSS 目錄 {rss_dir} 不存在")
        return all_articles

    xml_files = list(rss_dir.glob("*.xml"))
    print(f"找到 {len(xml_files)} 個 RSS 檔案")

    for xml_file in xml_files:
        articles = parse_rss_file(xml_file)
        all_articles.extend(articles)

    return all_articles


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


async def fetch_article_content(playwright: Playwright, url: str) -> str:
    """
    使用 Playwright 抓取文章網頁的主要內容

    Args:
        playwright: Playwright 實例
        url: 文章 URL

    Returns:
        文章的 HTML 內容
    """
    webkit = playwright.webkit
    browser = await webkit.launch(headless=True)
    try:
        context = await browser.new_context()
        page = await context.new_page()

        response = await page.goto(url, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            print(f"  警告: 無法載入頁面 {url}")
            return ""

        # iThome 文章主要內容的選擇器
        # 文章內容通常在 .markdown-body 或 .qa-markdown 內
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
        print(f"  錯誤: 抓取 {url} 時發生錯誤 - {e}")
        return ""
    finally:
        await browser.close()


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


async def process_articles(articles: list[dict], output_dir: Path) -> int:
    """
    處理所有文章：抓取網頁內容並轉換為 Markdown

    Args:
        articles: 文章列表
        output_dir: 輸出目錄

    Returns:
        成功處理的文章數量
    """
    # 建立輸出目錄
    output_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0

    async with async_playwright() as playwright:
        for i, article in enumerate(articles, 1):
            title = article["title"]
            link = article["link"]

            print(f"  處理中 ({i}/{len(articles)}): {title[:50]}...")

            if not link:
                print(f"    跳過: 沒有連結")
                continue

            # 抓取網頁內容
            html_content = await fetch_article_content(playwright, link)

            if not html_content:
                print(f"    警告: 無法取得內容")
                continue

            # 轉換為 Markdown
            markdown_content = convert_html_to_markdown(html_content)

            if not markdown_content:
                print(f"    警告: 轉換後內容為空")
                continue

            # 儲存檔案
            if save_article_as_markdown(title, link, markdown_content, output_dir):
                success_count += 1

    return success_count


async def main():
    """主程式入口"""
    # 設定路徑
    script_dir = Path(__file__).parent
    rss_dir = script_dir / "rss"
    output_dir = script_dir / "output"

    print("=" * 50)
    print("RSS 文章抓取並轉換為 Markdown")
    print("=" * 50)

    # Phase 1: 掃描並解析 RSS 檔案
    print("\n[Phase 1] 掃描 RSS 目錄並解析文章...")
    articles = scan_rss_directory(rss_dir)
    print(f"總共找到 {len(articles)} 篇文章")

    if not articles:
        print("沒有找到任何文章，程式結束")
        return

    # 顯示文章清單
    print("\n文章清單:")
    for i, article in enumerate(articles, 1):
        print(f"  {i}. {article['title']}")

    # Phase 2: 抓取網頁並轉換為 Markdown
    print(f"\n[Phase 2] 從網頁抓取內容並儲存至 {output_dir}...")

    success_count = await process_articles(articles, output_dir)

    print("\n" + "=" * 50)
    print(f"完成！成功儲存 {success_count}/{len(articles)} 篇文章")
    print(f"輸出目錄: {output_dir}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
