#!/usr/bin/env python3
"""
Moldova News Bot — автоматический сбор, рерайт и публикация новостей в Telegram
"""

import os
import json
import time
import hashlib
import logging
import requests
import feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SEEN_FILE = "seen_articles.json"
MAX_NEW_PER_RUN = 5

client = Anthropic(api_key=ANTHROPIC_API_KEY)

RSS_FEEDS = [
    {"url": "https://newsmaker.md/rss/", "lang": "ru", "name": "Newsmaker"},
    {"url": "https://www.nokta.md/feed/", "lang": "ru", "name": "Nokta"},
    {"url": "https://point.md/ru/rss/news", "lang": "ru", "name": "Point.md"},
    {"url": "https://ru.locals.md/rss", "lang": "ru", "name": "Locals"},
    {"url": "https://tv8.md/feed/", "lang": "ru", "name": "TV8"},
    {"url": "https://www.zdg.md/feed/", "lang": "ro", "name": "ZdG"},
    {"url": "https://stiri.md/rss", "lang": "ro", "name": "Stiri.md"},
    {"url": "https://moldova.org/feed", "lang": "ro", "name": "Moldova.org"},
]

def load_seen() -> set:
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen: set):
    data = list(seen)[-2000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)

def article_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()

def fetch_full_text(url: str) -> str:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form"]):
            tag.decompose()
        for selector in ["article", ".article-body", ".post-content", ".entry-content", ".content", "main"]:
            block = soup.select_one(selector)
            if block:
                text = block.get_text(" ", strip=True)
                if len(text) > 200:
                    return text[:3000]
        return soup.get_text(" ", strip=True)[:3000]
    except Exception as e:
        log.warning(f"Не удалось загрузить текст {url}: {e}")
        return ""

def fetch_image_url(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            return tw["content"]
    except Exception:
        pass
    return None

def collect_articles(seen: set) -> list[dict]:
    articles = []
    for feed_cfg in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:15]:
                url = entry.get("link", "")
                if not url:
                    continue
                h = article_hash(url)
                if h in seen:
                    continue
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", "").strip()
                summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
                articles.append({"hash": h, "url": url, "title": title, "summary": summary[:500], "source": feed_cfg["name"], "lang": feed_cfg["lang"]})
        except Exception as e:
            log.warning(f"Ошибка чтения RSS {feed_cfg['name']}: {e}")
    log.info(f"Найдено {len(articles)} новых статей")
    return articles[:MAX_NEW_PER_RUN * 3]

REWRITE_PROMPT = """Ты редактор молдавского новостного Telegram-канала.
Исходный материал может быть на румынском или русском — это не важно.
ВЕСЬ твой ответ должен быть ТОЛЬКО на русском языке, без исключений.

Перепиши новость для Telegram в таком формате:

**Заголовок** (цепляющий, до 80 символов)

Основной текст (2-4 абзаца, живой журналистский стиль, без воды, только факты + контекст). Используй эмодзи умеренно. Не копируй оригинал дословно. Если исходник на румынском — сначала переведи, затем перепиши.

#хэштег1 #хэштег2 #хэштег3 (3-5 релевантных тегов на русском)

Источник: {source}

---
Язык оригинала: {lang}
Оригинальный заголовок: {title}
Краткое содержание: {summary}
Полный текст: {full_text}
"""

def rewrite_article(article: dict) -> str | None:
    full_text = fetch_full_text(article["url"])
    lang_label = "румынский" if article["lang"] == "ro" else "русский"
    prompt = REWRITE_PROMPT.format(source=article["source"], lang=lang_label, title=article["title"], summary=article["summary"], full_text=full_text or article["summary"])
    try:
        msg = client.messages.create(model="claude-opus-4-5", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Ошибка Claude API: {e}")
        return None

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_telegram(text: str, image_url: str | None = None) -> bool:
    if image_url:
        r = requests.post(f"{TG_API}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": text[:1024], "parse_mode": "Markdown"}, timeout=15)
    else:
        r = requests.post(f"{TG_API}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4096], "parse_mode": "Markdown", "disable_web_page_preview": False}, timeout=15)
    ok = r.status_code == 200 and r.json().get("ok")
    if not ok:
        log.error(f"Telegram error: {r.text}")
    return ok

def main():
    log.info("▶ Запуск Moldova News Bot")
    seen = load_seen()
    articles = collect_articles(seen)
    published = 0
    for art in articles:
        if published >= MAX_NEW_PER_RUN:
            break
        log.info(f"📰 Обработка: {art['title'][:60]}")
        rewritten = rewrite_article(art)
        if not rewritten:
            continue
        image_url = fetch_image_url(art["url"])
        success = send_telegram(rewritten, image_url)
        if success:
            seen.add(art["hash"])
            published += 1
            log.info(f"✅ Опубликовано [{published}/{MAX_NEW_PER_RUN}]")
            time.sleep(3)
        else:
            log.warning("⚠ Не удалось опубликовать")
    save_seen(seen)
    log.info(f"✔ Готово. Опубликовано: {published} постов")

if __name__ == "__main__":
    main()
