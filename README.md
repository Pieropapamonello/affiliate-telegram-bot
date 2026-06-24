# Gli Affari di Nello — Bot + Portale

Bot Telegram che trasforma un link di **qualsiasi store** in un **link affiliato**, fa da
**redazione automatica** (articoli, schede tecniche, video) e alimenta un **portale web 3D**.

## Architettura (lineare)

```
Telegram @nellinobuybot  ──►  main.py (bot + server HTTP)  ──►  Firestore (persistenza)
        (utenti)                     │
                                     ├─ Groq        → articoli, schede, recensioni, chat
                                     ├─ YouTube API → video
                                     └─ endpoint JSON pubblici
                                            │
                          Portale 3D (tech-news-ai/) legge gli endpoint
```

- **Un solo motore AI**: Groq (`GROQ_API_KEY`).
- **Un solo magazzino**: Firestore (offerte, articoli, portale, watchlist). Fallback su file JSON locale se Firestore non è configurato.
- **Un solo sito**: il portale 3D in `tech-news-ai/` (servito come Static Site Render).

## Cosa fa il bot

| Funzione | Dettaglio |
|---|---|
| Link → affiliato | Amazon tag nativo (+Bitly), resto via `DEEPLINK_TEMPLATE` |
| Card prodotto | Immagine scontornata + prezzo + brand (Pillow) |
| Offerte live | Ogni link inviato finisce in `/deals.json` (storico 60) |
| Redazione | `/portal.json` (14 articoli + 6 schede + 4 video), `/article?id=` (articolo lungo on-demand), `/ask?q=` (chat) |
| Watchlist | `/watch /list /unwatch`, monitor prezzi + auto-post su canale |

## Endpoint HTTP (serviti dal bot)

`/deals.json` · `/portal.json` · `/article?id=<id>` · `/ask?q=<testo>` ·
`/img/article/<id>.png[?plain=1]` · `/healthz`

## Avvio

```bash
cp env-example .env   # compila i valori (vedi env-example)
pip install -r requirements.txt
python main.py
```

Deploy: **Docker** su Render (il `Dockerfile` installa i font e scarica il modello di scontorno).
Variabili principali: vedi `env-example`. Le uniche obbligatorie sono `TELEGRAM_TOKEN`
(+ `GROQ_API_KEY` e Firestore per il portale).

> ⚠️ Non committare mai `.env` o chiavi. Usa solo le Environment Variables di Render.
