"""Motore AI del bot/portale: provider unico Groq (testi, chat, parsing JSON)."""
import os
import re
import json
import logging

import httpx

logger = logging.getLogger("ai")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


async def ai_complete(system: str, user: str, max_tokens: int = 400) -> str:
    """Completamento async (usato dalle parti async del bot)."""
    if not GROQ_API_KEY:
        return None
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
            r = await client.post(GROQ_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload)
            data = r.json()
            if "choices" not in data:
                logger.error(f"Groq response: {str(data)[:300]}")
                return None
            return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None


def groq_sync(system, user, max_tokens=500, model=None) -> str:
    """Completamento sincrono (usato dall'HTTP server della chat /ask)."""
    if not GROQ_API_KEY:
        return None
    try:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": model or GROQ_MODEL, "max_tokens": max_tokens, "temperature": 0.8,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=30.0,
        )
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.warning(f"groq sync: {e}")
        return None


def extract_json_array(text):
    """Estrae un array JSON da una risposta AI (robusto a testo extra)."""
    if not text:
        return None
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            return v.get("articles") or v.get("items")
    except Exception:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def ai_status() -> str:
    return f"Groq ({GROQ_MODEL})" if GROQ_API_KEY else "disattivo (template)"
