"""
categorize_import.py — macht aus einer DKB-Umsatzliste eine Vorschlagsdatei.

Ausgabe ist eine CSV in der Spaltenstruktur von money.csv, mit vorgeschlagenen
Kat/UKat/Bem: suggestions/<auszug>.suggestion.csv. Die korrigierst du von Hand
(am besten in PyCharm, das die Datei unangetastet lässt); import_dkb.py trägt
dann genau diese Datei in money.csv ein. Hier wird NICHTS in money.csv geschrieben.

Drei Spalten bleiben leer, weil sie erst beim Eintragen entstehen: MF (Ist/F),
MT (laufende Nummer) und Saldo. Hinten hängen zwei zusätzliche Spalten,
Konfidenz und Quelle -- sie sagen dir, welcher Vorschlag auf 133 gleichen
Buchungen beruht und welcher auf geratener Tokenüberlappung. import_dkb.py
ignoriert sie; mit --plain bleiben sie weg.

Die Stufen kommen aus rules.json (siehe build_rules.py) und werden in der
dort festgelegten Reihenfolge abgefragt: paypal_submerchant, zweck, empf.
Danach folgt als letztes Netz eine unscharfe Stufe, die NICHT aus rules.json
kommt, sondern direkt gegen money.csv läuft (Tokenüberlappung über
Empfänger + Verwendungszweck, gleicher Lookback-Zeitraum wie
build_rules.select_recent). Sie liefert nur "unscharf" -- explizit zum
Gegenlesen gedacht. Grund für den Sonderweg: ein kompaktes rules.json ist
schneller zu lesen, zu diffen und zu versionieren als eine Kopie eines
Gutteils der Historie.

Zwei Nutzungsarten:

  * Als CLI (der Normalfall): siehe oben.
  * Als Bibliothek: make_categorizer(rules_path, money_path).suggest(booking).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # als Paketmodul: python -m finance.categorize_import
    from . import build_rules
    from . import import_dkb as money_io
except ImportError:  # direkt als Skript aus dem Paketordner gestartet
    import build_rules
    import import_dkb as money_io

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

FUZZY_TOKEN_RE = re.compile(r"[A-ZÄÖÜ0-9]{4,}")
FUZZY_MIN_SCORE = 3  # unterhalb dieser Tokenüberlappung: kein Vorschlag

REVIEW_COLUMNS = ["Konfidenz", "Quelle"]
SUGGESTION_COLUMNS = money_io.MONEY_COLUMNS + REVIEW_COLUMNS


# ---------------------------------------------------------------------------
# Vorschlag
# ---------------------------------------------------------------------------

@dataclass
class Suggestion:
    kat: str
    ukat: str
    bem: str
    confidence: str  # "hoch" | "mittel" | "niedrig" | "unscharf" | "-"
    source: str  # menschenlesbare Herkunftsangabe, zum Gegenlesen


def _tokens(s: str) -> set[str]:
    s = (s or "").upper()
    for ch in (",", ".", "/"):
        s = s.replace(ch, " ")
    return set(FUZZY_TOKEN_RE.findall(s))


class Categorizer:
    """Fragt die Stufen aus rules.json in ihrer Reihenfolge ab, danach die
    unscharfe Stufe. `fuzzy_pool` ist optional -- ohne money.csv gibt es nur
    die Regelstufen (schnell, aber ohne Auffangnetz für Neues)."""

    def __init__(self, rules: dict, fuzzy_pool: Optional[list[dict]] = None):
        if "tiers" not in rules:
            raise ValueError(
                "rules.json hat kein 'tiers' -- vermutlich eine alte Version. "
                "Bitte build_rules.py neu laufen lassen."
            )
        sample = next((e for t in rules["tiers"].values() for e in t.values()), None)
        if sample is not None and not isinstance(sample.get("kat"), dict):
            raise ValueError(
                "rules.json ist im alten Format (ein Label je Regel statt je Feld "
                "einem p). Bitte build_rules.py neu laufen lassen."
            )
        self.tiers = [
            (build_rules.TIERS_BY_NAME[name], rules["tiers"].get(name, {}))
            for name in rules.get("tier_order", list(rules["tiers"]))
        ]
        self._fuzzy_pool = []
        for r in (fuzzy_pool or []):
            b = build_rules.Booking.from_money_row(r)
            self._fuzzy_pool.append({
                "tok": _tokens(b.empf + " " + b.text),
                "kat": r[money_io.MONEY_KAT_COL],
                "ukat": r[money_io.MONEY_UKAT_COL],
                "bem": r[money_io.MONEY_BEM_COL],
                "empf": b.empf,
                "text": b.text,
            })

    def best_rule(self, booking: build_rules.Booking):
        """Die erste Stufe in Abfragereihenfolge, deren Regel die Schwelle
        besteht -- nicht die erste, die überhaupt trifft, und auch nicht die
        mit dem höchsten p. Liefert (Stufenname, Schlüssel, Regel) oder None.

        Die Schwelle muss in die Auswahl hinein, nicht erst hinter sie: sonst
        verdeckt eine dünn belegte Regel der feinen Stufe eine dicht belegte
        der groben, und das Feld bleibt leer, obwohl die Historie eindeutig
        ist (1/1 aus "zweck" vor 133/135 aus "empf").

        Nach dem höchsten p auszuwählen wäre der naheliegende nächste
        Schritt und ist gemessen schlechter: 84,8 % gegen 86,3 % auf der Kat,
        und von den Vorschlägen, in denen sich beide unterschieden, wurde
        keiner dadurch richtig, aber acht falsch (`python -m finance.evaluate`,
        Tabelle 4). p kennt nur Häufigkeiten. Dass eine Zweck-Signatur den
        Sachverhalt schärfer fasst als der Empfänger allein, steht in keinem
        Zähler -- das weiß nur die Stufenreihenfolge."""
        fallback = None
        for tier, entries in self.tiers:
            key = tier.key(booking)
            if not key:
                continue
            rule = entries.get(key)
            if rule is None:
                continue
            if rule["kat"]["p"] >= build_rules.P_MIN_SUGGEST:
                return tier.name, key, rule
            if fallback is None:  # nur noch für die Begründung in der Quelle
                fallback = (tier.name, key, rule)
        return fallback

    def suggest(self, booking: build_rules.Booking) -> Suggestion:
        best = self.best_rule(booking)
        if best is not None:
            tier_name, key, rule = best
            fields = [rule[name] for name in ("kat", "ukat", "bem")]
            detail = (
                f"{tier_name}:{key} ({rule['total']} Beleg"
                f"{'' if rule['total'] == 1 else 'e'}, "
                + " / ".join(f"{label} {f['p']:.2f}"
                             for label, f in zip(("Kat", "UKat", "Bem"), fields))
                + ")"
            )
            # p ist präfixweise monoton fallend (Kat >= Kat+UKat >= Tripel),
            # die Schwelle schneidet also von hinten ab: Kat ohne Bem kommt
            # vor, Bem ohne Kat nicht.
            kat, ukat, bem = (f["value"] if f["p"] >= build_rules.P_MIN_SUGGEST else ""
                              for f in fields)
            if kat:
                return Suggestion(kat, ukat, bem,
                                  build_rules.confidence_label(fields[0]["p"]), detail)
            # Zu dünn zum Vorschlagen, aber nicht zu dünn zum Erwähnen: der
            # Wert steht in der Quelle, nur eben nicht in der Kat-Spalte.
            #
            # Hier NICHT auf die unscharfe Stufe durchfallen. Gemessen auf den
            # 121 unterdrückten Testbuchungen trifft sie genau dieselben
            # 56,2 % und schweigt zusätzlich 37 mal -- kein Wunder, ihre
            # Tokenüberlappung findet als nächsten Nachbarn meist denselben
            # Empfänger, über den die Regel schon dünn belegt ist.
            return Suggestion("", "", "", "-", f"zu dünn: {detail} -> {fields[0]['value']}")

        if self._fuzzy_pool:
            search_tok = _tokens(booking.empf + " " + booking.text)
            best, best_score = None, 0
            for cand in self._fuzzy_pool:
                score = len(search_tok & cand["tok"])
                if score > best_score:
                    best, best_score = cand, score
            if best is not None and best_score >= FUZZY_MIN_SCORE:
                # Eigenes Label, nicht "niedrig": die Regelstufen treffen
                # dort noch 90,7 %, die unscharfe Stufe nur 67,7 %. Ein Wort
                # für beides würde die Spalte wieder unbrauchbar machen --
                # genau das, was die Kalibrierung abgeschafft hat.
                return Suggestion(
                    best["kat"], best["ukat"], best["bem"], "unscharf",
                    f"fuzzy(score={best_score}):{best['empf']} / {best['text'][:40]}",
                )

        return Suggestion("", "", "", "-", "kein Treffer")


def make_categorizer(
        rules_path: Path,
        money_path: Optional[Path] = None,
        lookback_months: int = build_rules.DEFAULT_LOOKBACK_MONTHS,
) -> Categorizer:
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    fuzzy_pool = None
    if money_path is not None:
        money_rows = money_io.read_money_csv(money_path)
        fuzzy_pool = build_rules.select_recent(money_rows, lookback_months)
    return Categorizer(rules, fuzzy_pool)


# ---------------------------------------------------------------------------
# Vorschlagsdatei schreiben
# ---------------------------------------------------------------------------

def build_suggestion_rows(dkb_rows: list[dict], categorizer: Categorizer) -> list[dict]:
    """Gebuchte DKB-Zeilen -> Zeilen in money.csv-Struktur (als Strings, genau
    so, wie sie in die Datei geschrieben werden) plus Konfidenz/Quelle."""
    out = []
    for r in dkb_rows:
        booking = build_rules.Booking.from_dkb_row(r)
        s = categorizer.suggest(booking)
        row = {col: "" for col in SUGGESTION_COLUMNS}
        row[money_io.MONEY_DATE_COL] = money_io.format_de_date(booking.date)
        row[money_io.MONEY_YEAR_COL] = str(booking.date.year)
        row[money_io.MONEY_MONTH_COL] = str(booking.date.month)
        row[money_io.MONEY_KAT_COL] = s.kat
        row[money_io.MONEY_UKAT_COL] = s.ukat
        row[money_io.MONEY_BEM_COL] = s.bem
        row[money_io.MONEY_AMOUNT_COL] = money_io.format_de_amount(booking.amount)
        row[money_io.MONEY_EMPF_COL] = booking.empf
        row[money_io.MONEY_TEXT_COL] = booking.text
        row["Konfidenz"] = s.confidence
        row["Quelle"] = s.source
        out.append(row)
    return out


def write_suggestion_csv(path: Path, rows: list[dict], plain: bool = False) -> None:
    columns = money_io.MONEY_COLUMNS if plain else SUGGESTION_COLUMNS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns,
                                delimiter=money_io.SUGGESTION_DELIMITER,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def suggestion_path_for(statement: Path, folder: Path) -> Path:
    return folder / (statement.name.removesuffix(".csv") + money_io.SUGGESTION_SUFFIX)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    money_io.add_test_flag(ap)
    ap.add_argument("statement", nargs="?",
                    help="Dateiname im umsatzlisten-Ordner (Default: neueste Datei dort)")
    ap.add_argument("--iban", default="DE84120300001089317836",
                    help="nur für die Suche nach der neuesten Umsatzliste")
    ap.add_argument("--rules", default="rules.json", help="Dateiname im rules-Ordner")
    ap.add_argument("--money", default="money.csv",
                    help="Dateiname im money-Ordner, für die unscharfe Stufe")
    ap.add_argument("--no-fuzzy", action="store_true",
                    help="nur Regelstufen aus rules.json, keine unscharfe Stufe")
    ap.add_argument("-o", "--out",
                    help="Zieldateiname im suggestions-Ordner "
                         "(Default: <umsatzliste>.suggestion.csv)")
    ap.add_argument("--plain", action="store_true",
                    help="ohne die Spalten Konfidenz und Quelle")
    args = ap.parse_args()

    fld = money_io.folders(args.test)
    if args.statement:
        statement_path = money_io.resolve_in(fld.umsatzlisten, args.statement, ".csv")
    else:
        statement_path = money_io.find_latest_dkb_file(fld.umsatzlisten, args.iban)
        print(f"Gefundene Umsatzliste: {statement_path.name}")

    rules_path = money_io.resolve_in(fld.rules, args.rules, ".json")
    money_path = None if args.no_fuzzy else money_io.resolve_in(fld.money, args.money, ".csv")
    categorizer = make_categorizer(rules_path, money_path)
    print(f"Regeln: {rules_path.name}"
          f"{'' if money_path is None else f' + unscharfe Stufe gegen {money_path.name}'}")

    all_rows = money_io.read_dkb_csv(statement_path)
    booked = [r for r in all_rows if r[money_io.DKB_STATUS_COL] == money_io.DKB_STATUS_BOOKED]
    n_skipped = len(all_rows) - len(booked)
    suggestions = build_suggestion_rows(booked, categorizer)

    out_path = (money_io.resolve_in(fld.suggestions, args.out, ".csv") if args.out
                else suggestion_path_for(statement_path, fld.suggestions))
    write_suggestion_csv(out_path, suggestions, plain=args.plain)

    counts = Counter(r["Konfidenz"] for r in suggestions)
    order = ["hoch", "mittel", "niedrig", "unscharf", "-"]
    stat = ", ".join(f"{k}: {counts[k]}" for k in order if counts[k])
    print(f"{len(suggestions)} gebuchte Buchungen ({stat})"
          + (f", {n_skipped} nicht gebuchte übersprungen" if n_skipped else ""))
    print(f"Geschrieben nach {out_path}")
    print("Jetzt Kat/UKat/Bem korrigieren (am besten in PyCharm), dann import_dkb.py.")


if __name__ == "__main__":
    main()
