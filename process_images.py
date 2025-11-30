"""
處理 Markdown 文章中的圖片

掃描 articles 目錄下所有 Markdown 文章，
下載圖片到 media 目錄並更新文章中的圖片路徑
"""

import asyncio
from crawl_from_rss import process_images_main

if __name__ == "__main__":
    asyncio.run(process_images_main())
