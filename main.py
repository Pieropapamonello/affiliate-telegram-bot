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
from collections import deque
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

# Moduli interni
from ai import (
    GROQ_API_KEY, GROQ_MODEL,
    ai_complete as _ai_complete,
    groq_sync as _groq_sync,
    extract_json_array as _extract_json_array,
    ai_status as _ai_status,
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

# YouTube Data API (per trovare un video pertinente e corto) — opzionale
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
VIDEO_MAX_SECONDS = int(os.environ.get("VIDEO_MAX_SECONDS", 180))

# Immagine personalizzata (card) — prodotto + sfondo + scritta brand + badge store
BRAND_TEXT = os.environ.get("BRAND_TEXT", "Gli Affari di Nello")

# Scontorno AI del prodotto (rembg/U2Net via onnxruntime). Funziona anche su sfondi complessi.
# Disattivabile con CUTOUT_AI=0 se il piano Render va in OOM; in tal caso usa lo scontorno bianco.
CUTOUT_AI = os.environ.get("CUTOUT_AI", "1").lower() in ("1", "true", "yes", "si")
U2NET_MODEL_PATH = os.environ.get("U2NET_MODEL_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "u2netp.onnx"))

# AI: provider unico Groq (vedi ai.py). GROQ_API_KEY/GROQ_MODEL importati da ai.

PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

if not DEEPLINK_TEMPLATE and SKIMLINKS_ID:
    DEEPLINK_TEMPLATE = f"https://go.skimresources.com/?id={SKIMLINKS_ID}&xs=1&url={{url}}"

# Firestore (inizializzato in init_firestore)
firestore_db = None
USE_FIRESTORE = False


logger.info("Bot Configuration:")
logger.info(f"  TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:10]}...")
logger.info(f"  AFFILIATE_TAG (Amazon): {AFFILIATE_TAG or '(non impostato)'}")
logger.info(f"  Aggregatore: {'attivo' if DEEPLINK_TEMPLATE else 'NON configurato'}")
logger.info(f"  YOURLS: {'attivo' if (YOURLS_URL and YOURLS_SIGNATURE) else 'disattivo'}")
logger.info(f"  Canale auto-post: {CHANNEL_ID or '(non impostato)'}")
logger.info(f"  Monitor prezzi: ogni {CHECK_INTERVAL_MIN} min, soglia {DISCOUNT_THRESHOLD}%")
logger.info(f"  AI copy: {_ai_status()}")
logger.info(f"  DB: {'Firestore' if (FIRESTORE_PROJECT_ID or GOOGLE_CREDENTIALS_JSON) else 'file JSON'}")
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
# Contenuti pubblicati (in memoria + Firestore) — esposti via HTTP per la landing page
RECENT_DEALS = deque(maxlen=60)   # offerte inviate dagli utenti (storico mostrato sul sito)
ARTICLES = deque(maxlen=20)       # articoli/recensioni della "redazione" (1/giorno)
# Portale "Gli Affari di Nello": batch di contenuti tech generati da Groq, serviti via /portal.json
PORTAL = {"articles": [], "ticker": [], "editorial": "", "tags": [], "specs": [], "videos": [], "updated": 0}


def _find_article(aid):
    for a in list(ARTICLES) + PORTAL.get("articles", []):
        if a.get("id") == aid:
            return a
    return None


def record_recent_deal(title, price_line=None, store=None, url=None, image=None):
    if not title:
        return
    try:
        RECENT_DEALS.appendleft({
            "title": str(title)[:140],
            "price": (price_line or "").split("(")[0].strip(),
            "store": store or "",
            "url": url or "",
            "image": image or "",
        })
        _persist_fs("recent_deals", {"items": list(RECENT_DEALS)})
    except Exception:
        pass


def _persist_fs(doc_id, data):
    """Salva un documento in Firestore (best-effort)."""
    if not (USE_FIRESTORE and firestore_db):
        return
    try:
        firestore_db.collection("bot").document(doc_id).set(data)
    except Exception as e:
        logger.warning(f"persist {doc_id} fs: {e}")


def load_persisted_content():
    """Ricarica offerte e articoli da Firestore all'avvio (così non si perde nulla)."""
    if not (USE_FIRESTORE and firestore_db):
        return
    for doc_id, target in (("recent_deals", RECENT_DEALS), ("articles", ARTICLES)):
        try:
            doc = firestore_db.collection("bot").document(doc_id).get()
            if doc.exists:
                items = doc.to_dict().get("items", [])
                target.clear()
                for it in items[:target.maxlen]:
                    target.append(it)
                logger.info(f"Caricati {len(target)} elementi '{doc_id}' da Firestore")
        except Exception as e:
            logger.warning(f"load {doc_id} fs: {e}")
    try:
        doc = firestore_db.collection("bot").document("portal").get()
        if doc.exists:
            d = doc.to_dict() or {}
            for k in ("articles", "tags", "ticker", "editorial", "specs", "videos", "updated"):
                if k in d:
                    PORTAL[k] = d[k]
            logger.info(f"Portale caricato da Firestore: {len(PORTAL.get('articles', []))} articoli")
    except Exception as e:
        logger.warning(f"load portal fs: {e}")


def _json_response(handler, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "public, max-age=120")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Server HTTP pubblico (feed JSON + immagini) con tabella di rotte."""

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        for prefix, handler in HealthCheckHandler.ROUTES:
            if path.startswith(prefix):
                return handler(self, path)
        self._text("Bot is running")

    # --- helpers ---
    def _text(self, s):
        body = s.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _png(self, raw):
        if not raw:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-type", "image/png")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _query(self, key):
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    # --- rotte ---
    def r_health(self, path):
        self._text("ok")

    def r_deals(self, path):
        _json_response(self, {"deals": list(RECENT_DEALS)})

    def r_portal(self, path):
        arts = [{k: v for k, v in a.items() if k != "cover_b64"} for a in PORTAL.get("articles", [])]
        keys = ("ticker", "editorial", "tags", "specs", "videos", "updated")
        _json_response(self, {**{k: PORTAL[k] for k in keys}, "articles": arts})

    def r_ask(self, path):
        q = self._query("q")[:500]
        titles = [a.get("title") for a in PORTAL.get("articles", [])][:15]
        sysp = ("Sei l'assistente di 'Gli Affari di Nello', portale tech italiano. "
                "Rispondi SEMPRE in italiano, tono amichevole ed esperto, max 150 parole. "
                f"Notizie del giorno: {titles}")
        ans = _groq_sync(sysp, q) if q else "Ciao! Chiedimi qualcosa sulla tecnologia di oggi."
        _json_response(self, {"answer": ans or "Al momento non riesco a rispondere, riprova."})

    def r_article(self, path):
        a = _find_article(self._query("id"))
        if not a:
            return _json_response(self, {"title": "", "body": ""})
        if not a.get("full_body"):
            sysp = ("Sei un giornalista tech italiano della testata 'Gli Affari di Nello'. "
                    "Scrivi articoli completi, informativi e scorrevoli, in italiano corretto.")
            pr = (f"Scrivi un articolo completo di 450-600 parole sul tema: \"{a.get('title')}\" "
                  f"(categoria: {a.get('category')}). Inizia con un'introduzione (senza sottotitolo), "
                  "poi 2-3 sezioni ognuna con un sottotitolo su una riga che inizia con '## ', "
                  "e chiudi con una conclusione. Dettagli concreti e consigli pratici. "
                  "Niente titolo principale. Paragrafi separati da una riga vuota.")
            a["full_body"] = _groq_sync(sysp, pr, 1600) or a.get("body", "")
        _json_response(self, {"title": a.get("title"), "category": a.get("category"), "body": a.get("full_body")})

    def r_cover(self, path):
        plain = "plain=1" in self.path
        aid = path.rsplit("/", 1)[-1].replace(".png", "")
        raw = b""
        a = _find_article(aid)
        if a:
            try:
                import base64
                key = "cover_plain_b64" if plain else "cover_b64"
                if not a.get(key):
                    cover = (_compose_plain_cover(a.get("category")) if plain
                             else _compose_article_cover(a.get("title") or "", a.get("category")))
                    a[key] = base64.b64encode(cover).decode("ascii")
                raw = base64.b64decode(a[key])
            except Exception as e:
                logger.warning(f"cover regen: {e}")
        self._png(raw)

    ROUTES = [
        ("/healthz", r_health), ("/ping", r_health),
        ("/deals", r_deals), ("/portal", r_portal),
        ("/ask", r_ask), ("/img/article/", r_cover), ("/article", r_article),
    ]

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
async def fetch_html(url: str, attempts: int = 2) -> str:
    headers_base = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }
    for _ in range(attempts):
        for user_agent in USER_AGENTS:
            try:
                headers = dict(headers_base, **{"User-Agent": user_agent})
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    response = await client.get(url, headers=headers)
                    # pagina valida = ricca (le pagine anti-bot sono corte). Amazon/og recuperati comunque.
                    if response.status_code == 200 and len(response.text) > 3000:
                        return response.text
            except Exception as e:
                logger.warning(f"fetch_html error: {e}")
                continue
    return ""


CRAWLER_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "TelegramBot (like TwitterBot)",
    "Twitterbot/1.0",
    "WhatsApp/2.23.20.0",
]


async def fetch_og_html(url: str) -> str:
    """Scarica la pagina con User-Agent da crawler social: store come AliExpress/Temu
    servono gli og tag (titolo+immagine) ai crawler anche dai datacenter."""
    for ua in CRAWLER_UAS:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": ua, "Accept-Language": "it-IT,it;q=0.9"})
                if r.status_code == 200 and "og:title" in r.text:
                    return r.text
        except Exception as e:
            logger.warning(f"fetch_og_html error: {e}")
            continue
    return ""


async def fetch_via_jina(url: str) -> str:
    """Scarica la pagina tramite Jina Reader (IP non bloccato). Ritorna markdown/testo."""
    try:
        async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as client:
            r = await client.get("https://r.jina.ai/" + url, headers={"User-Agent": USER_AGENTS[0]})
            if r.status_code == 200 and len(r.text) > 80:
                return r.text
    except Exception as e:
        logger.warning(f"jina error: {e}")
    return ""


def _title_from_url(url: str) -> str:
    """Ricava un titolo leggibile dallo slug dell'URL (utile per Temu, ecc.)."""
    try:
        seg = urlparse(url).path.rstrip("/").split("/")[-1]
        seg = re.sub(r"\.html?$", "", seg, flags=re.I)
        seg = re.sub(r"-?g?-?\d{6,}$", "", seg)        # rimuove id prodotto finale
        seg = re.sub(r"[-_]+", " ", seg).strip()
        seg = re.sub(r"\s+", " ", seg)
        if len(seg) < 6:
            return None
        return seg[:120].strip().capitalize()
    except Exception:
        return None


def _jina_extract(text: str) -> tuple:
    """Estrae (title, image) dal markdown di Jina Reader."""
    title = None
    m = re.search(r"^Title:\s*(.+)$", text or "", re.M)
    if m:
        title = re.sub(r"\s*[-|]\s*AliExpress.*$", "", m.group(1).strip()).strip() or None
    img = None
    im = re.search(r"https://ae0?1\.alicdn\.com/kf/[A-Za-z0-9]+\.(?:jpg|jpeg|png|webp)", text or "")
    if im:
        img = im.group(0)
    return title, img


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
    # Store che bloccano i datacenter (AliExpress/Temu): riprova con UA da crawler
    # social, che ricevono gli og tag (titolo + immagine).
    if not is_amazon and (not html or "og:title" not in html):
        crawler_html = await fetch_og_html(url)
        if crawler_html:
            html = crawler_html

    if not html:
        info = dict(EMPTY_INFO)
        info["source"] = store_name_from_url(url)
        if not is_amazon:
            jt = await fetch_via_jina(url)
            if jt:
                t, img = _jina_extract(jt)
                info["title"], info["image"] = t, img
            if not info["title"]:
                info["title"] = _title_from_url(url)
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
    info["image"] = meta_content(soup, "og:image") or meta_content(soup, "twitter:image")
    info["video"] = extract_page_video(soup)
    info["source"] = store_name_from_url(url)
    info["price"] = meta_content(soup, "product:price:amount") or meta_content(soup, "og:price:amount")

    # Fallback immagine da CDN noti (es. Temu kwcdn, AliExpress alicdn)
    if not info["image"]:
        m = re.search(r'https://[a-z0-9.\-]*(?:kwcdn|alicdn|aliexpress-media)\.com/[^\s"\'\\)]+\.(?:jpg|jpeg|png|webp)', html)
        if m:
            info["image"] = m.group(0)

    # Fallback dati da JSON-LD (schema.org Product)
    ld = extract_jsonld_product(soup)
    if ld:
        info["title"] = info["title"] or ld.get("title")
        info["image"] = info["image"] or ld.get("image")
        if not info["price"] and ld.get("price"):
            info["price"] = _fmt_price(ld.get("price"), ld.get("currency"))

    # Microdata (itemprop="price")
    if not info["price"]:
        el = soup.find(attrs={"itemprop": "price"})
        if el:
            val = el.get("content") or el.get_text(strip=True)
            if val:
                curel = soup.find(attrs={"itemprop": "priceCurrency"})
                cur = (curel.get("content") if curel else None) or "EUR"
                info["price"] = _fmt_price(val, cur)

    # Fallback prezzo da JSON inline (es. AliExpress runParams)
    if not info["price"]:
        for pat in (
            r'"formatedActivityPrice"\s*:\s*"([^"]+)"',
            r'"formatedPrice"\s*:\s*"([^"]+)"',
            r'"minActivityAmount"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)',
            r'"minAmount"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)',
            r'"salePrice"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)',
        ):
            m = re.search(pat, html)
            if m:
                val = m.group(1)
                info["price"] = val if "€" in val or "," in val else f"{val}€"
                break

    # Fallback Jina se titolo/immagine ancora mancanti
    if not info["title"] or not info["image"]:
        jt = await fetch_via_jina(url)
        if jt:
            t, img = _jina_extract(jt)
            info["title"] = info["title"] or t
            info["image"] = info["image"] or img
    if not info["title"]:
        info["title"] = _title_from_url(url)
    return info


def _fmt_price(value, currency=None) -> str:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if any(sym in s for sym in ("€", "$", "£")):
        return s
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return f"{s}€"
    if cur == "USD":
        return f"${s}"
    if cur == "GBP":
        return f"£{s}"
    return f"{s} {cur}"


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
            price = cur = None
            if isinstance(offers, dict):
                # Offer normale, oppure AggregateOffer (lowPrice)
                price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                cur = offers.get("priceCurrency")
                # a volte il prezzo è annidato in priceSpecification
                if not price and isinstance(offers.get("priceSpecification"), dict):
                    price = offers["priceSpecification"].get("price")
                    cur = cur or offers["priceSpecification"].get("priceCurrency")
            img = c.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")
            return {"title": c.get("name"), "image": img, "price": price, "currency": cur}
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


SPEC_WORDS = {
    "pro", "plus", "max", "mini", "lite", "android", "ios", "wifi", "bluetooth",
    "lumen", "lumens", "smart", "tv", "led", "rgb", "usb", "hd", "uhd", "4k", "8k",
    "tragbarer", "projektor", "custodia", "cover", "case", "set", "kit", "new",
    "originale", "version", "edition", "gaming", "wireless",
}


def _norm_tokens(s: str) -> list:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _video_is_relevant(product_title: str, video_title: str) -> bool:
    """Match SEVERO: brand del prodotto + (numero di modello OR 2 parole distintive) nel titolo video."""
    ptoks = _norm_tokens(product_title)
    if not ptoks:
        return False
    vset = set(_norm_tokens(video_title))
    if not vset:
        return False
    brand = next((t for t in ptoks if len(t) >= 3 and t not in IT_STOPWORDS and t not in SPEC_WORDS), None)
    if not brand or brand not in vset:
        return False
    # token "modello": misti lettera+numero (es. hy300, v3, b0fhbs)
    models = [t for t in ptoks if len(t) >= 2 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)]
    if models:
        return any(m in vset for m in models)
    distinct = [t for t in ptoks if len(t) >= 4 and t not in IT_STOPWORDS and t not in SPEC_WORDS and not t.isdigit()]
    overlap = sum(1 for t in dict.fromkeys(distinct[:6]) if t in vset)
    return overlap >= 2


def _clean_query(title: str) -> str:
    words = re.split(r"[\s,;:/|]+", title or "")
    return " ".join(w for w in words if len(w) > 1)[:80]


async def find_youtube_video(title: str) -> str:
    """Trova una VIDEO-RECENSIONE pertinente (match severo): prima IT, poi EN.
    Ritorna None se nessun risultato è esattamente sul prodotto (meglio niente)."""
    if not (title and YOUTUBE_API_KEY):
        return None
    base = _clean_query(title)
    attempts = [("it", f"{base} recensione", "IT"), ("en", f"{base} review", None)]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for lang, q, region in attempts:
                params = {
                    "key": YOUTUBE_API_KEY,
                    "part": "snippet",
                    "q": q,
                    "type": "video",
                    "maxResults": 10,
                    "relevanceLanguage": lang,
                }
                if region:
                    params["regionCode"] = region
                s = await client.get("https://www.googleapis.com/youtube/v3/search", params=params)
                for it in s.json().get("items", []):
                    vid = it.get("id", {}).get("videoId")
                    vtitle = it.get("snippet", {}).get("title", "")
                    if vid and _video_is_relevant(title, vtitle):
                        return f"https://www.youtube.com/watch?v={vid}"
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
        # 1) Prezzo "da pagare" canonico (esclude il prezzo di listino barrato)
        for sel in [
            "span.priceToPay span.a-offscreen",
            "span.apexPriceToPay span.a-offscreen",
            "span.reinventPricePriceToPayMargin span.a-offscreen",
            "#sns-base-price span.a-offscreen",
        ]:
            el = soup.select_one(sel)
            if el and re.search(r"\d", el.get_text()):
                return el.get_text(strip=True)
        # 2) Nei blocchi ufficiali: primo a-offscreen che NON sia il prezzo barrato (a-text-price)
        for div_id in [
            "corePriceDisplay_desktop_feature_div",
            "corePrice_feature_div",
            "apex_desktop",
            "buybox",
        ]:
            div = soup.find(id=div_id)
            if not div:
                continue
            for sp in div.find_all("span", {"class": "a-offscreen"}):
                parent_cls = " ".join((sp.parent.get("class") or []) if sp.parent else [])
                if "a-text-price" in parent_cls:  # salta prezzo di listino / barrato
                    continue
                txt = sp.get_text(strip=True)
                if txt and re.search(r"\d", txt):
                    return txt
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


def _amazon_hires_url(url: str) -> str:
    """Trasforma un URL immagine Amazon ridimensionato (es. _SY300_SX300_ML2_) nella
    versione ad alta risoluzione e PULITA (senza overlay marketing): <id>._AC_SL1200_.jpg"""
    if not url:
        return url
    m = re.match(r"(https?://[^\s]+/I/[^.]+)\.[^/]+\.(jpg|jpeg|png)", url, re.I)
    if m:
        return f"{m.group(1)}._AC_SL1200_.{m.group(2)}"
    return url


def extract_amazon_image(soup) -> str:
    # Immagine principale ad ALTA risoluzione = vera foto prodotto intera (no thumbnail 300px,
    # no overlay marketing _ML_). Risolve le card con prodotto "tagliato male".
    li = soup.find("img", {"id": "landingImage"}) or soup.find("img", {"id": "imgTagWrapperId"})
    if li:
        hires = li.get("data-old-hires")
        if hires:
            return hires
        dyn = li.get("data-a-dynamic-image")
        if dyn:
            try:
                d = json.loads(dyn)
                if d:
                    return max(d.items(), key=lambda kv: (kv[1][0] * kv[1][1]) if len(kv[1]) == 2 else 0)[0]
            except Exception:
                pass
        if li.get("src"):
            return _amazon_hires_url(li["src"])
    for selector in [{"id": "imageBlockContainer"}, {"class": "a-dynamic-image"}]:
        elem = soup.find("img", selector)
        if elem and elem.get("src"):
            return _amazon_hires_url(elem["src"])
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


# ----------------------------------------------------------------------------
# Redazione: articoli/recensioni tech generati ogni giorno (con copertina)
# ----------------------------------------------------------------------------
def _compose_article_cover(title, category_hint=None) -> bytes:
    from PIL import Image, ImageDraw

    W, H = 1200, 675
    bg = _gradient(W, H, category_hint or title)
    draw = ImageDraw.Draw(bg)
    fk = _font(30)
    draw.text((60, 54), "GLI AFFARI DI NELLO · REDAZIONE", font=fk, fill=ACCENT)
    ft = _font(70)
    lines = _wrap(draw, title, ft, W - 120, 4)
    y = 150
    for ln in lines:
        draw.text((62, y + 2), ln, font=ft, fill=(0, 0, 0))
        draw.text((60, y), ln, font=ft, fill=(245, 247, 252))
        y += 84
    draw.rounded_rectangle([60, y + 16, 210, y + 26], radius=5, fill=ACCENT)
    fb = _font(32)
    draw.text((60, H - 66), "Guida all'acquisto · gliaffaridinello", font=fb, fill=(212, 218, 228))
    buf = io.BytesIO()
    bg.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _compose_plain_cover(category_hint=None) -> bytes:
    """Copertina pulita (solo gradiente a tema, senza testo) per le card del portale."""
    bg = _gradient(1200, 675, category_hint or "")
    buf = io.BytesIO()
    bg.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


async def generate_daily_article(context=None):
    """Pubblica una volta al giorno sul canale l'articolo TOP del portale.
    Niente generazione duplicata: riusa i contenuti già creati da generate_portal."""
    channel = get_post_channel()
    if not channel or context is None:
        return
    today = time.strftime("%Y-%m-%d")
    if ARTICLES and ARTICLES[0].get("date") == today:
        return  # già pubblicato oggi
    arts = PORTAL.get("articles") or []
    if not arts:
        return
    a = max(arts, key=lambda x: x.get("score", 0))
    title = a.get("title") or "Novità tech"
    body = a.get("body") or a.get("excerpt") or ""
    cover = None
    cover_b64 = ""
    try:
        import base64
        cover = await asyncio.to_thread(_compose_article_cover, title, a.get("category"))
        cover_b64 = base64.b64encode(cover).decode("ascii")
    except Exception as e:
        logger.warning(f"cover articolo: {e}")
    aid = a.get("id") or today.replace("-", "")
    ARTICLES.appendleft({
        "id": aid, "title": title, "body": body, "category": a.get("category"),
        "date": today, "ts": int(time.time()), "cover_b64": cover_b64,
    })
    _persist_fs("articles", {"items": [{k: v for k, v in x.items() if k != "cover_b64"} for x in ARTICLES]})
    try:
        import html as _html
        excerpt = (body.split("\n\n")[0] if body else "")[:600]
        caption = (f"📰 <b>{_html.escape(title)}</b>\n\n{_html.escape(excerpt)}"
                   f"\n\n<i>Gli Affari di Nello</i>")
        if cover:
            await context.bot.send_photo(chat_id=channel, photo=io.BytesIO(cover),
                                         caption=caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat_id=channel, text=caption, parse_mode=ParseMode.HTML)
        logger.info(f"Articolo del giorno pubblicato sul canale: {title}")
    except Exception as e:
        logger.warning(f"post articolo canale: {e}")


def _amazon_search_link(query):
    from urllib.parse import quote_plus
    link = f"https://www.amazon.it/s?k={quote_plus(query or 'tech')}"
    return link + (f"&tag={AFFILIATE_TAG}" if AFFILIATE_TAG else "")


async def fetch_tech_videos(n=4):
    """Video tech reali da YouTube (usa YOUTUBE_API_KEY)."""
    if not YOUTUBE_API_KEY:
        return []
    params = {"part": "snippet", "q": "recensione tech smartphone laptop gaming",
              "type": "video", "maxResults": n, "relevanceLanguage": "it",
              "order": "viewCount", "videoEmbeddable": "true", "key": YOUTUBE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://www.googleapis.com/youtube/v3/search", params=params)
            d = r.json()
        out = []
        for it in d.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            out.append({"title": sn.get("title", ""), "videoId": vid, "thumb": thumb,
                        "url": f"https://www.youtube.com/watch?v={vid}"})
        return out
    except Exception as e:
        logger.warning(f"youtube videos: {e}")
        return []


async def generate_portal(context=None):
    """Genera il batch di contenuti del portale 'Gli Affari di Nello' con Groq (max ogni 6h)."""
    if not GROQ_API_KEY:
        return
    if (PORTAL.get("articles") and PORTAL.get("specs") and PORTAL["specs"][0].get("review")
            and PORTAL.get("updated") and time.time() - PORTAL["updated"] < 6 * 3600):
        return
    sys = ("Sei il caporedattore di 'Gli Affari di Nello', portale tech italiano. "
           "Generi notizie e guide tech ORIGINALI, plausibili e attuali, in italiano corretto. "
           "Non spacciare per reali modelli o prezzi inventati: resta concreto ma generale.")
    prompt = (
        "Genera 14 articoli tech per la home del portale. Rispondi SOLO con un array JSON valido, niente altro testo.\n"
        'Ogni elemento: {"title":"max 75 caratteri","excerpt":"max 160 caratteri",'
        '"category":"una tra Smartphone,Laptop,Tablet,SmartHome,Gaming,AI,App,Video",'
        '"priority":"featured|breaking|normal","score":<1-10>,"tags":["t1","t2","t3"],'
        '"readingTime":<minuti>,"body":"2-3 paragrafi separati da \\n\\n, 120-180 parole totali"}\n'
        "Esattamente 1 elemento con priority=featured (lo score più alto). Varia le categorie."
    )
    raw = await _ai_complete(sys, prompt, 8000)
    arts = _extract_json_array(raw)
    if not arts:
        logger.warning("Portale: generazione non riuscita")
        return
    today = time.strftime("%Y%m%d")
    cleaned = []
    for i, a in enumerate(arts[:20]):
        if not isinstance(a, dict) or not a.get("title"):
            continue
        a["id"] = f"p{today}{i:02d}"
        a.setdefault("category", "Altro")
        a.setdefault("score", 5)
        a.setdefault("tags", [])
        a.setdefault("readingTime", 4)
        a.setdefault("priority", "normal")
        a.setdefault("excerpt", "")
        a.setdefault("body", "")
        a.pop("cover_b64", None)
        cleaned.append(a)
    if not cleaned:
        return
    if not any(a.get("priority") == "featured" for a in cleaned):
        max(cleaned, key=lambda x: x.get("score", 0))["priority"] = "featured"
    tags = []
    for a in cleaned:
        for t in (a.get("tags") or []):
            if t and t not in tags:
                tags.append(t)
    ticker = [a["title"] for a in sorted(cleaned, key=lambda x: x.get("score", 0), reverse=True)][:6]
    editorial = await _ai_complete(
        "Sei un editorialista tech italiano.",
        f"In 2-3 frasi scrivi l'editoriale di oggi sul tema tech dominante, a partire da questi titoli: {[a['title'] for a in cleaned[:8]]}",
        220,
    ) or ""
    # Schede tecniche (prodotti) generate dall'AI, con link Amazon affiliato
    specs = []
    specs_raw = await _ai_complete(
        "Sei un esperto di prodotti tech italiani.",
        ("Genera 6 schede tecniche di categorie/prodotti tech popolari e realistici. "
         "Rispondi SOLO con un array JSON, niente altro testo. Ogni elemento: "
         '{"name":"nome prodotto/categoria","category":"Smartphone|Laptop|Tablet|Gaming|SmartHome|App",'
         '"price":"fascia di prezzo es. €299-399","rating":<1-5>,"specs":["spec1","spec2","spec3"],'
         '"review":"2 frasi di recensione in italiano","pros":"max 8 parole","cons":"max 8 parole",'
         '"query":"termine di ricerca Amazon"}'),
        2000,
    )
    for s in (_extract_json_array(specs_raw) or [])[:8]:
        if isinstance(s, dict) and s.get("name"):
            s["url"] = _amazon_search_link(s.get("query") or s.get("name"))
            s.pop("query", None)
            specs.append(s)
    PORTAL["articles"] = cleaned
    PORTAL["tags"] = tags[:14]
    PORTAL["ticker"] = ticker
    PORTAL["editorial"] = editorial.strip()
    PORTAL["specs"] = specs
    PORTAL["videos"] = await fetch_tech_videos(4)
    PORTAL["updated"] = int(time.time())
    _persist_fs("portal", {k: PORTAL[k] for k in ("articles", "tags", "ticker", "editorial", "specs", "videos", "updated")})
    logger.info(f"Portale generato: {len(cleaned)} articoli, {len(specs)} schede, {len(PORTAL['videos'])} video")


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

STORE_DOMAINS = {
    "Amazon": "amazon.it", "AliExpress": "aliexpress.com", "eBay": "ebay.it",
    "Zalando": "zalando.it", "Temu": "temu.com", "MediaWorld": "mediaworld.it",
    "Unieuro": "unieuro.it", "Banggood": "banggood.com", "Shein": "shein.com",
    "IKEA": "ikea.com", "Decathlon": "decathlon.it", "Zooplus": "zooplus.it",
}

# Temi colore per categoria prodotto (sfondo quando manca l'immagine)
CATEGORY_THEMES = [
    (("gatt", "cane", "cani", "cucc", "pet", "whiskas", "croccant", "animal", "zoo", "acquar"), ((26, 120, 70), (8, 34, 20))),
    (("proiett", "cuffi", "auricol", "smartphone", "telefono", "tablet", "televis", "monitor", "laptop", "robot", "drone", "console", "elettro", "tech", "caric", "power", "camera"), ((24, 70, 150), (8, 16, 40))),
    (("trucco", "crema", "beauty", "make", "profumo", "skincare", "capell"), ((170, 40, 120), (44, 10, 34))),
    (("gioco", "funko", "lego", "puzzle", "collez", "nerd", "anime", "manga", "carte"), ((150, 60, 205), (30, 12, 52))),
    (("cucina", "casa", "arred", "mobil", "tavol", "letto", "lampada", "tenda", "aspira"), ((170, 95, 30), (44, 24, 8))),
    (("scarp", "maglia", "giacca", "vestit", "abbig", "moda", "borsa", "orolog"), ((40, 90, 130), (10, 24, 36))),
]


def _store_logo_url(store: str) -> str:
    if not store:
        return None
    domain = STORE_DOMAINS.get(store) or f"{store.lower().replace(' ', '')}.com"
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


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


ACCENT = (255, 138, 0)


def _category_colors(title: str):
    t = (title or "").lower()
    for keys, colors in CATEGORY_THEMES:
        if any(k in t for k in keys):
            return colors
    return ((40, 30, 66), (10, 11, 18))  # default synthwave


def _gradient(w: int, h: int, title: str = None):
    from PIL import Image, ImageDraw, ImageFilter

    c_top, c_bot = _category_colors(title)
    base = Image.new("RGB", (w, h), c_bot)
    top = Image.new("RGB", (w, h), c_top)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    for y in range(h):
        md.line([(0, y), (w, y)], fill=int(190 * (1 - y / h)))
    base = Image.composite(top, base, mask)

    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w * 0.45, -h * 0.25, w * 1.25, h * 0.55], fill=c_top + (110,))
    gd.ellipse([-w * 0.30, h * 0.50, w * 0.55, h * 1.25], fill=ACCENT + (60,))
    glow = glow.filter(ImageFilter.GaussianBlur(160))

    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([-w * 0.25, -h * 0.25, w * 1.25, h * 1.25], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(220))
    out = Image.alpha_composite(base.convert("RGBA"), glow).convert("RGB")
    dark = Image.new("RGB", (w, h), (6, 6, 10))
    return Image.composite(out, dark, vig)


def _blurred_backdrop(prod_img, w: int, h: int):
    """Sfondo = immagine prodotto ingrandita e sfocata (colori coerenti con l'articolo)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageEnhance

    bg = ImageOps.fit(prod_img.convert("RGB"), (w, h))
    bg = bg.filter(ImageFilter.GaussianBlur(60))
    bg = ImageEnhance.Color(bg).enhance(1.25)        # colori più vivi
    bg = Image.blend(bg, Image.new("RGB", (w, h), (0, 0, 0)), 0.45)  # scurisci per contrasto
    # vignettatura
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([-w * 0.2, -h * 0.2, w * 1.2, h * 1.2], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(200))
    return Image.composite(bg, Image.new("RGB", (w, h), (6, 6, 10)), vig)


def _rounded(size, radius, fill):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=fill)
    return img


def _wrap(draw, text, font, max_w, max_lines=2):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    lines = lines[:max_lines]
    if lines and len(" ".join(lines)) < len(text or ""):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _draw_store_chip(draw, bg, store, logo_bytes):
    """Chip bianco con logo (favicon) + nome store, in alto a sinistra."""
    if not store:
        return
    from PIL import Image

    x, y, h, pad = 56, 52, 74, 18
    icon = None
    if logo_bytes:
        try:
            icon = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            icon.thumbnail((50, 50))
        except Exception:
            icon = None
    f = _font(36)
    tw = draw.textlength(store, font=f)
    icon_w = (icon.width + 12) if icon else 0
    chip_w = int(pad + icon_w + tw + pad)
    chip = _rounded((chip_w, h), 22, (255, 255, 255, 240))
    bg.paste(chip, (x, y), chip)
    cx = x + pad
    if icon:
        bg.paste(icon, (cx, y + (h - icon.height) // 2), icon)
        cx += icon.width + 12
    draw.text((cx, y + (h - f.size) / 2 - 2), store, font=f, fill=(20, 20, 20))


def _fit_big(img, max_w, max_h):
    """Scala l'immagine (anche INGRANDENDOLA) per riempire l'area, mantenendo le proporzioni."""
    from PIL import Image

    w, h = img.size
    if not w or not h:
        return img
    scale = min(max_w / w, max_h / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


_REMBG_SESSION = None
_REMBG_TRIED = False


def _get_rembg_session():
    """Carica una sola volta la sessione onnxruntime per U2Net-p (lazy, leggera)."""
    global _REMBG_SESSION, _REMBG_TRIED
    if _REMBG_TRIED:
        return _REMBG_SESSION
    _REMBG_TRIED = True
    if not CUTOUT_AI:
        return None
    try:
        import onnxruntime as ort

        if not os.path.exists(U2NET_MODEL_PATH):
            logger.warning(f"Modello scontorno AI non trovato: {U2NET_MODEL_PATH}")
            return None
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        _REMBG_SESSION = ort.InferenceSession(
            U2NET_MODEL_PATH, sess_options=so, providers=["CPUExecutionProvider"]
        )
        logger.info("Scontorno AI (U2Net-p) attivo.")
    except Exception as e:
        logger.warning(f"Scontorno AI non disponibile: {e}")
        _REMBG_SESSION = None
    return _REMBG_SESSION


def _cutout_ai(prod):
    """Scontorna con U2Net-p (rembg) — funziona anche su sfondi complessi.
    Ritorna (immagine_RGBA, scontornata_bool)."""
    sess = _get_rembg_session()
    if sess is None:
        return None, False
    try:
        import numpy as np
        from PIL import Image, ImageFilter

        src = prod.convert("RGB")
        # cap dimensione per memoria/CPU su piano free
        work = src
        if max(src.size) > 1024:
            work = src.copy()
            work.thumbnail((1024, 1024), Image.LANCZOS)

        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        im = work.resize((320, 320), Image.LANCZOS)
        arr = np.array(im).astype(np.float32)
        mx = arr.max() or 1.0
        arr = arr / mx
        tmp = np.zeros((320, 320, 3), dtype=np.float32)
        for c in range(3):
            tmp[:, :, c] = (arr[:, :, c] - mean[c]) / std[c]
        tmp = tmp.transpose((2, 0, 1))[np.newaxis, :, :, :]

        out = sess.run(None, {sess.get_inputs()[0].name: tmp})[0]
        pred = out[:, 0, :, :]
        mi, ma = pred.min(), pred.max()
        pred = (pred - mi) / ((ma - mi) or 1.0)
        mask = Image.fromarray((np.squeeze(pred) * 255).astype("uint8"), mode="L")
        mask = mask.resize(src.size, Image.LANCZOS)
        mask = mask.filter(ImageFilter.GaussianBlur(0.8))

        # guard: maschera inutile (quasi vuota o quasi piena)
        m = np.asarray(mask)
        ratio = float((m > 40).mean())
        if ratio < 0.03 or ratio > 0.99:
            return None, False

        out_img = src.convert("RGBA")
        out_img.putalpha(mask)
        bbox = mask.getbbox()  # ritaglio sul soggetto (canale alpha), non sull'RGB
        if bbox:
            out_img = out_img.crop(bbox)
        return out_img, True
    except Exception as e:
        logger.warning(f"scontorno AI errore: {e}")
        return None, False


def _cutout(prod):
    """Scontorna il prodotto. Prima lo sfondo bianco/uniforme (foto e-commerce: tiene
    TUTTO il prodotto, niente errori), poi l'AI per gli sfondi complessi (lifestyle)."""
    cut, ok = _cutout_white_bg(prod)
    if ok:
        return cut, True
    return _cutout_ai(prod)


def _cutout_white_bg(prod):
    """Scontorna il prodotto rimuovendo lo sfondo bianco/uniforme (se presente).
    Ritorna (immagine_RGBA, scontornata_bool)."""
    from PIL import Image, ImageDraw, ImageFilter

    im = prod.convert("RGB")
    w, h = im.size
    corners = [im.getpixel((1, 1)), im.getpixel((w - 2, 1)), im.getpixel((1, h - 2)), im.getpixel((w - 2, h - 2))]
    if not all(min(c) >= 222 for c in corners):
        return prod.convert("RGBA"), False  # sfondo non bianco → niente scontorno
    work = im.copy()
    SENT = (255, 0, 255)
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for s in seeds:
        try:
            ImageDraw.floodfill(work, s, SENT, thresh=72)
        except Exception:
            pass
    alpha = Image.new("L", (w, h))
    alpha.putdata([0 if p == SENT else 255 for p in work.getdata()])
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.8))
    out = prod.convert("RGBA")
    out.putalpha(alpha)
    bbox = alpha.getbbox()  # ritaglio sul SOGGETTO (canale alpha), non sull'RGB bianco
    if bbox:
        out = out.crop(bbox)
    # se ha rimosso quasi tutto o quasi nulla, considera fallita
    opaque = sum(1 for a in alpha.getdata() if a > 30)
    if opaque < (w * h * 0.03) or opaque > (w * h * 0.985):
        return prod.convert("RGBA"), False
    return out, True


def _drop_shadow(bg_rgb, obj_rgba, x, y, blur=18, offset=(6, 16), alpha=0.55):
    """Aggiunge l'ombra di un oggetto RGBA su bg (RGB), ritorna RGB."""
    from PIL import Image

    W, H = bg_rgb.size
    sh = Image.new("RGBA", obj_rgba.size, (0, 0, 0, 0))
    sh.putalpha(obj_rgba.split()[3].point(lambda a: int(a * alpha)))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.paste(sh, (x + offset[0], y + offset[1]), sh)
    from PIL import ImageFilter

    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(bg_rgb.convert("RGBA"), layer).convert("RGB")


def _compose_card(img_bytes, store, brand_text, bg_bytes=None, price=None, old_price=None,
                  is_min=False, title=None, logo_bytes=None) -> bytes:
    from PIL import Image, ImageDraw, ImageOps, ImageFilter

    W = H = 1080
    prod = None
    if img_bytes:
        try:
            prod = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        except Exception:
            prod = None

    # Sfondo: /setbg utente > backdrop sfocato del prodotto > gradiente per categoria
    if bg_bytes:
        try:
            bg = ImageOps.fit(Image.open(io.BytesIO(bg_bytes)).convert("RGB"), (W, H))
            bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.5)
        except Exception:
            bg = _gradient(W, H, title)
    elif prod is not None:
        bg = _blurred_backdrop(prod, W, H)
    else:
        bg = _gradient(W, H, title)

    draw = ImageDraw.Draw(bg)
    footer_h = 130 + (84 if is_min else 0)

    if prod is not None:
        # --- Prodotto GRANDISSIMO: scontornato sullo sfondo, oppure su card bianca ---
        card_top = 150
        box_w = 920
        box_h = (H - footer_h - 40) - card_top - 10
        cut, is_cut = _cutout(prod)
        if is_cut:
            # Prodotto scontornato, enorme, direttamente sullo sfondo (stile "Affari da Nerd")
            big = _fit_big(cut, box_w, box_h)
            bx = (W - big.width) // 2
            by = card_top + (box_h - big.height) // 2
            bg = _drop_shadow(bg, big, bx, by, blur=22, offset=(8, 20), alpha=0.5)
            bg_rgba = bg.convert("RGBA")
            bg_rgba.paste(big, (bx, by), big)
            bg = bg_rgba.convert("RGB")
            draw = ImageDraw.Draw(bg)
            y_after = by + big.height + 30
        else:
            # Sfondo non bianco: prodotto INGRANDITO su card bianca arrotondata
            pad = 30
            big = _fit_big(prod, box_w - 2 * pad, box_h - 2 * pad)
            cw, ch = big.width + 2 * pad, big.height + 2 * pad
            cx, cy = (W - cw) // 2, card_top + (box_h - ch) // 2
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                [cx + 8, cy + 24, cx + cw + 8, cy + ch + 24], radius=46, fill=(0, 0, 0, 200)
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(28))
            bg = Image.alpha_composite(bg.convert("RGBA"), shadow).convert("RGB")
            card = _rounded((cw, ch), 46, (255, 255, 255, 255))
            card.paste(big, (pad, pad), big)
            bg.paste(card, (cx, cy), card)
            draw = ImageDraw.Draw(bg)
            y_after = cy + ch + 42
    else:
        # --- Senza immagine: titolo grande centrato ---
        ft = _font(60)
        lines = _wrap(draw, title or "Offerta", ft, 920, 4)
        block_h = len(lines) * 74
        ty = max(300, (H - block_h) // 2 - 30)
        for ln in lines:
            lw = draw.textlength(ln, font=ft)
            draw.text(((W - lw) / 2 + 2, ty + 2), ln, font=ft, fill=(0, 0, 0))
            draw.text(((W - lw) / 2, ty), ln, font=ft, fill=(245, 245, 245))
            ty += 74
        y_after = ty + 30

    # Logo store (chip con favicon + nome) in alto a sinistra
    _draw_store_chip(draw, bg, store, logo_bytes)

    # Sticker prezzo (in alto a destra)
    if price is not None:
        fbig = _font(64)
        ptext = f"{price:.2f}€"
        pw = draw.textlength(ptext, font=fbig)
        disc = None
        if old_price and old_price > price:
            disc = f"-{round((1 - price / old_price) * 100)}%"
        fdisc = _font(34)
        sw = int(max(pw, draw.textlength(disc or "", font=fdisc)) + 56)
        sh = 132 if disc else 96
        sx, sy = W - sw - 64, 60
        sticker = _rounded((sw, sh), 26, ((229, 57, 53, 255) if disc else ACCENT + (255,)))
        bg.paste(sticker, (sx, sy), sticker)
        ty = sy + 16
        if disc:
            dw = draw.textlength(disc, font=fdisc)
            draw.text((sx + (sw - dw) / 2, ty), disc, font=fdisc, fill=(255, 255, 255))
            ty += 44
        draw.text((sx + (sw - pw) / 2, ty), ptext, font=fbig, fill=(255, 255, 255))
        if disc:
            fo = _font(30)
            ot = f"{old_price:.2f}€"
            ow = draw.textlength(ot, font=fo)
            oy = sy + sh + 8
            draw.text((sx + (sw - ow) / 2, oy), ot, font=fo, fill=(210, 210, 210))
            draw.line([sx + (sw - ow) / 2, oy + 19, sx + (sw + ow) / 2, oy + 19], fill=(210, 210, 210), width=3)

    # Ribbon "MINIMO STORICO" (y_after calcolato nel layout sopra)
    if is_min:
        fr = _font(36)
        rt = "MINIMO STORICO"
        rw = draw.textlength(rt, font=fr)
        rp = 24
        rx = (W - (rw + 2 * rp)) // 2
        rib = _rounded((int(rw + 2 * rp), 60), 16, (229, 57, 53, 255))
        bg.paste(rib, (int(rx), int(y_after)), rib)
        draw.text((rx + rp, y_after + 12), rt, font=fr, fill=(255, 255, 255))
        y_after += 84

    # Footer brand + riga accento
    bt = brand_text or ""
    if bt:
        f = _font(74)
        tw = draw.textlength(bt, font=f)
        bx = (W - tw) / 2
        draw.text((bx + 3, y_after + 3), bt, font=f, fill=(0, 0, 0))
        draw.text((bx, y_after), bt, font=f, fill=(255, 255, 255))
        uw = min(tw, 540)
        uy = y_after + 92
        draw.rounded_rectangle([(W - uw) / 2, uy, (W + uw) / 2, uy + 9], radius=4, fill=ACCENT)

    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


async def generate_card_image(image_url, store, brand_text, price=None, old_price=None,
                              is_min=False, title=None) -> bytes:
    img = None
    if image_url:
        img = await fetch_bytes(image_url)
    # Senza immagine ma con titolo → card "testuale" brandizzata (no card vuota)
    if not img and not title:
        return None
    bg = None
    bgurl = get_bg_image_url()
    if bgurl:
        bg = await fetch_bytes(bgurl)
    logo = None
    lu = _store_logo_url(store)
    if lu:
        logo = await fetch_bytes(lu)
    try:
        return await asyncio.to_thread(
            _compose_card, img, store, brand_text, bg, price, old_price, is_min, title, logo
        )
    except Exception as e:
        logger.warning(f"card image error: {e}")
        return None


async def get_post_photo(info: dict, price=None, old_price=None, is_min=False):
    """Card personalizzata (byte) se attiva, altrimenti URL immagine, altrimenti None."""
    if card_enabled() and (info.get("image") or info.get("title")):
        card = await generate_card_image(
            info.get("image"), info.get("source"), get_brand_text(),
            price, old_price, is_min, info.get("title"),
        )
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

    lines = []
    if user_name:
        lines.append(f"👤 <i>{user_name} ha condiviso questo articolo</i>")
        lines.append("")
    lines.append(f"🛍️ <b>{title}</b>")
    lines.append("")  # separatore

    info_block = []
    if source:
        seller = f" · {condition}" if (condition and "Usato" in condition) else ""
        info_block.append(f"🏪 <b>Store:</b> {source}{seller}")
    if price_line:
        info_block.append(f"💰 <b>Prezzo:</b> {price_line}")
    if rating:
        info_block.append(f"⭐ <b>Voto:</b> {rating}/5")
    if info_block:
        lines.extend(info_block)
        lines.append("")

    if review:
        rev = max_lines(review, 3, line_len=70)
        if len(rev) > 200:
            rev = rev[:200].rsplit(" ", 1)[0] + "…"
        lines.append(f"📝 <i>{rev}</i>")

    if short_url:
        lines.append("")
        lines.append(f"🛒 {short_url}")

    return "\n".join(lines).strip()


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
            # In caso di errore NON sovrascrivo la cache: ritorno l'ultimo valore buono
            return _settings_cache["data"] or {}
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


# Promo Amazon con "bounty" (commissione fissa per iscrizione) — pagine ufficiali Associates
AMAZON_PROMOS = [
    ("🛒 Prova Amazon Prime (30gg gratis)", "https://www.amazon.it/amazonprime"),
    ("🎵 Amazon Music Unlimited (30gg gratis)", "https://www.amazon.it/gp/dmusic/promotions/AmazonMusicUnlimited"),
    ("🎧 Audible (30gg gratis)", "https://www.amazon.it/hz/audible/mlp"),
    ("📖 Kindle Unlimited (30gg gratis)", "https://www.amazon.it/kindle-dbs/hz/signup"),
    ("🎓 Prime Student (90gg gratis)", "https://www.amazon.it/joinstudent"),
    ("💍 Lista Nozze Amazon", "https://www.amazon.it/wedding"),
    ("👶 Lista Nascita Amazon", "https://www.amazon.it/baby-reg/homepage"),
]


def amazon_promo_keyboard() -> InlineKeyboardMarkup:
    """Pulsanti delle promo Amazon, ognuno col tag affiliato impostato."""
    tag = get_affiliate_tag()
    rows = []
    for label, url in AMAZON_PROMOS:
        full = url + (f"?tag={tag}" if tag else "")
        rows.append([InlineKeyboardButton(label, url=full)])
    return InlineKeyboardMarkup(rows)


def video_enabled() -> bool:
    # Default OFF: i video estratti dalle pagine non sono affidabili (spesso non
    # sono del prodotto). Si usa sempre la card personalizzata. Riattivabile con /setvideo on.
    return load_settings().get("video_enabled", False)


def review_link_enabled() -> bool:
    return load_settings().get("review_link", True)


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
    record_recent_deal(info.get("title"), price_line,
                       "Amazon" if kind == "amazon" else info.get("source"),
                       short_url, info.get("image"))

    destination = get_post_channel() or entry.get("chat_id")
    photo = await get_post_photo(info, price=current_price, old_price=old_price, is_min=hist.get("is_new_min"))

    review_link = None
    if review_link_enabled() and YOUTUBE_API_KEY and info.get("title"):
        review_link = await find_youtube_video(info["title"])

    ai_text = await generate_ai_copy(info, f"Prezzo: {price_line}", short_url)
    if ai_text:
        body = ai_text.replace(short_url, "").strip()
        if hist.get("is_new_min"):
            body = "🔥 MINIMO STORICO\n" + body
        if review_link:
            body += f"\n\n🎥 Recensione: {review_link}"
        body += f"\n\n🛒 {short_url}"  # link nel testo (copiabile), non come pulsante
        if photo:
            try:
                await bot.send_photo(chat_id=destination, photo=photo, caption=body)
                mark_posted(entry["url"])
                return
            except Exception as e:
                logger.warning(f"send_photo error: {e}")
        await bot.send_message(chat_id=destination, text=body, disable_web_page_preview=True)
        mark_posted(entry["url"])
        return

    # Fallback senza AI
    message = build_product_message(info, short_url=short_url, user_name=None, price_line=price_line)
    if review_link:
        message += f"\n\n🎥 <a href='{review_link}'>Guarda la video-recensione</a>"
    if photo:
        try:
            await bot.send_photo(chat_id=destination, photo=photo, caption=message,
                                 parse_mode=ParseMode.HTML)
            mark_posted(entry["url"])
            return
        except Exception as e:
            logger.warning(f"send_photo error: {e}")
    await bot.send_message(chat_id=destination, text=message, parse_mode=ParseMode.HTML)
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
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "👋 <b>Pannello admin</b>\nUsa i pulsanti qui sotto per gestire il bot. 👇",
            reply_markup=ADMIN_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "👋 <b>Benvenuto su Gli Affari di Nello!</b>\n\n"
            "• Inviami il <b>link di un prodotto</b> (Amazon o altri store) e ti do l'offerta pronta. 🛒\n"
            "• /promo — le <b>promo Amazon gratis</b> del momento\n"
            "• /aiuto — come funziona",
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(
            "🎁 <b>Promo Amazon attive — prova GRATIS:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=amazon_promo_keyboard(),
        )


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
BTN_PROMO = "🎁 Promo Amazon"
BTN_HELP = "❓ Aiuto"

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_CONFIG, BTN_PRODUCTS],
        [BTN_CHANNEL, BTN_ADD],
        [BTN_DEAL, BTN_TOKENS],
        [BTN_TAG, BTN_MERCHANTS],
        [BTN_CARD, BTN_PROMO],
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
    elif text == BTN_TAG:
        context.user_data["await"] = "amazon_tag"
        await update.message.reply_text(
            f"🏷️ Tag Amazon attuale: <b>{get_affiliate_tag() or '(nessuno)'}</b>\n\n"
            "✍️ Scrivimi ora il <b>nuovo tag</b> (es. <code>nellobuy-21</code>) e lo imposto.\n"
            "Routing link Amazon: /setrouting native|skimlinks",
            parse_mode=ParseMode.HTML,
        )
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
    elif text == BTN_PROMO:
        await promo_cmd(update, context)
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
    """True se l'utente NON è admin. (Per diventarlo: /admin <password>.)"""
    return not is_admin(update.effective_user.id)


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
        f"Video nativo: {'attivo' if video_enabled() else 'disattivo'}\n"
        f"Link recensione YT: {'attivo' if review_link_enabled() else 'disattivo'}\n"
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


async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra/pubblica i pulsanti delle promo Amazon (col tag affiliato attuale)."""
    text = ("🎁 <b>Promo Amazon attive — GRATIS</b>\n"
            "Prova gratis e disdici quando vuoi 👇")
    kb = amazon_promo_keyboard()
    # /promo canale  -> pubblica sul canale (solo admin)
    if context.args and context.args[0].lower() == "canale" and is_admin(update.effective_user.id):
        channel = get_post_channel()
        if not channel:
            await update.message.reply_text("⚠️ Nessun canale impostato. Usa /setchannel @tuocanale")
            return
        try:
            await context.bot.send_message(channel, text, parse_mode=ParseMode.HTML, reply_markup=kb)
            await update.message.reply_text("✅ Promo pubblicate sul canale.")
        except Exception as e:
            await update.message.reply_text(f"❌ Canale non raggiungibile: {e}")
        return
    suffix = "\n\n<i>Admin: /promo canale per pubblicarle sul canale.</i>" if is_admin(update.effective_user.id) else ""
    await update.message.reply_text(text + suffix, parse_mode=ParseMode.HTML, reply_markup=kb)


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


async def setreview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _deny_if_not_admin(update):
        await update.message.reply_text("❌ Solo gli admin. Usa /admin <password>.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        cur = "attivo" if review_link_enabled() else "disattivo"
        await update.message.reply_text(
            f"Link video-recensione: <b>{cur}</b>\nUso: /setreview on|off", parse_mode=ParseMode.HTML
        )
        return
    on = context.args[0].lower() == "on"
    s = load_settings()
    s["review_link"] = on
    save_settings(s)
    await update.message.reply_text(f"✅ Link video-recensione {'attivato' if on else 'disattivato'}.")


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
        await update.message.reply_text(
            "Uso: /deal <link> [prezzo] [prezzo_vecchio]\n"
            "Es: /deal https://aliexpress.it/item/... 79.99 99.99\n"
            "(il prezzo è utile dove non si legge in automatico, es. AliExpress)"
        )
        return
    url = context.args[0]
    manual_price = parse_price_to_float(context.args[1]) if len(context.args) > 1 else None
    manual_old = parse_price_to_float(context.args[2]) if len(context.args) > 2 else None
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
    price = manual_price if manual_price is not None else parse_price_to_float(info.get("price"))
    if manual_price is not None:
        info["price"] = f"{manual_price:.2f}€"
    entry = {"chat_id": update.message.chat_id, "url": url, "is_amazon": (store_kind == "amazon")}
    await publish_deal(context.bot, entry, info, price, old_price=manual_old)
    try:
        await status.delete()
    except Exception:
        pass
    if CHANNEL_ID:
        await update.message.reply_text("✅ Offerta pubblicata sul canale.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user = update.message.from_user

    # Input "in attesa" da un pulsante config (es. Tag Amazon)
    pending = context.user_data.get("await")
    if pending and is_admin(update.effective_user.id):
        context.user_data.pop("await", None)
        val = (text or "").strip()
        if pending == "amazon_tag" and val:
            s = load_settings()
            s["amazon_tag"] = val
            save_settings(s)
            note = ""
            if route_via_skimlinks():
                note = "\n⚠️ Routing su Skimlinks: il tag non viene usato. Per usarlo: /setrouting native"
            await update.message.reply_text(
                f"✅ Tag Amazon impostato: <b>{val}</b>{note}", parse_mode=ParseMode.HTML
            )
        return

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

        price_f = parse_price_to_float(info.get("price"))
        has_real_title = bool(info.get("title")) and len(info.get("title") or "") > 5
        review = None
        if has_real_title:
            await status_msg.edit_text("📝 Scrivo la recensione...")
            review = await generate_ai_review(info, f"Prezzo: {info.get('price') or 'n/d'}")

        hist = record_observation(url, price_f)
        price_line = build_price_line(price_f, hist) if price_f is not None else (info.get("price") or "")

        review_link = None
        if has_real_title and review_link_enabled() and YOUTUBE_API_KEY:
            await status_msg.edit_text("🎥 Cerco una recensione video...")
            review_link = await find_youtube_video(info["title"])

        await status_msg.edit_text("🔗 Accorciando...")
        short_url = await shorten_url(affiliate_url, use_bitly=(store_kind == "amazon"))
        chat = update.message.chat
        sent = False  # cancelliamo il messaggio originale SOLO dopo un invio riuscito

        # --- CON immagine prodotto → card brandizzata ---
        if info.get("image"):
            # link d'acquisto DENTRO il testo (copiabile), non come pulsante
            message = build_product_message(info, short_url=short_url, user_name=user.first_name,
                                            review=review, price_line=price_line)
            if review_link:
                message += f"\n\n🎥 <a href='{review_link}'>Guarda la video-recensione</a>"
            video_url = info.get("video") if video_enabled() else None
            if video_url:
                try:
                    await chat.send_video(video=video_url, caption=message, parse_mode=ParseMode.HTML)
                    sent = True
                except Exception as e:
                    logger.warning(f"send_video error: {e}")
            if not sent:
                photo = await get_post_photo(info, price=price_f, is_min=hist.get("is_new_min"))
                if photo:
                    try:
                        await chat.send_photo(photo=photo, caption=message, parse_mode=ParseMode.HTML)
                        sent = True
                    except Exception as e:
                        logger.warning(f"Photo error: {e}")
            if not sent:
                await chat.send_message(message, parse_mode=ParseMode.HTML)
                sent = True

        # --- SENZA immagine (es. AliExpress/Temu che bloccano) → anteprima Telegram del link ---
        else:
            title = info.get("title") or f"Offerta {info.get('source') or ''}".strip()
            parts = [f"🛍️ <b>{title}</b>"]
            if info.get("source"):
                parts.append(f"🏪 <b>Store:</b> {info['source']}")
            if price_line:
                parts.append(f"💰 <b>Prezzo:</b> {price_line}")
            if review:
                parts.append(f"\n📝 <i>{review}</i>")
            if review_link:
                parts.append(f"🎥 <a href='{review_link}'>Video-recensione</a>")
            parts.append(f"\n🛒 {short_url}")
            await chat.send_message(
                "\n".join(parts),
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(url=short_url, prefer_large_media=True, show_above_text=True),
            )
            sent = True

        # Pulizia (status + messaggio originale) SOLO dopo che il post è andato a buon fine
        if sent:
            record_recent_deal(info.get("title"), price_line, info.get("source"),
                               short_url, info.get("image"))
            try:
                await status_msg.delete()
            except Exception:
                pass
            try:
                await update.message.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ Errore. Riprova.")
        except Exception:
            pass


def main():
    start_health_check_server()
    init_firestore()
    load_persisted_content()  # ricarica offerte e articoli salvati (no perdita su restart)
    # concurrent_updates=True: più link inviati di fila vengono elaborati in parallelo
    # (il lavoro pesante immagini/AI gira già in thread separati)
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("aiuto", help_cmd))
    app.add_handler(CommandHandler("promo", promo_cmd))
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
    app.add_handler(CommandHandler("setreview", setreview_cmd))
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
    kb_labels = f"^({re.escape(BTN_CONFIG)}|{re.escape(BTN_PRODUCTS)}|{re.escape(BTN_CHANNEL)}|{re.escape(BTN_ADD)}|{re.escape(BTN_DEAL)}|{re.escape(BTN_TOKENS)}|{re.escape(BTN_TAG)}|{re.escape(BTN_MERCHANTS)}|{re.escape(BTN_CARD)}|{re.escape(BTN_PROMO)}|{re.escape(BTN_HELP)})$"
    app.add_handler(MessageHandler(filters.Regex(kb_labels) & ~filters.COMMAND, keyboard_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    if app.job_queue:
        app.job_queue.run_repeating(monitor_prices, interval=CHECK_INTERVAL_MIN * 60, first=60)
        logger.info("Monitor prezzi schedulato")
        if SCHEDULED_POST_HOURS > 0:
            app.job_queue.run_repeating(scheduled_post, interval=SCHEDULED_POST_HOURS * 3600, first=120)
            logger.info(f"Post programmati ogni {SCHEDULED_POST_HOURS}h")
        # Articolo/recensione tech generato ogni giorno (stile redazione)
        app.job_queue.run_repeating(generate_daily_article, interval=24 * 3600, first=180)
        logger.info("Redazione: articolo giornaliero schedulato")
        # Portale 'Gli Affari di Nello': batch contenuti rigenerato ogni 6h
        app.job_queue.run_repeating(generate_portal, interval=6 * 3600, first=30)
        logger.info("Portale: generazione contenuti schedulata")
    else:
        logger.warning("JobQueue non disponibile: installa python-telegram-bot[job-queue]")

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
