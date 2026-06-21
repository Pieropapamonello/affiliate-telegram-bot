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
import io
import json
import time
import asyncio
import logging
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from telegram import (
    Update,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# Amazon Creators API / PA-API (OAuth2) — dati prodotto affidabili. Opzionale.
PAAPI_CLIENT_ID = os.environ.get("PAAPI_CLIENT_ID", "")
PAAPI_CLIENT_SECRET = os.environ.get("PAAPI_CLIENT_SECRET", "")
PAAPI_MARKETPLACE = os.environ.get("PAAPI_MARKETPLACE", "www.amazon.it")
# Tag partner per le richieste PA-API (può differire dal tag dei link). Default: AFFILIATE_TAG.
PAAPI_PARTNER_TAG = os.environ.get("PAAPI_PARTNER_TAG", "") or AFFILIATE_TAG
PAAPI_SCOPE = os.environ.get("PAAPI_SCOPE", "creatorsapi::default")
PAAPI_TOKEN_URL = os.environ.get("PAAPI_TOKEN_URL", "https://api.amazon.com/auth/o2/token")
PAAPI_HOST = os.environ.get("PAAPI_HOST", "https://creatorsapi.amazon")

# Aggregatore per "qualsiasi store".
SKIMLINKS_ID = os.environ.get("SKIMLINKS_ID", "")
DEEPLINK_TEMPLATE = os.environ.get("DEEPLINK_TEMPLATE", "")
# Se true, anche i link Amazon passano da Skimlinks (uniforme) invece del tag nativo
ROUTE_ALL_VIA_SKIMLINKS = os.environ.get("ROUTE_ALL_VIA_SKIMLINKS", "").lower() in ("1", "true", "yes", "si")

# Accorciatori link (opzionali). Bitly con rotazione di più token, poi YOURLS, poi is.gd.
BITLY_TOKENS = [t.strip() for t in os.environ.get("BITLY_TOKENS", "").split(",") if t.strip()]
YOURLS_URL = os.environ.get("YOURLS_URL", "").rstrip("/")
YOURLS_SIGNATURE = os.environ.get("YOURLS_SIGNATURE", "")

# Automazione
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # @canale o -100123... per auto-post
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", 60))  # ogni quanto controlla i prezzi
DISCOUNT_THRESHOLD = float(os.environ.get("DISCOUNT_THRESHOLD", 10))  # % calo minimo per alert
ANTIDUP_DAYS = float(os.environ.get("ANTIDUP_DAYS", 2))  # non ripubblicare lo stesso prodotto entro N giorni
SCHEDULED_POST_HOURS = float(os.environ.get("SCHEDULED_POST_HOURS", 0))  # 0 = disattivato
DATA_DIR = os.environ.get("DATA_DIR", ".")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
# Password una-tantum per diventare admin dal bot (comando /admin <password>)
SETUP_PASSWORD = os.environ.get("SETUP_PASSWORD", "")

# Firestore (Google) per la persistenza della watchlist — opzionale
FIRESTORE_PROJECT_ID = os.environ.get("FIRESTORE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")

# Realtime Database (feed live delle offerte pubblicate) — opzionale
RTDB_URL = os.environ.get("RTDB_URL", "").rstrip("/")

# YouTube Data API (per trovare un video pertinente e corto) — opzionale
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
VIDEO_MAX_SECONDS = int(os.environ.get("VIDEO_MAX_SECONDS", 180))

# Immagine personalizzata (card) — prodotto + sfondo + scritta brand + badge store
BRAND_TEXT = os.environ.get("BRAND_TEXT", "Affari di Nello")

# AI per i testi dei post — opzionale. Provider in ordine: Groq > Gemini > Claude.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-4-8")

PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

if not DEEPLINK_TEMPLATE and SKIMLINKS_ID:
    DEEPLINK_TEMPLATE = f"https://go.skimresources.com/?id={SKIMLINKS_ID}&xs=1&url={{url}}"

# Client AI Claude (solo se la chiave è presente e nessun provider gratuito è configurato)
ai_client = None
if ANTHROPIC_API_KEY and not (GROQ_API_KEY or GEMINI_API_KEY):
    try:
        from anthropic import AsyncAnthropic

        ai_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as e:
        logger.warning(f"Claude non disponibile: {e}")

# Firestore / RTDB (inizializzati in init_firestore / init_rtdb)
firestore_db = None
USE_FIRESTORE = False
rtdb_creds = None


def _ai_status() -> str:
    if GROQ_API_KEY:
        return f"Groq ({GROQ_MODEL})"
    if GEMINI_API_KEY:
        return f"Gemini ({GEMINI_MODEL})"
    if ai_client:
        return f"Claude ({AI_MODEL})"
    return "disattivo (template)"


logger.info("Bot Configuration:")
logger.info(f"  TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:10]}...")
logger.info(f"  AFFILIATE_TAG (Amazon): {AFFILIATE_TAG or '(non impostato)'}")
logger.info(f"  Aggregatore: {'attivo' if DEEPLINK_TEMPLATE else 'NON configurato'}")
logger.info(f"  YOURLS: {'attivo' if (YOURLS_URL and YOURLS_SIGNATURE) else 'disattivo'}")
logger.info(f"  Canale auto-post: {CHANNEL_ID or '(non impostato)'}")
logger.info(f"  Monitor prezzi: ogni {CHECK_INTERVAL_MIN} min, soglia {DISCOUNT_THRESHOLD}%")
logger.info(f"  AI copy: {_ai_status()}")
logger.info(f"  DB: {'Firestore' if (FIRESTORE_PROJECT_ID or GOOGLE_CREDENTIALS_JSON) else 'file JSON'}")
logger.info(f"  Realtime DB: {'attivo' if (RTDB_URL and GOOGLE_CREDENTIALS_JSON) else 'disattivo'}")
logger.info(f"  Amazon PA-API: {'configurata' if (PAAPI_CLIENT_ID and PAAPI_CLIENT_SECRET) else 'disattiva (scraping)'}")
_short = "Bitly" if BITLY_TOKENS else ("YOURLS" if (YOURLS_URL and YOURLS_SIGNATURE) else "is.gd")
logger.info(f"  Accorciatore: {_short} ({len(BITLY_TOKENS)} token Bitly)")
logger.info(f"  PORT: {PORT}")


def init_firestore():
    """Inizializza Firestore se sono presenti le credenziali."""
    global firestore_db, USE_FIRESTORE
    if not (GOOGLE_CREDENTIALS_JSON or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
        return
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account

        if GOOGLE_CREDENTIALS_JSON:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(info)
            project = FIRESTORE_PROJECT_ID or info.get("project_id")
            firestore_db = firestore.Client(project=project, credentials=creds)
        else:
            firestore_db = firestore.Client(project=FIRESTORE_PROJECT_ID or None)
        USE_FIRESTORE = True
        logger.info("Firestore attivo")
    except Exception as e:
        logger.warning(f"Firestore non disponibile, uso file JSON: {e}")


def init_rtdb():
    """Prepara le credenziali per scrivere sul Realtime Database (REST)."""
    global rtdb_creds
    if not (RTDB_URL and GOOGLE_CREDENTIALS_JSON):
        return
    try:
        from google.oauth2 import service_account

        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        rtdb_creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/firebase.database",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        )
        logger.info("Realtime DB attivo")
    except Exception as e:
        logger.warning(f"Realtime DB non disponibile: {e}")


async def rtdb_push(path: str, data: dict) -> None:
    """Aggiunge un record (push) sul Realtime Database. Non blocca mai il bot in caso di errore."""
    if not (RTDB_URL and rtdb_creds):
        return
    try:
        import google.auth.transport.requests

        if not rtdb_creds.valid:
            rtdb_creds.refresh(google.auth.transport.requests.Request())
        url = f"{RTDB_URL}/{path}.json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, params={"access_token": rtdb_creds.token}, json=data)
    except Exception as e:
        logger.warning(f"rtdb_push error: {e}")

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


# Domini NON-ecommerce: i loro link vengono ignorati (utile nei gruppi)
NON_SHOP_DOMAINS = [
    "tiktok.com", "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.watch",
    "fb.com", "twitter.com", "x.com", "t.me", "telegram.me", "telegram.org", "reddit.com",
    "wikipedia.org", "google.", "bing.com", "spotify.com", "twitch.tv", "linkedin.com",
    "pinterest.", "whatsapp.com", "wa.me", "vimeo.com", "threads.net", "discord.",
    "tumblr.com", "snapchat.com", "soundcloud.com", "medium.com", "paypal.",
]


def is_shop_url(url: str) -> bool:
    """True se il link sembra di un e-commerce (non social/video/messaggistica)."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return False
        if any(b in host for b in NON_SHOP_DOMAINS):
            return False
        # Deve avere un percorso (non solo la home del sito)
        if parsed.path in ("", "/"):
            return is_amazon_url(url)
        return True
    except Exception:
        return False


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
    headers = {"User-Agent": USER_AGENTS[0], "Accept-Language": "it-IT,it;q=0.9"}
    # 1) Tentativo standard: segui i redirect e prendi l'URL finale
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            final = str(response.url)
            if final and not is_short_url(final):
                logger.info(f"Resolved {url} -> {final}")
                return final
    except Exception as e:
        logger.warning(f"resolve error: {e}")
    # 2) Fallback: segui i redirect manualmente leggendo gli header Location
    try:
        cur = url
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, headers=headers) as client:
            for _ in range(6):
                r = await client.get(cur)
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    cur = loc if loc.startswith("http") else str(httpx.URL(cur).join(loc))
                    if not is_short_url(cur):
                        break
                else:
                    break
        logger.info(f"Resolved (manual) {url} -> {cur}")
        return cur
    except Exception as e:
        logger.warning(f"resolve manual error: {e}")
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
    """Ritorna (affiliate_url, store_kind). store_kind in {amazon, merchant, aggregator, none}.
    Priorità: Amazon (tag nativo) → mappa per-negozio → Skimlinks → nessuno."""
    amazon = is_amazon_url(url)
    tag = get_affiliate_tag()
    via_skimlinks = route_via_skimlinks()

    # 1) Amazon col tag nativo (salvo routing forzato su Skimlinks)
    if amazon and not (via_skimlinks and DEEPLINK_TEMPLATE):
        normalized = normalize_amazon_url(url)
        return add_amazon_tag(normalized, tag), "amazon"

    # 2) Mappa per-negozio (deeplink dedicato del merchant, se configurato)
    tmpl = get_merchant_template(url)
    if tmpl:
        target = normalize_amazon_url(url) if amazon else url
        return tmpl.replace("{url}", quote_plus(target)), "merchant"

    # 3) Skimlinks (fallback per tutto il resto)
    if DEEPLINK_TEMPLATE:
        target = normalize_amazon_url(url) if amazon else url
        return DEEPLINK_TEMPLATE.replace("{url}", quote_plus(target)), "aggregator"

    # 4) Ripiego: Amazon col tag se non c'è aggregatore
    if amazon:
        normalized = normalize_amazon_url(url)
        return add_amazon_tag(normalized, tag), "amazon"
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
    "video": None,
    "source": None,
    "condition_status": None,
    "promotion": None,
    "coupon": None,
}

STORE_NAMES = {
    "amazon": "Amazon",
    "aliexpress": "AliExpress",
    "ebay": "eBay",
    "zalando": "Zalando",
    "mediaworld": "MediaWorld",
    "unieuro": "Unieuro",
    "temu": "Temu",
    "banggood": "Banggood",
    "shein": "Shein",
    "ikea": "IKEA",
    "decathlon": "Decathlon",
}


def store_name_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        for key, name in STORE_NAMES.items():
            if key in host:
                return name
        parts = host.split(".")
        return parts[0].capitalize() if parts and parts[0] else host
    except Exception:
        return None


def extract_page_video(soup) -> str:
    for prop in ["og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"]:
        v = meta_content(soup, prop)
        if v and v.startswith("http"):
            return v
    vid = soup.find("video")
    if vid:
        if vid.get("src", "").startswith("http"):
            return vid["src"]
        source = vid.find("source")
        if source and source.get("src", "").startswith("http"):
            return source["src"]
    return None


# --- Amazon Creators API / PA-API (OAuth2) ---------------------------------
_paapi_token = {"value": None, "exp": 0.0}


def _dig(d, *path):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        else:
            return None
    return d


async def _paapi_token_get() -> str:
    now = time.time()
    if _paapi_token["value"] and _paapi_token["exp"] - 60 > now:
        return _paapi_token["value"]
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                PAAPI_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "scope": PAAPI_SCOPE,
                    "client_id": PAAPI_CLIENT_ID,
                    "client_secret": PAAPI_CLIENT_SECRET,
                },
            )
            d = r.json()
            tok = d.get("access_token")
            if not tok:
                logger.error(f"PA-API token error {r.status_code}: {str(d)[:200]}")
                return None
            _paapi_token["value"] = tok
            _paapi_token["exp"] = now + int(d.get("expires_in", 3600))
            logger.info("PA-API token ottenuto")
            return tok
    except Exception as e:
        logger.error(f"PA-API token exception: {e}")
        return None


def _parse_paapi_item(it: dict) -> dict:
    info = dict(EMPTY_INFO)
    info["title"] = _dig(it, "itemInfo", "title", "displayValue") or _dig(it, "ItemInfo", "Title", "DisplayValue")
    info["image"] = (
        _dig(it, "images", "primary", "large", "url")
        or _dig(it, "images", "primary", "medium", "url")
        or _dig(it, "Images", "Primary", "Large", "URL")
    )
    listings = _dig(it, "offersV2", "listings") or _dig(it, "OffersV2", "Listings") or []
    if listings:
        l0 = listings[0]
        info["price"] = (
            _dig(l0, "price", "money", "displayAmount")
            or _dig(l0, "price", "displayString")
            or _dig(l0, "price", "displayAmount")
            or _dig(l0, "Price", "Money", "DisplayAmount")
        )
    rating = _dig(it, "customerReviews", "starRating", "value") or _dig(it, "customerReviews", "starRating")
    if isinstance(rating, (int, float)):
        rating = str(rating)
    info["rating"] = rating
    info["reviews"] = _dig(it, "customerReviews", "count")
    return info


async def paapi_get_item(asin: str) -> dict:
    if not (asin and PAAPI_CLIENT_ID and PAAPI_CLIENT_SECRET):
        return None
    token = await _paapi_token_get()
    if not token:
        return None
    body = {
        "itemIds": [asin],
        "itemIdType": "ASIN",
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
            "customerReviews.starRating",
            "customerReviews.count",
        ],
        "partnerTag": PAAPI_PARTNER_TAG,
        "partnerType": "Associate",
        "marketplace": PAAPI_MARKETPLACE,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-marketplace": PAAPI_MARKETPLACE,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(f"{PAAPI_HOST}/catalog/v1/getItems", headers=headers, json=body)
            data = r.json()
        if r.status_code != 200:
            logger.warning(f"PA-API getItems {r.status_code}: {str(data)[:250]}")
            return None
        items = (
            _dig(data, "itemsResult", "items")
            or _dig(data, "ItemsResult", "Items")
            or data.get("items")
        )
        if not items:
            logger.warning(f"PA-API nessun item: {str(data)[:300]}")
            return None
        parsed = _parse_paapi_item(items[0])
        logger.info(f"PA-API OK per {asin}: {parsed.get('title')}")
        return parsed
    except Exception as e:
        logger.error(f"PA-API getItems exception: {e}")
        return None


async def get_product_info(url: str, is_amazon: bool) -> dict:
    # Per Amazon prova prima la PA-API (dati affidabili), poi fallback scraping
    if is_amazon:
        asin = extract_asin_from_url(url)
        api_info = await paapi_get_item(asin) if asin else None
        if api_info and api_info.get("title"):
            for k in EMPTY_INFO:
                api_info.setdefault(k, None)
            api_info["source"] = store_name_from_url(url)
            api_info["condition_status"] = detect_seller_condition(url, BeautifulSoup("", "html.parser"))
            return api_info

    html = await fetch_html(url)
    if not html:
        info = dict(EMPTY_INFO)
        info["source"] = store_name_from_url(url)
        return info
    soup = BeautifulSoup(html, "html.parser")

    if is_amazon:
        info = dict(EMPTY_INFO)
        info["title"] = extract_amazon_title(soup)
        info["price"] = extract_amazon_price(soup)
        info["rating"], info["reviews"] = extract_amazon_rating(soup)
        info["image"] = extract_amazon_image(soup)
        info["video"] = extract_page_video(soup)
        info["source"] = store_name_from_url(url)
        info["condition_status"] = detect_seller_condition(url, soup)
        # Promozione/coupon disattivati: l'euristica generava falsi positivi
        # (es. "Promozione: Promozioni" preso da un menu). Riattivabili con selettori precisi.
        return info

    info = dict(EMPTY_INFO)
    info["title"] = meta_content(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    info["image"] = meta_content(soup, "og:image")
    info["video"] = extract_page_video(soup)
    info["source"] = store_name_from_url(url)
    info["price"] = meta_content(soup, "product:price:amount") or meta_content(soup, "og:price:amount")

    # Fallback dati da JSON-LD (schema.org Product)
    ld = extract_jsonld_product(soup)
    if ld:
        info["title"] = info["title"] or ld.get("title")
        info["image"] = info["image"] or ld.get("image")
        if not info["price"] and ld.get("price"):
            cur = ld.get("currency") or "€"
            info["price"] = f"{ld['price']}{cur if cur == '€' else ' ' + cur}"
    return info


def extract_jsonld_product(soup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = data["@graph"]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            t = c.get("@type")
            is_product = t == "Product" or (isinstance(t, list) and "Product" in t)
            if not is_product:
                continue
            offers = c.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            img = c.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")
            return {
                "title": c.get("name"),
                "image": img,
                "price": offers.get("price") if isinstance(offers, dict) else None,
                "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
            }
    return None


def _iso8601_to_seconds(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    h, mi, se = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
    return h * 3600 + mi * 60 + se


IT_STOPWORDS = {
    "per", "con", "di", "da", "del", "della", "dei", "delle", "degli", "il", "lo", "la",
    "le", "gli", "un", "una", "uno", "in", "su", "e", "a", "o", "the", "and", "of",
    "pezzi", "set", "confezione", "colore", "nero", "bianco", "misura", "taglia",
}


def _keywords(title: str) -> list:
    words = re.findall(r"[a-zA-Z0-9àèéìòùç]+", (title or "").lower())
    return [w for w in words if len(w) >= 4 and not w.isdigit() and w not in IT_STOPWORDS]


def _video_is_relevant(product_title: str, video_title: str) -> bool:
    pk = _keywords(product_title)
    if not pk:
        return False
    vt = (video_title or "").lower()
    brand = pk[0]
    if brand in vt:  # il brand del prodotto compare nel titolo del video
        return True
    overlap = sum(1 for w in set(pk[1:6]) if w in vt)  # parole distintive condivise
    return overlap >= 2


def _clean_query(title: str) -> str:
    words = re.split(r"[\s,;:-]+", title or "")
    return " ".join(w for w in words if len(w) > 1)[:80]


async def find_youtube_video(title: str) -> str:
    """Trova un video YouTube CORTO (<VIDEO_MAX_SECONDS) e PERTINENTE via API ufficiale.
    Se nessun risultato è abbastanza pertinente, ritorna None (meglio niente che un video sbagliato)."""
    if not (title and YOUTUBE_API_KEY):
        return None
    query = _clean_query(title)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            s = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "maxResults": 8,
                    "videoDuration": "short",
                    "relevanceLanguage": "it",
                },
            )
            sd = s.json()
            titles = {}
            for it in sd.get("items", []):
                vid = it.get("id", {}).get("videoId")
                if vid:
                    titles[vid] = it.get("snippet", {}).get("title", "")
            if not titles:
                return None
            v = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"key": YOUTUBE_API_KEY, "part": "contentDetails", "id": ",".join(titles.keys())},
            )
            vd = v.json()
            for it in vd.get("items", []):
                dur = _iso8601_to_seconds(it.get("contentDetails", {}).get("duration"))
                if dur and dur <= VIDEO_MAX_SECONDS and _video_is_relevant(title, titles.get(it["id"], "")):
                    return f"https://www.youtube.com/watch?v={it['id']}"
    except Exception as e:
        logger.warning(f"youtube API error: {e}")
    return None


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
        # Cerca il prezzo PRINCIPALE nei blocchi ufficiali (evita prezzi di accessori/varianti)
        for div_id in [
            "corePriceDisplay_desktop_feature_div",
            "corePrice_feature_div",
            "apex_desktop",
            "buybox",
        ]:
            div = soup.find(id=div_id)
            if div:
                off = div.find("span", {"class": "a-offscreen"})
                if off and off.get_text(strip=True):
                    return off.get_text(strip=True)
        for pid in ["priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"]:
            el = soup.find(id=pid)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        # Ultima spiaggia: primo prezzo "a-offscreen" della pagina
        off = soup.find("span", {"class": "a-offscreen"})
        if off and off.get_text(strip=True):
            return off.get_text(strip=True)
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


async def _bitly_shorten(url: str) -> str:
    """Accorcia con Bitly provando i token in ordine (rotazione su quota esaurita)."""
    for tok in get_bitly_tokens():
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "https://api-ssl.bitly.com/v4/shorten",
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                    json={"long_url": url},
                )
                if r.status_code in (200, 201):
                    link = r.json().get("link")
                    if link:
                        return link
                else:
                    logger.warning(f"Bitly token KO {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.warning(f"Bitly error: {e}")
    return None


async def shorten_url(url: str, use_bitly: bool = True) -> str:
    """Accorcia un link. Bitly (solo se use_bitly) -> YOURLS -> is.gd. L'affiliazione resta nel link."""
    if use_bitly and get_bitly_tokens():
        b = await _bitly_shorten(url)
        if b:
            return b
    if YOURLS_URL and YOURLS_SIGNATURE:
        short = await shorten_with_yourls(url)
        if short and short != url:
            return short
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://is.gd/create.php", params={"format": "simple", "url": url})
            if r.status_code == 200 and r.text.strip().startswith("http"):
                return r.text.strip()
            logger.warning(f"is.gd response: {r.text[:120]}")
    except Exception as e:
        logger.warning(f"is.gd error: {e}")
    return url


# ----------------------------------------------------------------------------
# AI copywriting (Claude)
# ----------------------------------------------------------------------------
AI_SYSTEM_PROMPT = (
    "Sei un copywriter per un canale Telegram di offerte e affiliazione. "
    "Scrivi un post breve (massimo 500 caratteri), accattivante, in italiano, "
    "con qualche emoji, che invogli all'acquisto e crei urgenza in modo onesto. "
    "Inserisci il link così com'è (verrà reso cliccabile da Telegram). "
    "Rispondi SOLO con il testo del post, senza virgolette né spiegazioni."
)


REVIEW_SYSTEM_PROMPT = (
    "Sei un esperto di recensioni prodotti. Scrivi una mini-recensione onesta e utile, "
    "MOLTO breve: massimo 3 righe e non oltre 180 caratteri totali, in italiano. "
    "Vai dritto al punto: a chi è adatto e il vantaggio principale. "
    "Non inventare specifiche tecniche. Niente emoji. Rispondi SOLO con la recensione, senza titoli né virgolette."
)


async def generate_ai_copy(info: dict, price_line: str, link: str) -> str:
    user = (
        f"Prodotto: {info.get('title') or 'Prodotto in offerta'}\n"
        f"Store: {info.get('source') or 'n/d'}\n"
        f"{price_line}\n"
        f"Valutazione: {info.get('rating') or 'n/d'}\n"
        f"Link: {link}"
    )
    return await _ai_complete(AI_SYSTEM_PROMPT, user, 400)


async def generate_ai_review(info: dict, price_line: str) -> str:
    user = (
        f"Prodotto: {info.get('title') or 'Prodotto'}\n"
        f"Store: {info.get('source') or 'n/d'}\n"
        f"{price_line}\n"
        f"Valutazione: {info.get('rating') or 'n/d'}"
    )
    return await _ai_complete(REVIEW_SYSTEM_PROMPT, user, 130)


async def _ai_complete(system: str, user: str, max_tokens: int = 400) -> str:
    if GROQ_API_KEY:
        return await _groq_chat(system, user, max_tokens)
    if GEMINI_API_KEY:
        return await _gemini_chat(system, user, max_tokens)
    if ai_client:
        return await _claude_chat(system, user, max_tokens)
    return None


async def _groq_chat(system: str, user: str, max_tokens: int = 400) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": GROQ_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.9,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            data = r.json()
            if "choices" not in data:
                logger.error(f"Groq response: {str(data)[:300]}")
                return None
            return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None


async def _gemini_chat(system: str, user: str, max_tokens: int = 400) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.9},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload)
            data = r.json()
            if "candidates" not in data:
                logger.error(f"Gemini response: {str(data)[:300]}")
                return None
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            return text.strip() or None
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None


async def _claude_chat(system: str, user: str, max_tokens: int = 400) -> str:
    try:
        resp = await ai_client.messages.create(
            model=AI_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return text.strip() if text else None
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return None


# ----------------------------------------------------------------------------
# Messaggi
# ----------------------------------------------------------------------------
def one_line(text: str, maxlen: int = 70) -> str:
    if not text:
        return text
    t = " ".join(str(text).split())
    if len(t) <= maxlen:
        return t
    return t[:maxlen].rsplit(" ", 1)[0] + "…"


def max_lines(text: str, n: int = 3, line_len: int = 90) -> str:
    if not text:
        return text
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    lines = [(ln[:line_len].rsplit(" ", 1)[0] + "…") if len(ln) > line_len else ln for ln in lines]
    return "\n".join(lines[:n])


def buy_button(short_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Acquista ora", url=short_url)]])


# ----------------------------------------------------------------------------
# Immagine personalizzata (card) con Pillow
# ----------------------------------------------------------------------------
STORE_COLORS = {
    "Amazon": (255, 153, 0),
    "AliExpress": (229, 57, 53),
    "eBay": (0, 100, 210),
    "Zalando": (255, 102, 0),
    "Temu": (255, 102, 0),
}


async def fetch_bytes(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENTS[0]})
            if r.status_code == 200:
                return r.content
    except Exception as e:
        logger.warning(f"fetch_bytes error: {e}")
    return None


def _font(size: int):
    from PIL import ImageFont

    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _gradient(w: int, h: int):
    from PIL import Image, ImageDraw

    top, bot = (38, 40, 54), (12, 12, 18)
    base = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(base)
    for y in range(h):
        r = int(top[0] + (bot[0] - top[0]) * y / h)
        g = int(top[1] + (bot[1] - top[1]) * y / h)
        b = int(top[2] + (bot[2] - top[2]) * y / h)
        d.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def _compose_card(img_bytes: bytes, store: str, brand_text: str, bg_bytes: bytes = None) -> bytes:
    from PIL import Image, ImageDraw, ImageOps

    W = H = 1080
    # Sfondo
    if bg_bytes:
        try:
            bg = ImageOps.fit(Image.open(io.BytesIO(bg_bytes)).convert("RGB"), (W, H))
            bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.45)
        except Exception:
            bg = _gradient(W, H)
    else:
        bg = _gradient(W, H)

    # Prodotto su card bianca arrotondata
    prod = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    prod.thumbnail((760, 620))
    pad = 30
    cw, ch = prod.width + 2 * pad, prod.height + 2 * pad
    card = Image.new("RGBA", (cw, ch), (255, 255, 255, 255))
    card.paste(prod, (pad, pad), prod)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw, ch], radius=40, fill=255)
    cx, cy = (W - cw) // 2, 150
    bg.paste(card, (cx, cy), mask)

    draw = ImageDraw.Draw(bg)
    # Scritta brand sotto al prodotto
    bt = brand_text or ""
    if bt:
        f = _font(72)
        tw = draw.textlength(bt, font=f)
        ty = cy + ch + 55
        draw.text(((W - tw) / 2 + 3, ty + 3), bt, font=f, fill=(0, 0, 0))
        draw.text(((W - tw) / 2, ty), bt, font=f, fill=(255, 255, 255))

    # Badge store in alto a sinistra
    if store:
        badge = store.upper()
        fb = _font(40)
        bw = draw.textlength(badge, font=fb)
        p = 22
        col = STORE_COLORS.get(store, (33, 150, 243))
        draw.rounded_rectangle([40, 40, 40 + bw + 2 * p, 106], radius=20, fill=col)
        draw.text((40 + p, 52), badge, font=fb, fill=(255, 255, 255))

    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


async def generate_card_image(image_url: str, store: str, brand_text: str) -> bytes:
    if not image_url:
        return None
    img = await fetch_bytes(image_url)
    if not img:
        return None
    bg = None
    bgurl = get_bg_image_url()
    if bgurl:
        bg = await fetch_bytes(bgurl)
    try:
        return await asyncio.to_thread(_compose_card, img, store, brand_text, bg)
    except Exception as e:
        logger.warning(f"card image error: {e}")
        return None


async def get_post_photo(info: dict):
    """Ritorna i byte della card personalizzata se attiva, altrimenti l'URL immagine, altrimenti None."""
    if card_enabled() and info.get("image"):
        card = await generate_card_image(info.get("image"), info.get("source"), get_brand_text())
        if card:
            return card
    return info.get("image")


def build_product_message(info: dict, short_url: str = None, user_name: str = None,
                          review: str = None, price_line: str = None) -> str:
    title = one_line(info.get("title") or "Prodotto")
    rating = info.get("rating") or ""
    condition = info.get("condition_status") or ""
    source = info.get("source") or ""

    if not price_line:
        price = info.get("price") or ""
        clean_price = re.sub(r"€.*", "€", price).strip() if price else ""
        price_line = clean_price

    rating_stars = ""
    if rating:
        try:
            rating_stars = "⭐" * int(float(rating.replace(",", ".")))
        except Exception:
            rating_stars = f"⭐ {rating}/5"

    msg = ""
    if user_name:
        msg += f"👤 {user_name} ha condiviso questo articolo\n\n"
    msg += f"📌 <b>{title}</b>\n"
    if source:
        msg += f"🏪 {source}"
        # mostra il venditore solo se è "Usato" (info utile); altrimenti lo ometto
        if condition and "Usato" in condition:
            msg += f" · {condition}"
        msg += "\n"
    if price_line:
        msg += f"💰 <b>{price_line}</b>\n"
    if rating_stars:
        msg += f"{rating_stars}\n"
    if review:
        rev = max_lines(review, 3, line_len=70)
        if len(rev) > 200:
            rev = rev[:200].rsplit(" ", 1)[0] + "…"
        msg += f"\n📝 {rev}\n"
    if short_url:
        msg += f"\n🛒 {short_url}"
    return msg.strip()


# ----------------------------------------------------------------------------
# Watchlist (persistenza su file JSON)
# ----------------------------------------------------------------------------
def load_watchlist() -> list:
    if USE_FIRESTORE:
        try:
            doc = firestore_db.collection("bot").document("watchlist").get()
            return doc.to_dict().get("items", []) if doc.exists else []
        except Exception as e:
            logger.error(f"Firestore load error: {e}")
            return []
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(wl: list) -> None:
    if USE_FIRESTORE:
        try:
            firestore_db.collection("bot").document("watchlist").set({"items": wl})
            return
        except Exception as e:
            logger.error(f"Firestore save error: {e}")
            return
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"save_watchlist error: {e}")


# ----------------------------------------------------------------------------
# Impostazioni bot (admin, canale) — Firestore o file
# ----------------------------------------------------------------------------
_settings_cache = {"data": None, "ts": 0.0}
SETTINGS_TTL = 30  # secondi


def load_settings() -> dict:
    now = time.time()
    if _settings_cache["data"] is not None and (now - _settings_cache["ts"]) < SETTINGS_TTL:
        return _settings_cache["data"]
    data = {}
    if USE_FIRESTORE:
        try:
            doc = firestore_db.collection("bot").document("settings").get()
            data = doc.to_dict() if doc.exists else {}
        except Exception as e:
            logger.error(f"settings load error: {e}")
            data = _settings_cache["data"] or {}
    else:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    _settings_cache["data"] = data
    _settings_cache["ts"] = now
    return data


def save_settings(s: dict) -> None:
    _settings_cache["data"] = s
    _settings_cache["ts"] = time.time()
    if USE_FIRESTORE:
        try:
            firestore_db.collection("bot").document("settings").set(s)
            return
        except Exception as e:
            logger.error(f"settings save error: {e}")
            return
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"settings save error: {e}")


# ----------------------------------------------------------------------------
# Cronologia prezzi (minimo storico) + anti-duplicato
# ----------------------------------------------------------------------------
def _product_key(url: str) -> str:
    asin = extract_asin_from_url(url or "")
    if asin:
        return asin
    return re.sub(r"\W+", "", (url or ""))[:80] or "x"


def _load_price_rec(key: str) -> dict:
    if USE_FIRESTORE:
        try:
            doc = firestore_db.collection("prices").document(key).get()
            return doc.to_dict() if doc.exists else {}
        except Exception as e:
            logger.warning(f"price load error: {e}")
            return {}
    try:
        with open(os.path.join(DATA_DIR, "prices.json"), "r", encoding="utf-8") as f:
            return json.load(f).get(key, {})
    except Exception:
        return {}


def _save_price_rec(key: str, rec: dict) -> None:
    if USE_FIRESTORE:
        try:
            firestore_db.collection("prices").document(key).set(rec)
            return
        except Exception as e:
            logger.warning(f"price save error: {e}")
            return
    path = os.path.join(DATA_DIR, "prices.json")
    try:
        alld = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                alld = json.load(f)
        except Exception:
            alld = {}
        alld[key] = rec
        with open(path, "w", encoding="utf-8") as f:
            json.dump(alld, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"price save error: {e}")


def record_observation(url: str, price: float) -> dict:
    """Registra il prezzo osservato e ritorna info sul minimo storico.
    Ritorna {min, prev_min, is_new_min, seen} (price può essere None)."""
    key = _product_key(url)
    rec = _load_price_rec(key)
    prev_min = rec.get("min")
    seen = rec.get("seen", 0)
    is_new_min = False
    new_min = prev_min
    if price is not None:
        if prev_min is None or price < prev_min:
            new_min = price
            is_new_min = seen > 0  # "minimo storico" solo se l'avevamo già visto prima
        rec["min"] = new_min
        rec["last"] = price
        rec["seen"] = seen + 1
        rec["ts"] = int(time.time())
        _save_price_rec(key, rec)
    return {"min": new_min, "prev_min": prev_min, "is_new_min": is_new_min, "seen": seen}


def build_price_line(price: float, hist: dict, old_price: float = None) -> str:
    """Riga prezzo con minimo storico / sconto."""
    if price is None:
        return ""
    line = f"{price:.2f}€"
    if old_price and old_price > price:
        drop = round((1 - price / old_price) * 100)
        line += f"  (era {old_price:.2f}€, -{drop}%)"
    if hist and hist.get("is_new_min"):
        line += "  🔥 minimo storico!"
    elif hist and hist.get("min") and price > hist["min"]:
        line += f"  (min: {hist['min']:.2f}€)"
    return line


def already_posted_recently(url: str, days: int) -> bool:
    rec = _load_price_rec(_product_key(url))
    lp = rec.get("last_posted")
    return lp is not None and (time.time() - lp) < days * 86400


def mark_posted(url: str) -> None:
    key = _product_key(url)
    rec = _load_price_rec(key)
    rec["last_posted"] = int(time.time())
    _save_price_rec(key, rec)


def is_admin(user_id: int) -> bool:
    return user_id in load_settings().get("admins", [])


def admins_exist() -> bool:
    return bool(load_settings().get("admins"))


def get_post_channel() -> str:
    return load_settings().get("channel") or CHANNEL_ID


def get_bitly_tokens() -> list:
    """Token Bitly: da env (BITLY_TOKENS) + quelli aggiunti dal bot (settings)."""
    toks = list(BITLY_TOKENS)
    for t in load_settings().get("bitly_tokens", []):
        if t not in toks:
            toks.append(t)
    return toks


def get_affiliate_tag() -> str:
    return load_settings().get("amazon_tag") or AFFILIATE_TAG


def route_via_skimlinks() -> bool:
    s = load_settings()
    if "route_all_via_skimlinks" in s:
        return bool(s["route_all_via_skimlinks"])
    return ROUTE_ALL_VIA_SKIMLINKS


def video_enabled() -> bool:
    return load_settings().get("video_enabled", True)


def card_enabled() -> bool:
    return load_settings().get("card_enabled", True)


def get_brand_text() -> str:
    return load_settings().get("brand_text") or BRAND_TEXT


def get_bg_image_url() -> str:
    return load_settings().get("bg_image_url", "")


def get_merchant_template(url: str) -> str:
    """Deeplink dedicato per il dominio del merchant, se configurato dall'admin."""
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return None
    if not host:
        return None
    for domain, tmpl in load_settings().get("merchants", {}).items():
        if domain.lower() in host:
            return tmpl
    return None


# ----------------------------------------------------------------------------
# Pubblicazione offerta (canale o utente)
# ----------------------------------------------------------------------------
async def publish_deal(bot, entry: dict, info: dict, current_price: float, old_price: float = None):
    affiliate_url, kind = build_affiliate_link(entry["url"])
    short_url = await shorten_url(affiliate_url, use_bitly=(kind == "amazon"))

    hist = record_observation(entry["url"], current_price)
    price_line = build_price_line(current_price, hist, old_price) or "n/d"

    await rtdb_push(
        "deals",
        {
            "title": info.get("title") or "Prodotto",
            "price": current_price,
            "old_price": old_price,
            "url": short_url,
            "store": "amazon" if entry.get("is_amazon") else "altro",
            "ts": int(time.time()),
        },
    )

    destination = get_post_channel() or entry.get("chat_id")
    kb = buy_button(short_url)
    photo = await get_post_photo(info)

    ai_text = await generate_ai_copy(info, f"Prezzo: {price_line}", short_url)
    if ai_text:
        body = ai_text.replace(short_url, "").strip()
        if hist.get("is_new_min"):
            body = "🔥 <b>MINIMO STORICO</b>\n" + body
        if photo:
            try:
                await bot.send_photo(chat_id=destination, photo=photo, caption=body, reply_markup=kb)
                mark_posted(entry["url"])
                return
            except Exception as e:
                logger.warning(f"send_photo error: {e}")
        await bot.send_message(chat_id=destination, text=body, reply_markup=kb, disable_web_page_preview=True)
        mark_posted(entry["url"])
        return

    # Fallback senza AI
    message = build_product_message(info, user_name=None, price_line=price_line)
    if photo:
        try:
            await bot.send_photo(chat_id=destination, photo=photo, caption=message,
                                 parse_mode=ParseMode.HTML, reply_markup=kb)
            mark_posted(entry["url"])
            return
        except Exception as e:
            logger.warning(f"send_photo error: {e}")
    await bot.send_message(chat_id=destination, text=message, parse_mode=ParseMode.HTML, reply_markup=kb)
    mark_posted(entry["url"])


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

            if alert and already_posted_recently(entry["url"], ANTIDUP_DAYS):
                logger.info(f"Salto (anti-duplicato): {entry['url']}")
                alert = False

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
# Job: post programmati (opzionale, SCHEDULED_POST_HOURS > 0)
# ----------------------------------------------------------------------------
async def scheduled_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    wl = load_watchlist()
    if not wl or not get_post_channel():
        return
    # scegli il primo prodotto non pubblicato di recente
    for entry in wl:
        if already_posted_recently(entry["url"], ANTIDUP_DAYS):
            continue
        try:
            info = await get_product_info(entry["url"], is_amazon=entry.get("is_amazon", False))
            price = parse_price_to_float(info.get("price"))
            await publish_deal(context.bot, entry, info, price)
            logger.info(f"Post programmato pubblicato: {entry['url']}")
            return
        except Exception as e:
            logger.error(f"scheduled_post error: {e}")
    logger.info("Post programmato: nessun prodotto idoneo (tutti pubblicati di recente)")


# ----------------------------------------------------------------------------
# Handlers comandi
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "👋 Ciao! Sono il tuo Bot Offerte & Affiliazione.\n\n"
        "🔗 Inviami il link di un prodotto (Amazon o altri store) e ti do il link affiliato.\n\n"
        "🤖 Funzioni di automazione (admin):\n"
        "• /watch <link> [prezzo] – monitora un prodotto e avvisa al calo\n"
        "• /list – mostra i prodotti monitorati\n"
        "• /unwatch <numero> – rimuovi dalla lista\n"
        "• /deal <link> – pubblica subito un'offerta\n\n"
        "⚙️ Configurazione (admin):\n"
        "• /admin <password> – diventa admin\n"
        "• /setchannel <@canale|id> – dove pubblicare le offerte\n"
        "• /config – stato configurazione\n"
        "• /id – mostra il tuo id\n"
    )
    if is_admin(update.effective_user.id):
        await update.message.reply_text(welcome, reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text(welcome)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


BTN_CONFIG = "⚙️ Config"
BTN_PRODUCTS = "📋 Prodotti"
BTN_CHANNEL = "📢 Imposta canale"
BTN_ADD = "➕ Aggiungi prodotto"
BTN_DEAL = "🔥 Pubblica offerta"
BTN_TOKENS = "🔑 Token Bitly"
BTN_TAG = "🏷️ Tag Amazon"
BTN_MERCHANTS = "🗺️ Negozi"
BTN_CARD = "🎨 Grafica"
BTN_HELP = "❓ Aiuto"

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_CONFIG, BTN_PRODUCTS],
        [BTN_CHANNEL, BTN_ADD],
        [BTN_DEAL, BTN_TOKENS],
        [BTN_TAG, BTN_MERCHANTS],
        [BTN_CARD, BTN_HELP],
    ],
    resize_keyboard=True,
)


async def keyboard_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == BTN_CONFIG:
        await config_cmd(update, context)
    elif text == BTN_PRODUCTS:
        await list_cmd(update, context)
    elif text == BTN_CHANNEL:
        await update.message.reply_text(
            "📢 Manda:  /setchannel @iltuocanale\n"
            "(prima aggiungi il bot come AMMINISTRATORE del canale). Per togliere: /setchannel off"
        )
    elif text == BTN_ADD:
        await update.message.reply_text(
            "➕ Manda:  /watch <link prodotto> [prezzo]\n"
            "Es:  /watch https://www.amazon.it/dp/XXXX 19.90"
        )
    elif text == BTN_DEAL:
        await update.message.reply_text("🔥 Manda:  /deal <link prodotto>")
    elif text == BTN_TOKENS:
        await tokens_cmd(update, context)
        await update.message.reply_text("➕ Per aggiungerne: /addtoken <token1> <token2> ...")
    elif text == BTN_TAG:
        await settag_cmd(update, context)
        await update.message.reply_text("Routing: /setrouting native|skimlinks")
    elif text == BTN_MERCHANTS:
        await merchants_cmd(update, context)
        await update.message.reply_text("➕ Aggiungi: /setmerchant <dominio> <template_con_{url}>")
    elif text == BTN_CARD:
        await update.message.reply_text(
            "🎨 <b>Grafica card</b>\n"
            f"Card: {'attiva' if card_enabled() else 'disattiva'}\n"
            f"Scritta: {get_brand_text()}\n"
            f"Sfondo: {get_bg_image_url() or 'automatico'}\n\n"
            "/setcard on|off · /setbrand <testo> · /setbg <url|off>",
            parse_mode=ParseMode.HTML,
        )
    elif text == BTN_HELP:
        await start(update, context)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"👤 Tuo user id: <code>{update.effective_user.id}</code>\n"
        f"💬 Chat id: <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not SETUP_PASSWORD:
        await update.message.reply_text("⚠️ Setup non configurato (manca SETUP_PASSWORD).")
        return
    if not context.args or context.args[0] != SETUP_PASSWORD:
        await update.message.reply_text("❌ Password errata. Uso: /admin <password>")
        return
    s = load_settings()
    admins = s.get("admins", [])
    uid = update.effective_user.id
    if uid not in admins:
        admins.append(uid)
        s["admins"] = admins
        save_settings(s)
    await update.message.reply_text(
        "✅ Ora sei admin! Usa i tasti qui sotto 👇",
        reply_markup=ADMIN_KEYBOARD,
    )


def _deny_if_not_admin(update: Update) -> bool:
    """True se l'utente NON è autorizzato (admin esistono e lui non lo è)."""
    return admins_exist() and not is_admin(update.effective_user.id)


async def setchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: /setchannel <@canale o id>\n"
            "Prima aggiungi il bot come AMMINISTRATORE del canale.\n"
            "Per togliere: /setchannel off"
        )
        return
    value = context.args[0]
    s = load_settings()
    if value.lower() in ("off", "none", "-"):
        s["channel"] = ""
        save_settings(s)
        await update.message.reply_text("✅ Canale rimosso. Le offerte torneranno in chat.")
        return
    s["channel"] = value
    save_settings(s)
    # Test invio
    try:
        await context.bot.send_message(chat_id=value, text="✅ Canale collegato: pubblicherò qui le offerte.")
        await update.message.reply_text(f"✅ Canale impostato: {value} (messaggio di prova inviato).")
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Canale salvato ({value}) ma NON riesco a scrivere lì: {e}\n"
            "Assicurati che il bot sia AMMINISTRATORE del canale."
        )


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    s = load_settings()
    wl = load_watchlist()
    await update.message.reply_text(
        "<b>⚙️ Configurazione</b>\n"
        f"Canale: {s.get('channel') or CHANNEL_ID or '(nessuno → posta in chat)'}\n"
        f"Admin: {len(s.get('admins', []))}\n"
        f"Prodotti monitorati: {len(wl)}\n"
        f"Tag Amazon: {get_affiliate_tag() or '(nessuno)'}\n"
        f"Routing Amazon: {'Skimlinks' if route_via_skimlinks() else 'tag nativo'}\n"
        f"Negozi mappati: {len(s.get('merchants', {}))}\n"
        f"Card grafica: {'attiva' if card_enabled() else 'disattiva'} (scritta: {get_brand_text()})\n"
        f"AI: {_ai_status()}\n"
        f"YouTube API: {'sì' if YOUTUBE_API_KEY else 'no'}\n"
        f"Video: {'attivo' if video_enabled() else 'disattivo'}\n"
        f"Token Bitly: {len(get_bitly_tokens())}",
        parse_mode=ParseMode.HTML,
    )


async def settag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        await update.message.reply_text(
            f"Tag Amazon attuale: <b>{get_affiliate_tag() or '(nessuno)'}</b>\n"
            "Per cambiarlo: /settag <tuotag-21>",
            parse_mode=ParseMode.HTML,
        )
        return
    tag = context.args[0].strip()
    s = load_settings()
    s["amazon_tag"] = tag
    save_settings(s)
    note = ""
    if route_via_skimlinks():
        note = "\n\n⚠️ Ora i link Amazon passano da Skimlinks, quindi questo tag NON viene usato. Per usarlo: /setrouting native"
    await update.message.reply_text(f"✅ Tag Amazon impostato: <b>{tag}</b>{note}", parse_mode=ParseMode.HTML)


async def setrouting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args or context.args[0].lower() not in ("native", "skimlinks"):
        cur = "Skimlinks" if route_via_skimlinks() else "tag nativo"
        await update.message.reply_text(
            f"Routing Amazon attuale: <b>{cur}</b>\n"
            "Per cambiarlo:\n"
            "• /setrouting native  → usa il TUO tag (commissione piena)\n"
            "• /setrouting skimlinks → passa tutto da Skimlinks",
            parse_mode=ParseMode.HTML,
        )
        return
    via = context.args[0].lower() == "skimlinks"
    s = load_settings()
    s["route_all_via_skimlinks"] = via
    save_settings(s)
    await update.message.reply_text(
        f"✅ Routing Amazon: <b>{'Skimlinks' if via else 'tag nativo (' + (get_affiliate_tag() or 'nessun tag') + ')'}</b>",
        parse_mode=ParseMode.HTML,
    )


async def setmerchant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if len(context.args) < 2 or "{url}" not in context.args[1]:
        await update.message.reply_text(
            "Uso: /setmerchant <dominio> <template_con_{url}>\n\n"
            "Esempi:\n"
            "/setmerchant zooplus.it https://www.awin1.com/cread.php?awinmid=XXXX&awinaffid=YYYY&ued={url}\n"
            "/setmerchant vodafone.it https://clk.tradedoubler.com/click?p=XXXX&a=YYYY&url={url}\n\n"
            "Se un dominio non è in mappa, si usa Skimlinks. (Amazon resta col tuo tag.)"
        )
        return
    domain = context.args[0].lower().replace("www.", "").strip()
    tmpl = context.args[1].strip()
    s = load_settings()
    merchants = s.get("merchants", {})
    merchants[domain] = tmpl
    s["merchants"] = merchants
    save_settings(s)
    await update.message.reply_text(f"✅ Negozio mappato: <b>{domain}</b>\n→ {tmpl}", parse_mode=ParseMode.HTML)


async def merchants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    merchants = load_settings().get("merchants", {})
    if not merchants:
        await update.message.reply_text(
            "Nessun negozio mappato. Tutti i link (tranne Amazon) passano da Skimlinks.\n"
            "Aggiungi con /setmerchant <dominio> <template_con_{url}>"
        )
        return
    lines = ["<b>🗺️ Negozi mappati:</b>"]
    for d, t in merchants.items():
        lines.append(f"• <b>{d}</b> → {t[:60]}…")
    lines.append("\nRimuovi con /delmerchant <dominio>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def delmerchant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /delmerchant <dominio>")
        return
    domain = context.args[0].lower().replace("www.", "").strip()
    s = load_settings()
    merchants = s.get("merchants", {})
    if domain in merchants:
        del merchants[domain]
        s["merchants"] = merchants
        save_settings(s)
        await update.message.reply_text(f"🗑️ Rimosso: {domain}")
    else:
        await update.message.reply_text("Dominio non trovato. Vedi /merchants")


async def setvideo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        cur = "attivo" if video_enabled() else "disattivo"
        await update.message.reply_text(f"Video: <b>{cur}</b>\nUso: /setvideo on|off", parse_mode=ParseMode.HTML)
        return
    on = context.args[0].lower() == "on"
    s = load_settings()
    s["video_enabled"] = on
    save_settings(s)
    await update.message.reply_text(f"✅ Video {'attivati' if on else 'disattivati'}.")


async def setbrand_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        await update.message.reply_text(
            f"Scritta attuale sulla card: <b>{get_brand_text()}</b>\nPer cambiarla: /setbrand <testo>",
            parse_mode=ParseMode.HTML,
        )
        return
    text = " ".join(context.args).strip()[:40]
    s = load_settings()
    s["brand_text"] = text
    save_settings(s)
    await update.message.reply_text(f"✅ Scritta card impostata: <b>{text}</b>", parse_mode=ParseMode.HTML)


async def setcard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        cur = "attiva" if card_enabled() else "disattiva"
        await update.message.reply_text(f"Card personalizzata: <b>{cur}</b>\nUso: /setcard on|off", parse_mode=ParseMode.HTML)
        return
    on = context.args[0].lower() == "on"
    s = load_settings()
    s["card_enabled"] = on
    save_settings(s)
    await update.message.reply_text(f"✅ Card personalizzata {'attivata' if on else 'disattivata'}.")


async def setbg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        cur = get_bg_image_url() or "(sfondo generato automaticamente)"
        await update.message.reply_text(
            f"Sfondo card: {cur}\n"
            "Per impostarlo: /setbg <url_immagine>\nPer togliere: /setbg off"
        )
        return
    val = context.args[0].strip()
    s = load_settings()
    if val.lower() in ("off", "none", "-"):
        s["bg_image_url"] = ""
        save_settings(s)
        await update.message.reply_text("✅ Sfondo personalizzato rimosso (uso lo sfondo generato).")
        return
    s["bg_image_url"] = val
    save_settings(s)
    await update.message.reply_text("✅ Sfondo card impostato. Manda un prodotto per vedere l'anteprima.")


async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: /addtoken <token_bitly>\n"
            "Puoi mandarne anche più di uno separati da spazio o virgola."
        )
        return
    raw = " ".join(context.args).replace(",", " ")
    new_tokens = [t.strip() for t in raw.split() if t.strip()]
    s = load_settings()
    tokens = s.get("bitly_tokens", [])
    added, invalid, dup = 0, 0, 0
    for t in new_tokens:
        if t in tokens or t in BITLY_TOKENS:
            dup += 1
            continue
        # valida il token su Bitly
        ok = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://api-ssl.bitly.com/v4/user", headers={"Authorization": f"Bearer {t}"})
                ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            tokens.append(t)
            added += 1
        else:
            invalid += 1
    s["bitly_tokens"] = tokens
    save_settings(s)
    await update.message.reply_text(
        f"✅ Token aggiunti: {added}\n"
        f"⚠️ Non validi: {invalid} · Duplicati: {dup}\n"
        f"🔑 Totale token attivi: {len(get_bitly_tokens())}"
    )


async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    toks = get_bitly_tokens()
    if not toks:
        await update.message.reply_text("Nessun token Bitly. Aggiungili con /addtoken <token> (uso is.gd).")
        return
    masked = "\n".join(f"• …{t[-6:]}" for t in toks)
    await update.message.reply_text(
        f"🔑 <b>Token Bitly attivi: {len(toks)}</b>\n{masked}\n\nPer azzerare: /cleartokens",
        parse_mode=ParseMode.HTML,
    )


async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    s = load_settings()
    s["bitly_tokens"] = []
    save_settings(s)
    await update.message.reply_text("🗑️ Token Bitly (aggiunti dal bot) rimossi.")


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
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
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
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

    # Nei gruppi: ignora link non-ecommerce (tiktok, youtube, social, ecc.)
    if not is_shop_url(original_url):
        logger.info(f"Link ignorato (non e-commerce): {original_url}")
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

        await status_msg.edit_text("📝 Scrivo la recensione...")
        price_f = parse_price_to_float(info.get("price"))
        review = await generate_ai_review(info, f"Prezzo: {info.get('price') or 'n/d'}")

        # Minimo storico
        hist = record_observation(url, price_f)
        price_line = build_price_line(price_f, hist) if price_f is not None else (info.get("price") or "")

        # Video: dal sito, altrimenti cercato su YouTube (se attivo e pertinente)
        video_url = info.get("video") if video_enabled() else None
        youtube_url = None
        if video_enabled() and not video_url and info.get("title"):
            await status_msg.edit_text("🎥 Cerco un video...")
            youtube_url = await find_youtube_video(info["title"])

        await status_msg.edit_text("🔗 Accorciando...")
        short_url = await shorten_url(affiliate_url, use_bitly=(store_kind == "amazon"))

        message = build_product_message(info, user_name=user.first_name, review=review, price_line=price_line)
        kb = buy_button(short_url)
        await status_msg.delete()
        try:
            await update.message.delete()
        except Exception:
            pass

        chat = update.message.chat

        # 1) Video dal sito → nativo, al posto dell'immagine
        if video_url:
            try:
                await chat.send_video(video=video_url, caption=message, parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception as e:
                logger.warning(f"send_video error: {e}")

        # 2) Video YouTube pertinente → anteprima grande + bottone acquista
        if youtube_url:
            try:
                await chat.send_message(
                    f"{message}\n\n🎥 <a href='{youtube_url}'>Guarda il video</a>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=LinkPreviewOptions(url=youtube_url, prefer_large_media=True),
                )
                return
            except Exception as e:
                logger.warning(f"youtube send error: {e}")

        # 3) Foto personalizzata (card) se attiva, altrimenti immagine prodotto
        photo = await get_post_photo(info)
        if photo:
            try:
                await chat.send_photo(photo=photo, caption=message, parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception as e:
                logger.warning(f"Photo error: {e}")

        # 4) Solo testo
        await chat.send_message(message, parse_mode=ParseMode.HTML, reply_markup=kb)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ Errore. Riprova.")
        except Exception:
            pass


def main():
    start_health_check_server()
    init_firestore()
    init_rtdb()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("setchannel", setchannel_cmd))
    app.add_handler(CommandHandler("config", config_cmd))
    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("cleartokens", cleartokens_cmd))
    app.add_handler(CommandHandler("settag", settag_cmd))
    app.add_handler(CommandHandler("setrouting", setrouting_cmd))
    app.add_handler(CommandHandler("setvideo", setvideo_cmd))
    app.add_handler(CommandHandler("setmerchant", setmerchant_cmd))
    app.add_handler(CommandHandler("merchants", merchants_cmd))
    app.add_handler(CommandHandler("delmerchant", delmerchant_cmd))
    app.add_handler(CommandHandler("setbrand", setbrand_cmd))
    app.add_handler(CommandHandler("setcard", setcard_cmd))
    app.add_handler(CommandHandler("setbg", setbg_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("deal", deal_cmd))
    kb_labels = f"^({re.escape(BTN_CONFIG)}|{re.escape(BTN_PRODUCTS)}|{re.escape(BTN_CHANNEL)}|{re.escape(BTN_ADD)}|{re.escape(BTN_DEAL)}|{re.escape(BTN_TOKENS)}|{re.escape(BTN_TAG)}|{re.escape(BTN_MERCHANTS)}|{re.escape(BTN_CARD)}|{re.escape(BTN_HELP)})$"
    app.add_handler(MessageHandler(filters.Regex(kb_labels) & ~filters.COMMAND, keyboard_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    if app.job_queue:
        app.job_queue.run_repeating(monitor_prices, interval=CHECK_INTERVAL_MIN * 60, first=60)
        logger.info("Monitor prezzi schedulato")
        if SCHEDULED_POST_HOURS > 0:
            app.job_queue.run_repeating(scheduled_post, interval=SCHEDULED_POST_HOURS * 3600, first=120)
            logger.info(f"Post programmati ogni {SCHEDULED_POST_HOURS}h")
    else:
        logger.warning("JobQueue non disponibile: installa python-telegram-bot[job-queue]")

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
