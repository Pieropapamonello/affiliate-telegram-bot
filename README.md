# 🔗 Multi-Store Affiliate Bot + Automazione — Telegram

Bot Telegram che trasforma il link di un prodotto di **qualsiasi negozio online** in un **link affiliato**, con automazione stile Doublegram/Afflow.

- **Amazon** → usa il tuo tag affiliato nativo (`AFFILIATE_TAG`) per le commissioni migliori, con scraping di titolo/prezzo/immagine/recensioni.
- **Altri store** → usa una **rete aggregatrice** (Sovrn/Skimlinks, Admitad, Awin…) tramite un *deeplink template* o il tuo Publisher ID Skimlinks.
- **Monitor sconti** → watchlist di prodotti, controllo prezzi periodico, alert automatico al calo.
- **Auto-post su canale** → pubblica offerte formattate su un canale Telegram.
- **Copy AI (Claude)** → testo del post generato automaticamente.
- **Accorciamento** opzionale tramite **YOURLS**.

## 🤖 Comandi

| Comando | Funzione |
|---|---|
| _invia un link_ | Restituisce il link affiliato |
| `/watch <link> [prezzo]` | Monitora il prodotto; avvisa al calo (o sotto il prezzo target) |
| `/list` | Mostra i prodotti monitorati |
| `/unwatch <numero>` | Rimuove un prodotto dalla watchlist |
| `/deal <link>` | Pubblica subito un'offerta (sul canale se configurato) |
| `/help` | Aiuto |

> ⚠️ La watchlist è salvata in `watchlist.json`. Su Render Free il disco è **effimero** (si azzera a ogni deploy/restart): per persistenza aggiungi un disco Render o imposta `DATA_DIR` su un volume.

## ⚙️ Come funziona l'affiliazione "qualsiasi store"

Non è possibile rendere affiliato un link arbitrario senza essere iscritti al programma del negozio. La soluzione è una **rete aggregatrice**: ti iscrivi **una volta** e con una sola credenziale copri migliaia di store già nella rete.

1. Iscriviti a **[Sovrn/Skimlinks](https://www.sovrn.com/)** oppure **[Admitad](https://www.admitad.com/)**.
2. Ottieni il **Publisher ID** (Skimlinks) o il **deeplink** della tua campagna.
3. Impostalo come variabile d'ambiente (vedi sotto). Il bot funziona da subito per Amazon anche senza aggregatore.

## 📝 Variabili d'ambiente

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Token del bot da [@BotFather](https://t.me/BotFather) |
| `AFFILIATE_TAG` | — | Tag affiliato Amazon (es. `tuotag-21`) |
| `SKIMLINKS_ID` | — | Publisher ID Sovrn/Skimlinks (opzione semplice) |
| `DEEPLINK_TEMPLATE` | — | Template con `{url}`, es. `https://go.skimresources.com/?id=XXXX&xs=1&url={url}` |
| `YOURLS_URL` | — | URL installazione YOURLS (per accorciare) |
| `YOURLS_SIGNATURE` | — | Signature API di YOURLS |
| `CHANNEL_ID` | — | Canale per auto-post (`@miocanale` o `-100123…`); il bot dev'essere admin |
| `CHECK_INTERVAL_MIN` | — | Ogni quanti minuti controllare i prezzi (default 60) |
| `DISCOUNT_THRESHOLD` | — | % di calo minimo per l'alert (default 10) |
| `ANTHROPIC_API_KEY` | — | Chiave API Claude per i testi AI dei post |
| `AI_MODEL` | — | Modello Claude (default `claude-opus-4-8`) |
| `DATA_DIR` | — | Cartella per `watchlist.json` (default `.`) |
| `PORT` | — | Porta health-check (Render la imposta da sé) |

> ⚠️ Non committare mai `.env` o token nel codice. Usa solo variabili d'ambiente.

## 🚀 Deploy su Render

1. Collega questo repo GitHub a Render → **New + → Web Service** (ambiente **Docker**).
2. Imposta le variabili d'ambiente sopra.
3. Deploy. Il bot gira in *polling*, l'health-check risponde sulla porta `PORT`.

## 🐳 Esecuzione locale

```bash
cp env-example .env   # compila i valori
pip install -r requirements.txt
python main.py
```

## 📱 Utilizzo

Invia al bot il link di un prodotto (Amazon o altro store). Ricevi un messaggio con info prodotto e il **link affiliato** pronto da condividere.
