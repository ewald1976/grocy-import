#!/usr/bin/env python3
# grocy_import.py — Version 1.5
#
# Features:
#   ✅ INI-Konfiguration (grocy_url, api_key, csv_path, limit, debug, import_to_grocy)
#   ✅ Duplikatprüfung gegen Grocy
#   ✅ Strikte Trennung: CSV-only vs. CSV + Import
#   ✅ Statistik pro Kategorie
#   ✅ Zufällige Seitenwahl, sort_by=random
#   ✅ Unterbegriffe pro Kategorie (--random-subcats)
#   ✅ Reproduzierbarkeit via --seed
#   ✅ Nur Abhängigkeit: requests

import argparse, csv, sys, time, re, requests, configparser, os, random
from typing import Dict, List, Optional, Set
from collections import defaultdict

OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

CATEGORIES = [
    "Getränke", "Tiefkühlprodukte", "Backzutaten", "Grundnahrungsmittel",
    "Pasta und Reis", "Konserven", "Obst", "Gemüse", "Internationale Küche",
    "Hygiene", "Drogerie", "Putzmittel", "Haushaltswaren"
]

SUBKEYWORDS = {
    "Getränke": ["Wasser", "Saft", "Limonade", "Bier", "Wein", "Kaffee", "Tee"],
    "Tiefkühlprodukte": ["Pizza", "Gemüse", "Fisch", "Eis", "Kräuter"],
    "Backzutaten": ["Mehl", "Zucker", "Backpulver", "Hefe", "Vanille"],
    "Grundnahrungsmittel": ["Reis", "Öl", "Nudeln", "Kartoffeln", "Butter"],
    "Pasta und Reis": ["Spaghetti", "Penne", "Basmati", "Couscous"],
    "Konserven": ["Mais", "Bohnen", "Erbsen", "Tomaten", "Thunfisch"],
    "Obst": ["Apfel", "Banane", "Birne", "Traube", "Orange"],
    "Gemüse": ["Tomate", "Gurke", "Paprika", "Karotte", "Zwiebel"],
    "Internationale Küche": ["Sojasauce", "Curry", "Pesto", "Kokosmilch"],
    "Hygiene": ["Shampoo", "Zahnpasta", "Duschgel", "Deo", "Seife"],
    "Drogerie": ["Wattepads", "Creme", "Lotion", "Make-Up", "Rasiergel"],
    "Putzmittel": ["Spülmittel", "Reiniger", "Waschmittel", "Weichspüler"],
    "Haushaltswaren": ["Müllbeutel", "Küchenrolle", "Toilettenpapier"]
}

# ------------------------------------------------------------
# Hilfsfunktionen
# ------------------------------------------------------------

def valid_barcode(code: Optional[str]) -> bool:
    return bool(code and re.fullmatch(r"\d{8}|\d{12,14}", code.strip()))

def off_query(params: Dict, page_size: int = 50) -> List[Dict]:
    p = {"search_simple": 1, "action": "process", "json": 1, "page_size": page_size}
    p.update(params)
    r = requests.get(OFF_SEARCH_URL, params=p, timeout=25)
    r.raise_for_status()
    return r.json().get("products", [])

def fetch_category(cat: str, limit: int = 50, debug: bool = False, random_subcats=False) -> List[Dict]:
    # Wähle zufälligen Unterbegriff
    subterm = None
    if random_subcats and cat in SUBKEYWORDS:
        subterm = random.choice(SUBKEYWORDS[cat])
        search_term = subterm
    else:
        search_term = cat

    page = random.randint(1, 10)
    print(f"[INFO] Searching category: {cat} (Term: '{search_term}', Page: {page})")
    prods = []
    try:
        items = off_query({
            "search_terms": search_term,
            "countries": "Germany",
            "lc": "de",
            "sort_by": "random",
            "page": page
        }, page_size=limit)
        if debug:
            for p in items:
                print(f"   -> Raw: {p.get('product_name','<no name>')} "
                      f"EAN={p.get('code','')} Store={p.get('stores','')}")
        prods.extend(items)
    except Exception as e:
        print(f"[WARN] Error fetching {cat}: {e}")
    return prods

def normalize_name(p: Dict) -> str:
    n = p.get("product_name_de") or p.get("product_name") or ""
    n = n.strip()
    if not n:
        tags = p.get("categories_tags", [])
        if tags:
            n = tags[0].split(":")[-1].replace("-", " ").title()
    return n or "Unbenanntes Produkt"

def product_to_row(p: Dict, category=None) -> Optional[Dict]:
    code = p.get("code")
    if not valid_barcode(code):
        return None
    return {
        "name": normalize_name(p),
        "barcode": code.strip(),
        "brand": (p.get("brands") or "").split(",")[0].strip(),
        "store": (p.get("stores") or "").split(",")[0].strip(),
        "quantity": p.get("quantity") or "",
        "cat": category or "Unbekannt"
    }

def dedupe(rows: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for r in rows:
        bc = r.get("barcode")
        if bc and bc not in seen:
            seen.add(bc)
            out.append(r)
    return out

def parse_bool(s: str, default=False) -> bool:
    if s is None: return default
    return str(s).strip().lower() in ("1","true","yes","y","on")

# ------------------------------------------------------------
# Grocy API
# ------------------------------------------------------------

class GrocyAPI:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key
        self.session = requests.Session()
        self.session.headers.update({
            "GROCY-API-KEY": key,
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    def _post(self, path, payload):
        r = self.session.post(f"{self.url}{path}", json=payload, timeout=25)
        r.raise_for_status()
        return r.json()

    def _get(self, path):
        r = self.session.get(f"{self.url}{path}", timeout=25)
        r.raise_for_status()
        return r.json()

    def ensure_unit(self, name="Stück"):
        for u in self._get("/api/objects/quantity_units"):
            if u["name"] == name:
                return int(u["id"])
        r = self._post("/api/objects/quantity_units", {"name": name, "name_plural": name})
        return int(r["created_object_id"])

    def ensure_location(self, name="Vorrat"):
        for l in self._get("/api/objects/locations"):
            if l["name"] == name:
                return int(l["id"])
        r = self._post("/api/objects/locations", {"name": name})
        return int(r["created_object_id"])

    def fetch_existing_barcodes(self) -> Set[str]:
        data = self._get("/api/objects/product_barcodes")
        return { str(x.get("barcode","")).strip() for x in (data or []) if str(x.get("barcode","")).strip() }

    def create_product(self, row, qu_id, loc_id) -> Optional[int]:
        payload = {
            "name": row["name"],
            "description": f"{row['brand']} {row['quantity']}".strip(),
            "qu_id_stock": qu_id,
            "qu_id_purchase": qu_id,
            "location_id": loc_id,
            "min_stock_amount": 0
        }
        try:
            pid = int(self._post("/api/objects/products", payload)["created_object_id"])
            self._post("/api/objects/product_barcodes", {"product_id": pid, "barcode": row["barcode"]})
            return pid
        except requests.HTTPError:
            return None

# ------------------------------------------------------------
# Hauptlogik
# ------------------------------------------------------------

def write_example_ini(path: str):
    cfg = """[grocy]
grocy_url = https://grocy.myppl.mywire.org
api_key = REPLACE_WITH_YOUR_API_KEY
csv_path = products_sync.csv
limit = 200
debug = false
import_to_grocy = false
random_subcategories = false
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(cfg)
    print(f"[OK] Beispiel-INI geschrieben: {path}")

def main():
    parser = argparse.ArgumentParser(
        description="Importiert Produkte aus OpenFoodFacts (DE) in CSV oder Grocy mit Randomisierung.",
        epilog="Beispiel: python3 grocy_import.py --no-import --random-subcats -d",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--config", type=str, default="grocy_import.ini")
    parser.add_argument("--init-config", action="store_true", help="Beispiel-INI erzeugen")
    parser.add_argument("--csv", type=str, help="CSV-Dateipfad (überschreibt INI)")
    parser.add_argument("--limit", type=int, help="Anzahl Produkte (überschreibt INI)")
    parser.add_argument("--grocy-url", type=str, help="Grocy-URL (überschreibt INI)")
    parser.add_argument("--api-key", type=str, help="Grocy-API-Key (überschreibt INI)")
    parser.add_argument("--debug", "-d", action="store_true")
    parser.add_argument("--import", dest="do_import", action="store_true", help="CSV + Grocy-Import durchführen")
    parser.add_argument("--no-import", action="store_true", help="Nur CSV")
    parser.add_argument("--seed", type=int, help="Fixierter Seed für reproduzierbare Zufallswerte")
    parser.add_argument("--random-subcats", action="store_true", help="Aktiviere zufällige Unterbegriffe pro Kategorie")
    args = parser.parse_args()

    if args.init_config:
        write_example_ini(args.config)
        return

    if not os.path.exists(args.config):
        print(f"[ERROR] Keine INI gefunden ({args.config}). Mit --init-config erzeugen.")
        sys.exit(2)

    cfg = configparser.ConfigParser()
    cfg.read(args.config, encoding="utf-8")

    grocy_url = args.grocy_url or cfg["grocy"].get("grocy_url", "").strip()
    api_key   = args.api_key   or cfg["grocy"].get("api_key", "").strip()
    csv_path  = args.csv       or cfg["grocy"].get("csv_path", "products_sync.csv").strip()
    limit     = args.limit     or cfg["grocy"].getint("limit", 200)
    debug     = args.debug or parse_bool(cfg["grocy"].get("debug", "false"))
    ini_import = parse_bool(cfg["grocy"].get("import_to_grocy", "false"))
    random_subcats = args.random_subcats or parse_bool(cfg["grocy"].get("random_subcategories", "false"))

    do_import = args.do_import or (ini_import and not args.no_import)

    if args.seed is not None:
        random.seed(args.seed)
        print(f"[INFO] Random seed gesetzt: {args.seed}")

    print(f"[MODE] {'CSV + Import' if do_import else 'CSV-only (no import)'}")
    if random_subcats:
        print("[INFO] Zufällige Unterbegriffe aktiv")

    if not grocy_url or not api_key:
        print("[ERROR] grocy_url oder api_key fehlen.")
        sys.exit(2)

    # Verbinden + Barcodes
    print("[INFO] Lade bestehende Barcodes aus Grocy …")
    api = GrocyAPI(grocy_url, api_key)
    existing = api.fetch_existing_barcodes()
    print(f"[OK] {len(existing)} vorhandene Barcodes geladen.")

    stats_total = {"new":0,"skip":0,"imported":0}
    stats_cat = defaultdict(lambda: {"new":0,"skip":0,"imported":0})
    all_rows = []
    per_cat = max(10, limit // len(CATEGORIES))

    for cat in CATEGORIES:
        products = fetch_category(cat, limit=per_cat, debug=debug, random_subcats=random_subcats)
        for p in products:
            r = product_to_row(p, cat)
            if not r:
                stats_cat[cat]["skip"] += 1
                continue
            if r["barcode"] in existing:
                if debug:
                    print(f"   ⚠️  already in Grocy → skipped [{r['barcode']}] {r['name']}")
                stats_cat[cat]["skip"] += 1
                stats_total["skip"] += 1
                continue
            if debug:
                print(f"   ✅ new {r['name']} [{r['barcode']}]")
            all_rows.append(r)
            stats_cat[cat]["new"] += 1
            stats_total["new"] += 1
        time.sleep(0.25)

    all_rows = dedupe(all_rows)[:limit]
    print(f"[OK] CSV schreiben → {csv_path} ({len(all_rows)} neue Produkte)")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
    	w = csv.DictWriter(f, fieldnames=["name","barcode","brand","store","quantity","cat"])
        
    	w.writeheader(); w.writerows(all_rows)

    if do_import and all_rows:
        print("[INFO] Importiere neue Produkte nach Grocy …")
        qu = api.ensure_unit("Stück")
        loc = api.ensure_location("Vorrat")
        for r in all_rows:
            if api.create_product(r, qu, loc):
                stats_total["imported"] += 1
                stats_cat[r["cat"]]["imported"] += 1
            time.sleep(0.1)

    # Statistik
    print("\n===== Import Summary =====")
    print(f"Neue Produkte: {stats_total['new']}")
    print(f"Übersprungen (bereits vorhanden): {stats_total['skip']}")
    print(f"Importiert nach Grocy: {stats_total['imported']}")
    print("---------------------------")
    for cat, vals in stats_cat.items():
        if any(vals.values()):
            print(f"{cat}: neu {vals['new']}, skip {vals['skip']}, importiert {vals['imported']}")
    print("===========================")
    print("[DONE] Operation complete.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

