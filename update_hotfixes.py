#!/usr/bin/env python3
"""
WoW Hotfix Tracker — Auto-updater
====================================
Každé 3h (přes GitHub Actions) kontroluje Blizzard stránky:

  1. Načte aktuální HF[0].url a porovná nadpis s uloženým HF[0].title
     → Pokud se liší, Blizzard přepsal článek (přidal nové datum) → re-parsuj celý článek

  2. Prohledá Blizzard news listing hledá nové hotfix/tuning články
     → Pokud URL není v naší databázi → parsuj a přidej na začátek

  Parsování textu → strukturovaná JS data zajišťuje Claude API.
"""

import os
import re
import sys
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

# ── Konfigurace ──────────────────────────────────────────────────────────────

HTML_FILE    = "index.html"
NEWS_URL     = "https://worldofwarcraft.blizzard.com/en-us/news"
MODEL        = "claude-sonnet-4-6"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex pro hotfix / class-tuning URL path na Blizzard
HOTFIX_URL_RE = re.compile(
    r"/(hotfixes|class-tuning|hotfix)-[a-z]+-\d+-\d{4}",
    re.IGNORECASE
)

client = Anthropic()   # čte ANTHROPIC_API_KEY z prostředí

# ── Čtení / zápis HTML ───────────────────────────────────────────────────────

def read_html() -> str:
    with open(HTML_FILE, encoding="utf-8") as f:
        return f.read()

def write_html(content: str) -> None:
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(content)

# ── Extrakce dat z HF pole v HTML ────────────────────────────────────────────

def get_latest_hf(html: str) -> dict | None:
    """Vrátí {title, url} pro HF[0] (první entry v poli)."""
    try:
        hf_start = html.index("const HF = [")
    except ValueError:
        return None

    after = html[hf_start:]
    title_m = re.search(r"title:'([^']+)'", after)
    url_m   = re.search(r"url:'([^']+)'",   after)

    if title_m and url_m:
        return {"title": title_m.group(1), "url": url_m.group(1)}
    return None

def get_all_known_urls(html: str) -> set[str]:
    """Vrátí všechny URL z HF pole."""
    try:
        hf_start = html.index("const HF = [")
    except ValueError:
        return set()
    return set(re.findall(r"url:'([^']+)'", html[hf_start:]))

# ── Scraping Blizzard stránek ─────────────────────────────────────────────────

def fetch_blizzard_page(url: str) -> str:
    """Stáhne stránku a vrátí HTML text."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text

def get_article_title(article_html: str) -> str | None:
    """Extrahuje nadpis z hotfix článku."""
    soup = BeautifulSoup(article_html, "html.parser")

    # Blizzard používá různé selektory v různých verzích webu
    for sel in [
        "h1.Blog-title", ".NewsBlog-title", ".Blog-title",
        "[data-testid='article-title']", "h1",
    ]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            # Odstraní " - World of Warcraft" suffix pokud je v <title>
            text = re.sub(r"\s*[-|]\s*(World of Warcraft|WoW).*$", "", text, flags=re.I)
            return text.strip()

    # Fallback: <title> tagu
    title_el = soup.find("title")
    if title_el:
        text = title_el.get_text(strip=True)
        text = re.sub(r"\s*[-|]\s*(World of Warcraft|WoW).*$", "", text, flags=re.I)
        return text.strip()

    return None

def get_article_body(article_html: str) -> str:
    """Extrahuje tělo článku pro Claude parsování."""
    soup = BeautifulSoup(article_html, "html.parser")

    for sel in [
        ".NewsBlog-content", ".Blog-content",
        ".BlogPost-body", "[data-testid='article-body']",
        "article", "main",
    ]:
        el = soup.select_one(sel)
        if el:
            # Přidá newline za každý blokový element pro čistý text
            for tag in el.find_all(["p", "li", "h2", "h3", "h4"]):
                tag.append("\n")
            return el.get_text(separator="\n", strip=True)

    # Fallback: celá stránka (ořezaná)
    return soup.get_text(separator="\n", strip=True)[:10000]

def find_new_hotfix_articles(known_urls: set[str]) -> list[str]:
    """
    Prohledá Blizzard news listing a vrátí URL článků,
    které ještě nejsou v naší databázi.
    """
    new_urls = []
    try:
        html = fetch_blizzard_page(NEWS_URL)
        soup = BeautifulSoup(html, "html.parser")

        found = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if HOTFIX_URL_RE.search(href):
                # Normalizuj na absolutní URL
                if href.startswith("/"):
                    href = "https://worldofwarcraft.blizzard.com" + href
                elif not href.startswith("http"):
                    continue
                # Odstraň query string / fragment
                href = href.split("?")[0].split("#")[0]
                found.add(href)

        for url in sorted(found):
            if url not in known_urls:
                new_urls.append(url)
                print(f"  → Nový článek: {url}")

    except Exception as e:
        print(f"  ⚠ News listing nedostupný ({e}) — přeskočeno")

    return new_urls

# ── Claude parsování ──────────────────────────────────────────────────────────

PARSE_PROMPT = """Jsi parser World of Warcraft hotfix článků.
Dostaneš URL a text článku. Vrať POUZE jeden JavaScript object literal (ne const, ne markdown bloky, jen samotný objekt).

URL článku: {url}

Text článku:
{text}

Struktura musí přesně odpovídat tomuto vzoru:
{{
  id: 'march-30-2026',
  title: 'Hotfixes: March 30, 2026',
  url: '{url}',
  dateISO: '2026-03-30',
  sections: {{
    'Classes': {{
      'Death Knight': [
        {{spec:'Frost', text:'All damage increased by 4%. Not applied to PvP.', spell:'All damage', ch:+4, ct:'pct'}},
        {{spec:'Unholy', text:'Fixed an issue with X.', fix:1}},
      ],
      'Warrior': [
        {{spec:'Arms', text:'Execute damage increased by 15%.', spell:'Execute', ch:+15, ct:'pct'}},
        {{spec:'Arms', text:'Cleave damage reduced by 10%.', spell:'Cleave', ch:-10, ct:'pct'}},
      ],
    }},
    'Dungeons and Raids': {{
      'The Voidspire': [
        {{text:'Fixed boss encounter issue.', fix:1}},
      ],
    }},
    'Items and Rewards': {{
      '': [
        {{text:'Fixed item drop rates.', fix:1}},
      ],
    }},
  }},
}}

PRAVIDLA:
- id: lowercase slug (např. 'march-30-2026', 'class-tuning-march-31-2026')
- title: přesně jak je v nadpisu článku
- dateISO: datum nadpisu článku ve formátu YYYY-MM-DD (ne sub-data uvnitř článku)
- ch: číslo (kladné = buff, záporné = nerf). ct:'pct' pro procenta, ct:'abs' pro absolutní
- spell: krátký název schopnosti/kouzla
- ca: volitelný vlastní popis (např. '+1s CDR', '-0.5s cast')
- fix:1 pro opravy bugů (bez ch/spell)
- Zahrň VŠECHNY změny ze VŠECH dat v článku (Blizzard přidává starší dny na konec)
- Přesné názvy tříd: Death Knight, Demon Hunter, Druid, Evoker, Hunter, Mage, Monk, Paladin, Priest, Rogue, Shaman, Warlock, Warrior
- Sekce: 'Classes', 'Dungeons and Raids', 'Items and Rewards', 'Quests', 'Player versus Player', 'Miscellaneous'
- V ne-Classes sekcích použij '' jako klíč pokud není sub-kategorie, jinak název dungeonu/raidu

Vrať POUZE object literal začínající {{ a končící }}"""

def parse_with_claude(article_url: str, article_text: str) -> str:
    """Pošle text článku Claudovi a vrátí JS object literal."""
    prompt = PARSE_PROMPT.format(
        url=article_url,
        text=article_text[:7500]  # limit kontextu
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Odstraň případné markdown code fences
    raw = re.sub(r"^```(?:javascript|js)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()

# ── Manipulace s HF polem v HTML ──────────────────────────────────────────────

def find_matching_brace(text: str, start: int) -> int:
    """
    Vrátí index ZA zavírající } odpovídající { na pozici start.
    Správně ignoruje { } uvnitř stringů a // line komentářů.
    """
    depth = 0
    i = start
    in_string = False
    string_char = None

    while i < len(text):
        c = text[i]

        if in_string:
            if c == "\\" and i + 1 < len(text):
                i += 2       # přeskoč escaped znak
                continue
            if c == string_char:
                in_string = False
        else:
            # Přeskoč // komentář do konce řádku
            if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if c in ('"', "'"):
                in_string = True
                string_char = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1   # vrátí pozici ZA }
        i += 1

    return -1  # nenalezeno

def replace_first_hf_entry(html: str, new_js: str) -> str:
    """Nahradí první objekt v HF poli novým JS objektem."""
    hf_marker = "const HF = ["
    hf_pos = html.index(hf_marker) + len(hf_marker)

    # Najdi první { za [
    first_brace = html.index("{", hf_pos)

    # Najdi odpovídající }
    end_pos = find_matching_brace(html, first_brace)
    if end_pos == -1:
        raise RuntimeError("Nelze najít konec prvního HF objektu")

    # Přeskoč trailing čárku a whitespace za }
    after = html[end_pos:]
    comma_m = re.match(r"\s*,", after)
    if comma_m:
        end_pos += comma_m.end()

    # Zachovej odsazení (bílé znaky před {)
    pre_brace = html[hf_pos:first_brace]   # newlines + spaces/tabs
    replacement = pre_brace + new_js.rstrip() + ","

    return html[:hf_pos] + replacement + html[end_pos:]

def prepend_hf_entry(html: str, new_js: str) -> str:
    """Přidá nový objekt na ZAČÁTEK HF pole (před stávající první entry)."""
    hf_marker = "const HF = ["
    hf_pos = html.index(hf_marker) + len(hf_marker)

    # Zjisti odsazení z prvního { v poli
    first_brace = html.index("{", hf_pos)
    indent = html[hf_pos:first_brace]   # obvykle '\n  '

    # Sestavení komentáře + nového objektu
    new_block = indent + new_js.rstrip() + "," + "\n"

    return html[:hf_pos] + new_block + html[hf_pos:]

# ── Hlavní logika ─────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 55)
    print("  WoW Hotfix Tracker — Auto-updater")
    print("=" * 55)

    html = read_html()
    latest = get_latest_hf(html)

    if not latest:
        print("CHYBA: Nelze načíst HF pole z HTML souboru.")
        sys.exit(1)

    print(f"\n📋 Aktuální HF[0]: '{latest['title']}'")
    print(f"   URL: {latest['url']}\n")

    updated = False

    # ── Krok 1: Zkontroluj, zda Blizzard přepsal nadpis stávajícího článku ──
    print("── Krok 1: Kontrola nadpisu posledního článku ──────────")
    try:
        article_html  = fetch_blizzard_page(latest["url"])
        current_title = get_article_title(article_html)

        if not current_title:
            print("  ⚠ Nepodařilo se extrahovat nadpis ze stránky.")
        elif current_title == latest["title"]:
            print(f"  ✓ Nadpis beze změny: '{current_title}'")
        else:
            print(f"  🔄 Změna nadpisu!")
            print(f"     Bylo:  '{latest['title']}'")
            print(f"     Nyní:  '{current_title}'")

            body   = get_article_body(article_html)
            new_js = parse_with_claude(latest["url"], body)
            print(f"  🤖 Claude naparsoval objekt ({len(new_js)} znaků)")

            html    = replace_first_hf_entry(html, new_js)
            updated = True
            print("  ✅ HF[0] aktualizován.")

    except requests.RequestException as e:
        print(f"  ⚠ Blizzard stránka nedostupná: {e}")
    except Exception as e:
        print(f"  ⚠ Neočekávaná chyba při kontrole nadpisu: {e}")

    # ── Krok 2: Hledej nové hotfix články ────────────────────────────────────
    print("\n── Krok 2: Hledám nové články na Blizzard news ─────────")
    known_urls = get_all_known_urls(html)
    new_urls   = find_new_hotfix_articles(known_urls)

    if not new_urls:
        print("  ✓ Žádné nové články.")

    for url in new_urls:
        try:
            print(f"\n  🆕 Zpracovávám: {url}")
            article_html = fetch_blizzard_page(url)
            body         = get_article_body(article_html)
            new_js       = parse_with_claude(url, body)
            print(f"  🤖 Claude naparsoval objekt ({len(new_js)} znaků)")

            html    = prepend_hf_entry(html, new_js)
            updated = True
            print("  ✅ Přidán na začátek HF pole.")

        except requests.RequestException as e:
            print(f"  ⚠ Nelze stáhnout {url}: {e}")
        except Exception as e:
            print(f"  ⚠ Chyba při zpracování {url}: {e}")

    # ── Uložení ───────────────────────────────────────────────────────────────
    print("\n── Výsledek ─────────────────────────────────────────────")
    if updated:
        write_html(html)
        print("💾 index.html uložen. GitHub Actions pushne změny.")
    else:
        print("✓ Žádné změny — index.html nebyl upraven.")

    print("=" * 55)

if __name__ == "__main__":
    main()
