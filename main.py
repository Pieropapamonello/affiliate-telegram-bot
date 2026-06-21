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
import time
import logging
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from telegram import Update, LinkPreviewOptions, ReplyKeyboardMarkup
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
    amazon = is_amazon_url(url)
    # Amazon nativo (tag) salvo che si voglia far passare tutto da Skimlinks
    if amazon and not (ROUTE_ALL_VIA_SKIMLINKS and DEEPLINK_TEMPLATE):
        normalized = normalize_amazon_url(url)
        return add_amazon_tag(normalized, AFFILIATE_TAG), "amazon"
    if DEEPLINK_TEMPLATE:
        target = normalize_amazon_url(url) if amazon else url
        affiliate = DEEPLINK_TEMPLATE.replace("{url}", quote_plus(target))
        return affiliate, "aggregator"
    if amazon:  # nessun aggregatore: ripiego sul tag nativo
        normalized = normalize_amazon_url(url)
        return add_amazon_tag(normalized, AFFILIATE_TAG), "amazon"
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
        "partnerTag": AFFILIATE_TAG,
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


def _clean_query(title: str) -> str:
    # Primi termini significativi del titolo (evita la coda marketing lunghissima)
    words = re.split(r"[\s,;:-]+", title or "")
    return " ".join(w for w in words if len(w) > 1)[:80]


async def find_youtube_video(title: str) -> str:
    """Trova un video YouTube pertinente e CORTO (<VIDEO_MAX_SECONDS) via API ufficiale."""
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
                    "maxResults": 5,
                    "videoDuration": "short",  # < 4 min, poi filtriamo a VIDEO_MAX_SECONDS
                    "relevanceLanguage": "it",
                },
            )
            sd = s.json()
            ids = [it["id"]["videoId"] for it in sd.get("items", []) if it.get("id", {}).get("videoId")]
            if not ids:
                return None
            v = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"key": YOUTUBE_API_KEY, "part": "contentDetails", "id": ",".join(ids)},
            )
            vd = v.json()
            for it in vd.get("items", []):
                dur = _iso8601_to_seconds(it.get("contentDetails", {}).get("duration"))
                if dur and dur <= VIDEO_MAX_SECONDS:
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


async def shorten_url(url: str) -> str:
    """Accorcia un link: Bitly (rotazione token) -> YOURLS -> is.gd. L'affiliazione resta nel link."""
    if get_bitly_tokens():
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


def build_product_message(info: dict, short_url: str, user_name: str = None, review: str = None) -> str:
    title = one_line(info.get("title") or "Prodotto")
    price = info.get("price") or ""
    rating = info.get("rating") or ""
    condition = info.get("condition_status") or ""
    promotion = info.get("promotion") or ""
    coupon = info.get("coupon") or ""
    source = info.get("source") or ""

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
    if source:
        msg += f"<b>🏪 Store:</b> {source}\n\n"
    if condition:
        msg += f"<b>🔄 Venditore:</b> {condition}\n\n"
    if clean_price:
        msg += f"<b>💰 Prezzo:</b> {clean_price}\n\n"
    if rating_stars:
        msg += f"{rating_stars}\n\n"
    if review:
        rev = max_lines(review, 3, line_len=70)
        if len(rev) > 200:
            rev = rev[:200].rsplit(" ", 1)[0] + "…"
        msg += f"<b>📝 Recensione:</b>\n{rev}\n\n"
    if promotion:
        msg += f"<b>🎉 Promozione:</b> {promotion}\n\n"
    if coupon:
        msg += f"<b>🎟️ Coupon:</b> {coupon}\n\n"
    msg += f"<b>🛒 Acquista qui:</b>\n{short_url}"
    return msg


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
def load_settings() -> dict:
    if USE_FIRESTORE:
        try:
            doc = firestore_db.collection("bot").document("settings").get()
            return doc.to_dict() if doc.exists else {}
        except Exception as e:
            logger.error(f"settings load error: {e}")
            return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s: dict) -> None:
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


# ----------------------------------------------------------------------------
# Pubblicazione offerta (canale o utente)
# ----------------------------------------------------------------------------
async def publish_deal(bot, entry: dict, info: dict, current_price: float, old_price: float = None):
    affiliate_url, _ = build_affiliate_link(entry["url"])
    short_url = await shorten_url(affiliate_url)

    if current_price is not None:
        if old_price and old_price > current_price:
            drop = round((1 - current_price / old_price) * 100)
            price_line = f"Prezzo: {current_price:.2f}€ (era {old_price:.2f}€, -{drop}%)"
        else:
            price_line = f"Prezzo: {current_price:.2f}€"
    else:
        price_line = "Prezzo: n/d"

    # Feed live offerte sul Realtime Database (se configurato)
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

    ai_text = await generate_ai_copy(info, price_line, short_url)
    target = entry.get("chat_id")
    destination = get_post_channel() or target

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
BTN_HELP = "❓ Aiuto"

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_CONFIG, BTN_PRODUCTS],
        [BTN_CHANNEL, BTN_ADD],
        [BTN_DEAL, BTN_TOKENS],
        [BTN_HELP],
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
        f"AI: {_ai_status()}\n"
        f"YouTube API: {'sì' if YOUTUBE_API_KEY else 'no'}\n"
        f"Token Bitly: {len(get_bitly_tokens())}",
        parse_mode=ParseMode.HTML,
    )


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
        price_line = f"Prezzo: {info.get('price') or 'n/d'}"
        review = await generate_ai_review(info, price_line)

        # Video: dal sito, altrimenti cercato su YouTube
        video_url = info.get("video")
        youtube_url = None
        if not video_url and info.get("title"):
            await status_msg.edit_text("🎥 Cerco un video...")
            youtube_url = await find_youtube_video(info["title"])

        await status_msg.edit_text("🔗 Accorciando...")
        short_url = await shorten_url(affiliate_url)

        message = build_product_message(info, short_url, user.first_name, review=review)
        await status_msg.delete()
        try:
            await update.message.delete()
        except Exception:
            pass

        chat = update.message.chat

        # 1) Video direttamente dal sito → al posto dell'immagine
        if video_url:
            try:
                await chat.send_video(video=video_url, caption=message, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                logger.warning(f"send_video error: {e}")

        # 2) Video trovato su YouTube → anteprima video al posto dell'immagine
        if youtube_url:
            try:
                await chat.send_message(
                    f"{message}\n\n🎥 <a href='{youtube_url}'>Guarda il video</a>",
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(url=youtube_url, prefer_large_media=True),
                )
                return
            except Exception as e:
                logger.warning(f"youtube send error: {e}")

        # 3) Immagine
        if info.get("image"):
            try:
                await chat.send_photo(photo=info["image"], caption=message, parse_mode=ParseMode.HTML)
                return
            except Exception as e:
                logger.warning(f"Photo error: {e}")

        # 4) Solo testo
        await chat.send_message(message, parse_mode=ParseMode.HTML)

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
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("deal", deal_cmd))
    kb_labels = f"^({re.escape(BTN_CONFIG)}|{re.escape(BTN_PRODUCTS)}|{re.escape(BTN_CHANNEL)}|{re.escape(BTN_ADD)}|{re.escape(BTN_DEAL)}|{re.escape(BTN_TOKENS)}|{re.escape(BTN_HELP)})$"
    app.add_handler(MessageHandler(filters.Regex(kb_labels) & ~filters.COMMAND, keyboard_router))
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
