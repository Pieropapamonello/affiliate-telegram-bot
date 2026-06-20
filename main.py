#!/usr/bin/env python3
"""
Multi-Store Affiliate Bot for Telegram

Invia un link di un qualsiasi negozio online e ricevi un link affiliato.

- Amazon: usa il tag affiliato nativo (AFFILIATE_TAG) -> commissioni migliori.
- Altri store: usa una rete aggregatrice (Sovrn/Skimlinks, Admitad, Awin, ...)
  tramite un "deeplink template" configurabile, oppure il Publisher ID Skimlinks.
- Accorciamento opzionale tramite YOURLS.
"""

import os
import logging
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from telegram import Update
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
# Opzione 1 (semplice): imposta SKIMLINKS_ID con il tuo Publisher ID Sovrn/Skimlinks.
# Opzione 2 (flessibile): imposta DEEPLINK_TEMPLATE con {url} come segnaposto, es:
#   https://go.skimresources.com/?id=XXXXXX&xs=1&url={url}
#   https://ad.admitad.com/g/CAMPAIGN/?ulp={url}
SKIMLINKS_ID = os.environ.get("SKIMLINKS_ID", "")
DEEPLINK_TEMPLATE = os.environ.get("DEEPLINK_TEMPLATE", "")

# Accorciatore YOURLS (opzionale)
YOURLS_URL = os.environ.get("YOURLS_URL", "").rstrip("/")
YOURLS_SIGNATURE = os.environ.get("YOURLS_SIGNATURE", "")

PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

# Costruisci il template aggregatore da SKIMLINKS_ID se non gia' fornito.
if not DEEPLINK_TEMPLATE and SKIMLINKS_ID:
    DEEPLINK_TEMPLATE = f"https://go.skimresources.com/?id={SKIMLINKS_ID}&xs=1&url={{url}}"

logger.info("Bot Configuration:")
logger.info(f"  TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:10]}...")
logger.info(f"  AFFILIATE_TAG (Amazon): {AFFILIATE_TAG or '(non impostato)'}")
logger.info(f"  Aggregatore: {'attivo' if DEEPLINK_TEMPLATE else 'NON configurato'}")
logger.info(f"  YOURLS: {'attivo' if (YOURLS_URL and YOURLS_SIGNATURE) else 'disattivo'}")
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
    url_pattern = r"https?://[^\s\)\]]+"
    urls = re.findall(url_pattern, text or "")
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
                resolved = str(response.url)
                logger.info(f"Resolved {url} -> {resolved}")
                return resolved
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

        # Mantieni il dominio originale (amazon.it/.com/.de ...) se riconoscibile
        domain = parsed.netloc.lower().replace("www.", "")
        tld = domain.split("amazon.")[-1] if "amazon." in domain else "it"

        if asin:
            normalized = f"https://www.amazon.{tld}/dp/{asin}"
            if preserved:
                normalized += "?" + urlencode(preserved)
            logger.info(f"Normalized URL: {normalized}")
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

    # Nessun aggregatore configurato: ritorna il link originale.
    return url, "none"


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

    # Store generico: Open Graph
    info = dict(EMPTY_INFO)
    info["title"] = meta_content(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    info["image"] = meta_content(soup, "og:image")
    info["price"] = (
        meta_content(soup, "product:price:amount")
        or meta_content(soup, "og:price:amount")
    )
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
            logger.error(f"YOURLS error: {result.get('message', 'Unknown')}")
            return url
    except Exception as e:
        logger.error(f"YOURLS error: {e}")
        return url


# ----------------------------------------------------------------------------
# Messaggio
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
# Handlers
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "👋 Ciao! Sono il tuo Bot per i Link Affiliati.\n\n"
        "📝 Inviami il link di un prodotto di un qualsiasi negozio online "
        "e ti restituisco un link affiliato pronto da condividere.\n\n"
        "• 🛒 Amazon e altri store supportati\n"
        "• 📸 Immagine e info prodotto (quando disponibili)\n"
        "• 🔗 Link accorciato\n\n"
        "🚀 Inviami un link!"
    )
    await update.message.reply_text(welcome)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user = update.message.from_user

    original_url = extract_first_url(text)
    if not original_url:
        return

    status_msg = await update.message.reply_text("⏳ Elaborando...")
    try:
        logger.info(f"URL from {user.username}: {original_url}")

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
                await chat.send_photo(photo=info["image"], caption=message, parse_mode="HTML")
                return
            except Exception as e:
                logger.warning(f"Photo error: {e}")
        await chat.send_message(message, parse_mode="HTML")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
