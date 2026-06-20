#!/usr/bin/env python3
"""
Multi-Store Affiliate Bot for Telegram + Automazione (stile Doublegram/Afflow)

Funzioni:
- Link affiliato per qualsiasi store (Amazon nativo + aggregatore).
- Monitor sconti: watchlist di prodotti con controllo prezzi periodico e alert.
- Auto-post di offerte su un canale Telegram.
- Copy generato con AI (Claude) per i post.
- Accorciamento opzionale tramite YOURLS.
"""

import os
import json
import logging
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Configurazione (tutto via variabili d'ambiente)
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Amazon (tag affiliato nativo)
AFFILIATE_TAG = os.environ.get("AFFILIATE_TAG", "")

# Aggregatore per "qualsiasi store".
SKIMLINKS_ID = os.environ.get("SKIMLINKS_ID", "")
DEEPLINK_TEMPLATE = os.environ.get("DEEPLINK_TEMPLATE", "")

# Accorciatore YOURLS (opzionale)
YOURLS_URL = os.environ.get("YOURLS_URL", "").rstrip("/")
YOURLS_SIGNATURE = os.environ.get("YOURLS_SIGNATURE", "")

# Automazione
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # @canale o -100123... per auto-post
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", 60))  # ogni quanto controlla i prezzi
DISCOUNT_THRESHOLD = float(os.environ.get("DISCOUNT_THRESHOLD", 10))  # % calo minimo per alert
DATA_DIR = os.environ.get("DATA_DIR", ".")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")

# AI (Claude) per i testi dei post — opzionale
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-4-8")

PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

if not DEEPLINK_TEMPLATE and SKIMLINKS_ID:
    DEEPLINK_TEMPLATE = f"https://go.skimresources.com/?id={SKIMLINKS_ID}&xs=1&url={{url}}"

# Client AI (creato solo se la chiave è presente)
ai_client = None
if ANTHROPIC_API_KEY:
    try:
        from anthropic import AsyncAnthropic

        ai_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as e:
        logger.warning(f"AI non disponibile: {e}")

logger.info("Bot Configuration:")
logger.info(f"  TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:10]}...")
logger.info(f"  AFFILIATE_TAG (Amazon): {AFFILIATE_TAG or '(non impostato)'}")
logger.info(f"  Aggregatore: {'attivo' if DEEPLINK_TEMPLATE else 'NON configurato'}")
logger.info(f"  YOURLS: {'attivo' if (YOURLS_URL and YOURLS_SIGNATURE) else 'disattivo'}")
logger.info(f"  Canale auto-post: {CHANNEL_ID or '(non impostato)'}")
logger.info(f"  Monitor prezzi: ogni {CHECK_INTERVAL_MIN} min, soglia {DISCOUNT_THRESHOLD}%")
logger.info(f"  AI copy: {'attivo (' + AI_MODEL + ')' if ai_client else 'disattivo'}")
logger.info(f"  PORT: {PORT}")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

AMAZON_DOMAINS = ["amazon.", "amzn.eu", "amzn.to", "amzn.com", "amzlink.to", "a.co"]
SHORT_DOMAINS = ["amzn.eu", "amzn.com", "amzn.to", "amzlink.to", "a.co"]


# ----------------------------------------------------------------------------
# Health check server (Render Web Service ha bisogno di una porta aperta)
# ----------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass


def start_health_check_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server started on port {PORT}")


# ----------------------------------------------------------------------------
# Utility URL
# ----------------------------------------------------------------------------
def extract_first_url(text: str) -> str:
    urls = re.findall(r"https?://[^\s\)\]]+", text or "")
    for url in urls:
        return url.rstrip(")].,")
    return None


def is_amazon_url(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(d in domain for d in AMAZON_DOMAINS)
    except Exception:
        return False


def is_short_url(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(d in domain for d in SHORT_DOMAINS)
    except Exception:
        return False


async def resolve_short_url(url: str) -> str:
    for user_agent in USER_AGENTS:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": user_agent})
                return str(response.url)
        except Exception:
            continue
    return url


def extract_asin_from_url(url: str) -> str:
    for pattern in (r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"/d/([A-F0-9]+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def normalize_amazon_url(url: str) -> str:
    try:
        url = url.rstrip("/").replace("?&", "?")
        asin = extract_asin_from_url(url)
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        preserved = {}
        for param in ["smid", "condition", "psc", "aod", "m", "s"]:
            if param in query_params:
                preserved[param] = query_params[param][0]

        domain = parsed.netloc.lower().replace("www.", "")
        tld = domain.split("amazon.")[-1] if "amazon." in domain else "it"

        if asin:
            normalized = f"https://www.amazon.{tld}/dp/{asin}"
            if preserved:
                normalized += "?" + urlencode(preserved)
            return normalized
        return url
    except Exception as e:
        logger.error(f"Error normalizing URL: {e}")
        return url


def add_amazon_tag(url: str, tag: str) -> str:
    url = url.rstrip("/")
    url = re.sub(r"[?&]tag=[^&]*", "", url)
    if not tag:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}tag={tag}"


def build_affiliate_link(url: str) -> tuple:
    """Ritorna (affiliate_url, store_kind). store_kind in {amazon, aggregator, none}."""
    if is_amazon_url(url):
        normalized = normalize_amazon_url(url)
        return add_amazon_tag(normalized, AFFILIATE_TAG), "amazon"
    if DEEPLINK_TEMPLATE:
        affiliate = DEEPLINK_TEMPLATE.replace("{url}", quote_plus(url))
        return affiliate, "aggregator"
    return url, "none"


def parse_price_to_float(price_str) -> float:
    if not price_str:
        return None
    s = re.sub(r"[^\d,.]", "", str(price_str))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Scraping prodotto (Amazon dettagliato, altri store via Open Graph)
# ----------------------------------------------------------------------------
async def fetch_html(url: str) -> str:
    for user_agent in USER_AGENTS:
        try:
            headers = {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return response.text
        except Exception as e:
            logger.warning(f"fetch_html error: {e}")
            continue
    return ""


EMPTY_INFO = {
    "title": None,
    "price": None,
    "rating": None,
    "reviews": None,
    "image": None,
    "condition_status": None,
    "promotion": None,
    "coupon": None,
}


async def get_product_info(url: str, is_amazon: bool) -> dict:
    html = await fetch_html(url)
    if not html:
        return dict(EMPTY_INFO)
    soup = BeautifulSoup(html, "html.parser")

    if is_amazon:
        info = dict(EMPTY_INFO)
        info["title"] = extract_amazon_title(soup)
        info["price"] = extract_amazon_price(soup)
        info["rating"], info["reviews"] = extract_amazon_rating(soup)
        info["image"] = extract_amazon_image(soup)
        info["condition_status"] = detect_seller_condition(url, soup)
        info["promotion"] = extract_promotion(soup)
        info["coupon"] = extract_coupon(soup)
        return info

    info = dict(EMPTY_INFO)
    info["title"] = meta_content(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    info["image"] = meta_content(soup, "og:image")
    info["price"] = meta_content(soup, "product:price:amount") or meta_content(soup, "og:price:amount")
    return info


def meta_content(soup, prop: str) -> str:
    elem = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if elem and elem.get("content"):
        return elem["content"].strip()
    return None


def extract_amazon_title(soup) -> str:
    for selector in [{"id": "productTitle"}, {"class": "a-size-large"}]:
        elem = soup.find("span", selector)
        if elem:
            title = elem.get_text(strip=True)
            if title and len(title) > 5:
                return title
    return None


def extract_amazon_price(soup) -> str:
    try:
        container = soup.find("span", {"class": "a-price"})
        if container:
            prices = re.findall(r"[\d.,€$]+", container.get_text(strip=True))
            if prices:
                return prices[0]
        whole = soup.find("span", {"class": "a-price-whole"})
        if whole:
            return whole.get_text(strip=True)
    except Exception as e:
        logger.error(f"Error extracting price: {e}")
    return None


def extract_amazon_rating(soup) -> tuple:
    rating = reviews = None
    try:
        rating_elem = soup.find("span", {"class": "a-icon-star-small"}) or soup.find(
            "span", {"class": "a-icon-star"}
        )
        if rating_elem:
            inner = rating_elem.find("span")
            if inner:
                match = re.search(r"[\d,]+", inner.get_text(strip=True))
                if match:
                    rating = match.group(0)
        reviews_elem = soup.find("span", {"id": "acrCustomerReviewText"})
        if reviews_elem:
            match = re.search(r"[\d.]+", reviews_elem.get_text(strip=True).replace(".", ""))
            if match:
                reviews = match.group(0)
    except Exception:
        pass
    return rating, reviews


def extract_amazon_image(soup) -> str:
    for selector in [{"id": "landingImage"}, {"id": "imageBlockContainer"}, {"class": "a-dynamic-image"}]:
        elem = soup.find("img", selector)
        if elem and elem.get("src"):
            return elem["src"]
    return None


def extract_promotion(soup) -> str:
    try:
        for elem in soup.find_all(["span", "div", "a"]):
            text = elem.get_text(strip=True)
            if any(w in text.lower() for w in ["offerta", "sconto", "limited time", "deal", "promoz"]):
                if len(text) < 100:
                    return text
    except Exception:
        pass
    return None


def extract_coupon(soup) -> str:
    try:
        elem = soup.find("div", {"class": re.compile("coupon|promotion-badge", re.I)})
        if elem:
            text = elem.get_text(strip=True)
            if "coupon" in text.lower() or "sconto" in text.lower():
                return text
        for elem in soup.find_all(["span", "div"]):
            text = elem.get_text(strip=True)
            if "coupon" in text.lower() and len(text) < 150:
                return text
    except Exception:
        pass
    return None


def detect_seller_condition(url: str, soup) -> str:
    try:
        query_params = parse_qs(urlparse(url).query)
        smid = query_params.get("smid", [""])[0]
        aod = query_params.get("aod", [""])[0]
        s_param = query_params.get("s", [""])[0]
        if aod == "1":
            return "Usato - Venduto da terzo"
        if "warehouse-deals" in s_param.lower():
            return "Usato - Warehouse Deals Amazon"
        if not smid:
            return "Nuovo - Venduto da Amazon"
        if smid in ["A11IL2PNWYJU7H", "AQKAJJZN6SNBQ"]:
            return "Nuovo - Venduto da Amazon"
        seller = soup.find("div", {"id": "merchant-info"})
        if seller and "Amazon Seconda mano" in seller.get_text(strip=True):
            return "Usato - Venduto da Amazon Seconda mano"
        if smid:
            return "Usato - Venduto da terzo"
        return "Nuovo - Venduto da Amazon"
    except Exception as e:
        logger.error(f"Error detecting condition: {e}")
        return "Nuovo - Venduto da Amazon"


# ----------------------------------------------------------------------------
# YOURLS
# ----------------------------------------------------------------------------
async def shorten_with_yourls(url: str) -> str:
    if not (YOURLS_URL and YOURLS_SIGNATURE):
        return url
    try:
        api_url = f"{YOURLS_URL}/yourls-api.php"
        data = {
            "signature": YOURLS_SIGNATURE,
            "action": "shorturl",
            "format": "json",
            "url": url.replace("?&", "?"),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(api_url, data=data)
            result = response.json()
            if result.get("status") == "success":
                return result.get("shorturl")
            if "already exists" in result.get("message", ""):
                kw = result.get("url", {}).get("keyword")
                if kw:
                    return f"{YOURLS_URL}/{kw}"
            return url
    except Exception as e:
        logger.error(f"YOURLS error: {e}")
        return url


# ----------------------------------------------------------------------------
# AI copywriting (Claude)
# ----------------------------------------------------------------------------
async def generate_ai_copy(info: dict, price_line: str, link: str) -> str:
    if not ai_client:
        return None
    system = (
        "Sei un copywriter per un canale Telegram di offerte e affiliazione. "
        "Scrivi un post breve (massimo 500 caratteri), accattivante, in italiano, "
        "con qualche emoji, che invogli all'acquisto e crei urgenza in modo onesto. "
        "Inserisci il link così com'è (verrà reso cliccabile da Telegram). "
        "Rispondi SOLO con il testo del post, senza virgolette né spiegazioni."
    )
    user = (
        f"Prodotto: {info.get('title') or 'Prodotto in offerta'}\n"
        f"{price_line}\n"
        f"Valutazione: {info.get('rating') or 'n/d'}\n"
        f"Link: {link}"
    )
    try:
        resp = await ai_client.messages.create(
            model=AI_MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return text.strip() if text else None
    except Exception as e:
        logger.error(f"AI copy error: {e}")
        return None


# ----------------------------------------------------------------------------
# Messaggi
# ----------------------------------------------------------------------------
def build_product_message(info: dict, short_url: str, user_name: str = None) -> str:
    title = info.get("title") or "Prodotto"
    price = info.get("price") or ""
    rating = info.get("rating") or ""
    condition = info.get("condition_status") or ""
    promotion = info.get("promotion") or ""
    coupon = info.get("coupon") or ""

    clean_price = re.sub(r"€.*", "€", price).strip() if price else ""

    rating_stars = ""
    if rating:
        try:
            rating_stars = "⭐" * int(float(rating.replace(",", ".")))
        except Exception:
            rating_stars = f"⭐ {rating}/5"

    msg = ""
    if user_name:
        msg += f"<b>👤:</b> {user_name} ha condiviso questo articolo\n\n"
    msg += f"<b>📌 Articolo:</b>\n{title}\n\n"
    if condition:
        msg += f"<b>🔄 Venditore:</b> {condition}\n\n"
    if clean_price:
        msg += f"<b>💰 Prezzo:</b> {clean_price}\n\n"
    if rating_stars:
        msg += f"{rating_stars}\n\n"
    if promotion:
        msg += f"<b>🎉 Promozione:</b> {promotion}\n\n"
    if coupon:
        msg += f"<b>🎟️ Coupon:</b> {coupon}\n\n"
    msg += f"<b><a href='{short_url}'>👉 Clicca qui per acquistare</a></b>"
    return msg


# ----------------------------------------------------------------------------
# Watchlist (persistenza su file JSON)
# ----------------------------------------------------------------------------
def load_watchlist() -> list:
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(wl: list) -> None:
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"save_watchlist error: {e}")


# ----------------------------------------------------------------------------
# Pubblicazione offerta (canale o utente)
# ----------------------------------------------------------------------------
async def publish_deal(bot, entry: dict, info: dict, current_price: float, old_price: float = None):
    affiliate_url, _ = build_affiliate_link(entry["url"])
    short_url = await shorten_with_yourls(affiliate_url)

    if current_price is not None:
        if old_price and old_price > current_price:
            drop = round((1 - current_price / old_price) * 100)
            price_line = f"Prezzo: {current_price:.2f}€ (era {old_price:.2f}€, -{drop}%)"
        else:
            price_line = f"Prezzo: {current_price:.2f}€"
    else:
        price_line = "Prezzo: n/d"

    ai_text = await generate_ai_copy(info, price_line, short_url)
    target = entry.get("chat_id")
    destination = CHANNEL_ID or target

    if ai_text:
        body = ai_text
        if short_url not in body:
            body += f"\n\n{short_url}"
        if info.get("image"):
            try:
                await bot.send_photo(chat_id=destination, photo=info["image"], caption=body)
                return
            except Exception as e:
                logger.warning(f"send_photo error: {e}")
        await bot.send_message(chat_id=destination, text=body, disable_web_page_preview=False)
        return

    # Fallback senza AI: messaggio HTML strutturato
    info2 = dict(info)
    if current_price is not None and not info2.get("price"):
        info2["price"] = f"{current_price:.2f}€"
    message = build_product_message(info2, short_url)
    if old_price and current_price and old_price > current_price:
        drop = round((1 - current_price / old_price) * 100)
        message = f"<b>🔥 PREZZO IN CALO -{drop}%</b>\n\n" + message
    if info.get("image"):
        try:
            await bot.send_photo(chat_id=destination, photo=info["image"], caption=message, parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            logger.warning(f"send_photo error: {e}")
    await bot.send_message(chat_id=destination, text=message, parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------------
# Job: monitor prezzi
# ----------------------------------------------------------------------------
async def monitor_prices(context: ContextTypes.DEFAULT_TYPE) -> None:
    wl = load_watchlist()
    if not wl:
        return
    logger.info(f"Monitor prezzi: controllo {len(wl)} prodotti")
    for entry in wl:
        try:
            info = await get_product_info(entry["url"], is_amazon=entry.get("is_amazon", False))
            price = parse_price_to_float(info.get("price"))
            if price is None:
                continue
            entry["last_price"] = price
            baseline = entry.get("baseline_price")
            target = entry.get("target_price")

            alert = False
            if target and price <= target:
                alert = True
            elif baseline and price <= baseline * (1 - DISCOUNT_THRESHOLD / 100):
                alert = True

            if alert:
                logger.info(f"ALERT calo prezzo: {entry['url']} {baseline} -> {price}")
                await publish_deal(context.bot, entry, info, price, baseline)
                entry["baseline_price"] = price  # reset per evitare alert ripetuti
            elif not baseline:
                entry["baseline_price"] = price
        except Exception as e:
            logger.error(f"monitor error per {entry.get('url')}: {e}")
    save_watchlist(wl)


# ----------------------------------------------------------------------------
# Handlers comandi
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "👋 Ciao! Sono il tuo Bot Offerte & Affiliazione.\n\n"
        "🔗 Inviami il link di un prodotto (Amazon o altri store) e ti do il link affiliato.\n\n"
        "🤖 Funzioni di automazione:\n"
        "• /watch <link> [prezzo] – monitora un prodotto e avvisa al calo\n"
        "• /list – mostra i prodotti monitorati\n"
        "• /unwatch <numero> – rimuovi dalla lista\n"
        "• /deal <link> – pubblica subito un'offerta\n"
        "• /help – aiuto\n"
    )
    await update.message.reply_text(welcome)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /watch <link> [prezzo_target]\nEs: /watch https://amazon.it/dp/XXXX 19.90")
        return
    url = args[0]
    target_price = parse_price_to_float(args[1]) if len(args) > 1 else None

    if is_short_url(url):
        url = await resolve_short_url(url)

    info = await get_product_info(url, is_amazon=is_amazon_url(url))
    price = parse_price_to_float(info.get("price"))

    wl = load_watchlist()
    if any(e["url"] == url and e["chat_id"] == update.message.chat_id for e in wl):
        await update.message.reply_text("⚠️ Stai già monitorando questo prodotto.")
        return

    entry = {
        "chat_id": update.message.chat_id,
        "url": url,
        "is_amazon": is_amazon_url(url),
        "title": info.get("title"),
        "baseline_price": price,
        "last_price": price,
        "target_price": target_price,
    }
    wl.append(entry)
    save_watchlist(wl)

    title = info.get("title") or url
    parts = [f"✅ Monitoraggio attivato:\n<b>{title}</b>"]
    if price is not None:
        parts.append(f"Prezzo attuale: {price:.2f}€")
    if target_price is not None:
        parts.append(f"Ti avviso se scende sotto {target_price:.2f}€")
    else:
        parts.append(f"Ti avviso a ogni calo ≥ {DISCOUNT_THRESHOLD:.0f}%")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wl = [e for e in load_watchlist() if e["chat_id"] == update.message.chat_id]
    if not wl:
        await update.message.reply_text("La tua watchlist è vuota. Aggiungi con /watch <link>")
        return
    lines = ["<b>📋 Prodotti monitorati:</b>\n"]
    for i, e in enumerate(wl, 1):
        title = (e.get("title") or e["url"])[:60]
        price = f"{e['last_price']:.2f}€" if e.get("last_price") else "n/d"
        tgt = f" (target {e['target_price']:.2f}€)" if e.get("target_price") else ""
        lines.append(f"{i}. {title}\n   Prezzo: {price}{tgt}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /unwatch <numero> (vedi /list)")
        return
    idx = int(context.args[0])
    wl = load_watchlist()
    mine = [e for e in wl if e["chat_id"] == update.message.chat_id]
    if idx < 1 or idx > len(mine):
        await update.message.reply_text("Numero non valido. Controlla /list")
        return
    target = mine[idx - 1]
    wl.remove(target)
    save_watchlist(wl)
    await update.message.reply_text(f"🗑️ Rimosso: {(target.get('title') or target['url'])[:60]}")


async def deal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /deal <link>")
        return
    url = context.args[0]
    if is_short_url(url):
        url = await resolve_short_url(url)

    affiliate_url, store_kind = build_affiliate_link(url)
    if store_kind == "none":
        await update.message.reply_text(
            "⚠️ Store non configurato per l'affiliazione (manca SKIMLINKS_ID/DEEPLINK_TEMPLATE)."
        )
        return

    status = await update.message.reply_text("📢 Preparo l'offerta...")
    info = await get_product_info(url, is_amazon=(store_kind == "amazon"))
    price = parse_price_to_float(info.get("price"))
    entry = {"chat_id": update.message.chat_id, "url": url, "is_amazon": (store_kind == "amazon")}
    await publish_deal(context.bot, entry, info, price)
    try:
        await status.delete()
    except Exception:
        pass
    if CHANNEL_ID:
        await update.message.reply_text("✅ Offerta pubblicata sul canale.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user = update.message.from_user

    original_url = extract_first_url(text)
    if not original_url:
        return

    status_msg = await update.message.reply_text("⏳ Elaborando...")
    try:
        url = original_url
        if is_short_url(url):
            await status_msg.edit_text("🔗 Risolvendo link breve...")
            url = await resolve_short_url(url)

        affiliate_url, store_kind = build_affiliate_link(url)
        if store_kind == "none":
            await status_msg.edit_text(
                "⚠️ Questo store non è ancora configurato per l'affiliazione.\n"
                "Aggiungi un aggregatore (SKIMLINKS_ID o DEEPLINK_TEMPLATE) per supportarlo."
            )
            return

        await status_msg.edit_text("📸 Recupero info prodotto...")
        info = await get_product_info(url, is_amazon=(store_kind == "amazon"))

        await status_msg.edit_text("🔗 Accorciando...")
        short_url = await shorten_with_yourls(affiliate_url)

        message = build_product_message(info, short_url, user.first_name)
        await status_msg.delete()
        try:
            await update.message.delete()
        except Exception:
            pass

        chat = update.message.chat
        if info.get("image"):
            try:
                await chat.send_photo(photo=info["image"], caption=message, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                logger.warning(f"Photo error: {e}")
        await chat.send_message(message, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ Errore. Riprova.")
        except Exception:
            pass


def main():
    start_health_check_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("deal", deal_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    if app.job_queue:
        app.job_queue.run_repeating(monitor_prices, interval=CHECK_INTERVAL_MIN * 60, first=60)
        logger.info("Monitor prezzi schedulato")
    else:
        logger.warning("JobQueue non disponibile: installa python-telegram-bot[job-queue]")

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
