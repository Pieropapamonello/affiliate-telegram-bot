"""Mini-test delle funzioni delicate. Esegui:  TELEGRAM_TOKEN=dummy python tests.py"""
import os
os.environ.setdefault("TELEGRAM_TOKEN", "dummy")

import io
from PIL import Image

import ai
import main


def check(name, cond):
    print(("OK  " if cond else "FAIL") + "  " + name)
    assert cond, name


# --- prezzo Amazon ---
check("prezzo 899,00€", main.parse_price_to_float("899,00€") == 899.0)
check("prezzo 1.299,00€", main.parse_price_to_float("1.299,00€") == 1299.0)
check("prezzo $19.99", main.parse_price_to_float("$19.99") == 19.99)
check("prezzo vuoto", main.parse_price_to_float("") is None)

# --- estrazione JSON (risposte AI) ---
check("json array pulito", ai.extract_json_array('[{"a":1}]') == [{"a": 1}])
check("json con testo extra", ai.extract_json_array('ecco: [1,2,3] fine') == [1, 2, 3])
check("json dict con items", ai.extract_json_array('{"items":[1]}') == [1])
check("json invalido", ai.extract_json_array("non json") is None)

# --- link Amazon affiliato ---
link = main._amazon_search_link("smartphone")
check("amazon search ha k=", "k=smartphone" in link)

# --- generazione card (smoke) ---
b = io.BytesIO()
Image.new("RGB", (300, 300), (255, 255, 255)).save(b, "PNG")
card = main._compose_card(b.getvalue(), "Amazon", "Gli Affari di Nello", price=19.99, title="Test")
check("card e' un JPEG non vuoto", isinstance(card, (bytes, bytearray)) and len(card) > 1000)

# --- copertina articolo (smoke) ---
cover = main._compose_article_cover("Titolo di prova", "smartphone")
check("copertina e' un PNG non vuoto", len(cover) > 1000)

print("\nTutti i test passati.")
