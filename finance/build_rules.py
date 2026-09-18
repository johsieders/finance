"""
build_rules.py — leitet Kategorisierungs-Regeln aus der Historie in money.csv ab.

rules.json ist ein REIN ABGELEITETER CACHE, keine eigenständig gepflegte
Datei: money.csv ist die Quelle der Wahrheit (deine Korrekturen dort *sind*
das Training), rules.json wird bei Bedarf komplett neu berechnet. Kein
inkrementelles Lernen, kein Drift, keine zweite Wahrheit.

Regeln stehen in STUFEN ("tiers"), von präzise nach grob abgefragt. Jede
Stufe ist nichts als eine Schlüsselfunktion über eine Buchung plus eine
Tabelle "Schlüssel -> häufigste (Kat, UKat, Bem)-Kombination":

  "paypal_submerchant" -- bei PayPal-Zahlungen steht der eigentliche Händler
                          nicht im Empfänger (der ist immer "PayPal Europe
                          ..."), sondern im Verwendungszweck
                          (".../PP.<ref>.PP/. <Händler>, Ihr Einkauf...").
  "zweck"              -- Empfänger + normalisierter Verwendungszweck. Das
                          ist die Stufe, die gleiche Empfänger mit mehreren
                          Sachverhalten trennt: die Gemeinde Tuntenhausen
                          bucht Grundsteuer, Wasser, Abwasser und Abfall für
                          zwei Häuser über ein und denselben Empfänger und
                          ein und dasselbe Mandat -- unterscheidbar allein am
                          Verwendungszweck.
  "empf"               -- Empfänger allein, wortwörtlich (groß/klein-
                          insensitiv). Auffangnetz für alles andere.

Gemessen (Training bis 2025, Test 2026, nur Zeilen mit bekanntem Empfänger):
Kat allein 73,2 % -> 81,2 %, Kat+UKat 68,1 % -> 75,2 %, wenn die Stufe
"zweck" dazukommt. Kat+UKat+Bem bleibt bei ~55 %, weil Bem freier Text ist.

NEUE STUFE HINZUFÜGEN (der "beliebige Prädikate"-Punkt aus way-to-go.md):
eine Schlüsselfunktion `Booking -> str | None` schreiben und als Tier vor die
bestehenden hängen. Die Funktion darf sich auf beliebige externe Quellen
stützen (z.B. eine aus Amazon-Mails gebaute Tabelle Betrag+Datum -> Bestellung);
sie muss nur für Zeilen aus money.csv und für Zeilen aus der DKB-CSV dasselbe
liefern -- dafür ist `Booking` da. An der Mechanik (zählen, Mehrheit bilden,
Konfidenz vergeben, in rules.json schreiben, in categorize_import abfragen)
ändert sich dann nichts.

Der Verwendungszweck-Normalisierer ist bewusst konservativ: er entfernt nur
Datumsangaben und Jahreszahlen (die machen jeden Zweck einmalig und damit
nutzlos). Alles andere bleibt stehen -- "Lerchenweg 19 A" und "Lerchenweg 13"
sind zwei verschiedene Häuser (L2 vs L1), und "VZ" vs "VA" ebenfalls
verschiedene Sachverhalte.

Nur bereits kategorisierte Ist-Zeilen (Kat nicht leer) fließen ein -- frisch
importierte, noch nicht korrigierte Zeilen sollen die Regeln nicht verfälschen.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:  # als Paketmodul: python -m finance.build_rules
    from . import import_dkb as money_io  # read_money_csv, MONEY_*/Ordner-Konstanten
except ImportError:  # direkt als Skript aus dem Paketordner gestartet
    import import_dkb as money_io

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACK_MONTHS = 36

# Beispiel Verwendungszweck: "1053000404929/PP.3647.PP/. Spotify AB, Ihr Einkauf..."
PAYPAL_SUBMERCHANT_RE = re.compile(r"/PP\.\d+\.PP/\.?\s*([^,]+),")

# Tokens, die den Verwendungszweck einmalig machen, ohne etwas zu bedeuten.
ZWECK_DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.(?:\d{2}|\d{4})\.?$")
ZWECK_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Ab welchem Anteil des häufigsten Treffers an allen Treffern für diesen
# Schlüssel gilt die Regel als "hoch" statt "mittel".
HIGH_CONFIDENCE_RATIO = 0.6
HIGH_CONFIDENCE_MIN_COUNT = 2

# Eine Zweck-Signatur, die nur einmal vorkommt, hat sich noch nicht als
# wiederkehrend erwiesen; sie bläht rules.json auf (1850 statt 248 Einträge),
# ohne die Trefferquote messbar zu verbessern.
ZWECK_MIN_SUPPORT = 2

KEY_SEPARATOR = " | "  # Empfänger | Zweck-Signatur, in rules.json lesbar

Triple = tuple[str, str, str]  # (Kat, UKat, Bem)


# ---------------------------------------------------------------------------
# Buchung: der gemeinsame Nenner von money.csv-Zeile und DKB-CSV-Zeile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Booking:
    """Was eine Schlüsselfunktion von einer Buchung sehen darf. Regeln werden
    aus money.csv gebaut und auf DKB-Zeilen angewandt -- ohne diese Brücke
    müsste jede Stufe zweimal geschrieben werden (einmal je Spaltenschema),
    und genau da schleichen sich Abweichungen ein."""
    empf: str
    text: str
    date: Optional[dt.date] = None
    amount: Optional[float] = None

    @classmethod
    def from_money_row(cls, row: dict) -> "Booking":
        return cls(
            empf=(row.get(money_io.MONEY_EMPF_COL) or "").strip(),
            text=row.get(money_io.MONEY_TEXT_COL) or "",
            date=row.get(money_io.MONEY_DATE_COL),
            amount=row.get(money_io.MONEY_AMOUNT_COL),
        )

    @classmethod
    def from_dkb_row(cls, row: dict) -> "Booking":
        raw_date = row.get(money_io.DKB_DATE_COL) or ""
        raw_amount = row.get(money_io.DKB_AMOUNT_COL) or ""
        return cls(
            empf=(row.get(money_io.DKB_EMPF_COL) or "").strip(),
            text=row.get(money_io.DKB_TEXT_COL) or "",
            date=money_io.parse_de_date(raw_date) if raw_date else None,
            amount=money_io.parse_de_amount(raw_amount) if raw_amount else None,
        )


# ---------------------------------------------------------------------------
# Schlüsselfunktionen
# ---------------------------------------------------------------------------

def extract_paypal_submerchant(text: str) -> Optional[str]:
    m = PAYPAL_SUBMERCHANT_RE.search(text or "")
    return m.group(1).strip().upper() if m else None


def normalize_zweck(text: str) -> str:
    """Verwendungszweck -> Signatur. Konservativ: nur Datumsangaben und
    Jahreszahlen fallen weg, der Rest bleibt wortwörtlich (bis auf
    Großschreibung und normalisierte Leerzeichen)."""
    tokens = [
        t for t in (text or "").upper().split()
        if not ZWECK_DATE_RE.match(t) and not ZWECK_YEAR_RE.match(t)
    ]
    return " ".join(tokens)


def key_paypal(b: Booking) -> Optional[str]:
    return extract_paypal_submerchant(b.text)


def key_zweck(b: Booking) -> Optional[str]:
    if not b.empf:
        return None
    sig = normalize_zweck(b.text)
    return f"{b.empf.upper()}{KEY_SEPARATOR}{sig}" if sig else None


def key_empf(b: Booking) -> Optional[str]:
    return b.empf.upper() or None


@dataclass(frozen=True)
class Tier:
    name: str
    key: Callable[[Booking], Optional[str]]
    min_support: int = 1


# Reihenfolge = Abfragereihenfolge: präzise vor grob.
TIERS: tuple[Tier, ...] = (
    Tier("paypal_submerchant", key_paypal),
    Tier("zweck", key_zweck, min_support=ZWECK_MIN_SUPPORT),
    Tier("empf", key_empf),
)

TIERS_BY_NAME = {t.name: t for t in TIERS}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def months_ago(d: dt.date, months: int) -> dt.date:
    """Kalendergenaue Monatssubtraktion (kein '*30 Tage'-Näherungswert)."""
    total = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _rule_entry(counter: Counter) -> dict:
    (kat, ukat, bem), count = counter.most_common(1)[0]
    total = sum(counter.values())
    ratio = count / total
    confidence = (
        "hoch" if (ratio >= HIGH_CONFIDENCE_RATIO and total >= HIGH_CONFIDENCE_MIN_COUNT)
        else "mittel"
    )
    return {
        "kat": kat, "ukat": ukat, "bem": bem,
        "count": count, "total": total, "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Kernlogik
# ---------------------------------------------------------------------------

def select_recent(
        money_rows: list[dict],
        lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
        today: Optional[dt.date] = None,
) -> list[dict]:
    """Ist-Buchungen (kein F-Block) der letzten `lookback_months` Monate, die
    bereits kategorisiert sind (Kat nicht leer). Von build_rules() genutzt,
    und wiederverwendbar für categorize_import.py's unscharfe Suchstufe --
    dieselbe Definition von 'brauchbare Trainingsdaten' an genau einer Stelle."""
    today = today or dt.date.today()
    cutoff = months_ago(today, lookback_months)
    return [
        r for r in money_rows
        if r[money_io.MONEY_FLAG_COL] == ""  # nur Ist-Buchungen, kein F-Block
           and r[money_io.MONEY_DATE_COL] is not None
           and r[money_io.MONEY_DATE_COL] >= cutoff
           and r[money_io.MONEY_KAT_COL]  # nur bereits kategorisierte Zeilen
    ]


def build_rules(
        money_rows: list[dict],
        lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
        today: Optional[dt.date] = None,
        min_support: Optional[dict[str, int]] = None,
) -> dict:
    """rules.json als dict. `min_support` überschreibt je Stufe die
    Mindestanzahl an Belegen, ab der eine Regel überhaupt geschrieben wird."""
    today = today or dt.date.today()
    cutoff = months_ago(today, lookback_months)
    recent = select_recent(money_rows, lookback_months, today)
    min_support = min_support or {}

    counters: dict[str, dict[str, Counter]] = {
        t.name: defaultdict(Counter) for t in TIERS
    }

    for r in recent:
        booking = Booking.from_money_row(r)
        triple: Triple = (
            r[money_io.MONEY_KAT_COL], r[money_io.MONEY_UKAT_COL], r[money_io.MONEY_BEM_COL]
        )
        for tier in TIERS:
            key = tier.key(booking)
            if key:
                counters[tier.name][key][triple] += 1

    tiers = {}
    for tier in TIERS:
        threshold = min_support.get(tier.name, tier.min_support)
        tiers[tier.name] = {
            key: _rule_entry(counter)
            for key, counter in sorted(counters[tier.name].items())
            if sum(counter.values()) >= threshold
        }

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "lookback_months": lookback_months,
        "cutoff_date": money_io.format_de_date(cutoff),
        "source_rows_total": len(money_rows),
        "source_rows_used": len(recent),
        "min_support": {t.name: min_support.get(t.name, t.min_support) for t in TIERS},
        "tier_order": [t.name for t in TIERS],
        "tiers": tiers,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    money_io.add_test_flag(ap)
    ap.add_argument("money", nargs="?", default="money.csv",
                    help="Dateiname im money-Ordner")
    ap.add_argument("-o", "--out", default="rules.json",
                    help="Zieldateiname im rules-Ordner")
    ap.add_argument("--lookback-months", type=int, default=DEFAULT_LOOKBACK_MONTHS)
    ap.add_argument("--zweck-min-support", type=int, default=ZWECK_MIN_SUPPORT,
                    help="ab wie vielen Belegen eine Zweck-Signatur zur Regel wird")
    args = ap.parse_args()

    fld = money_io.folders(args.test)
    money_path = money_io.resolve_in(fld.money, args.money, ".csv")
    out_path = money_io.resolve_in(fld.rules, args.out, ".json")

    money_rows = money_io.read_money_csv(money_path)
    rules = build_rules(money_rows, args.lookback_months,
                        min_support={"zweck": args.zweck_min_support})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")

    print(
        f"{rules['source_rows_used']} Ist-Buchungen der letzten {args.lookback_months} Monate "
        f"ausgewertet (von {rules['source_rows_total']} Zeilen insgesamt, "
        f"Stichtag {rules['cutoff_date']})."
    )
    for name in rules["tier_order"]:
        entries = rules["tiers"][name]
        n_hoch = sum(1 for v in entries.values() if v["confidence"] == "hoch")
        print(f"  {name:20} {len(entries):5} Regeln ({n_hoch} davon hoch)")
    print(f"Geschrieben nach {out_path}")


if __name__ == "__main__":
    main()
