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
import math
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

# Konfidenz ist die UNTERE SCHRANKE eines einseitigen 95%-Kredibilitäts-
# intervalls der Beta-Posteriori, nicht der rohe Anteil count/total. Der rohe
# Anteil kann Belegmenge nicht von Einigkeit unterscheiden: 2/2 und 133/135
# sind beide "100 % einig", aber nur das zweite ist ein Versprechen. Die
# Schranke trennt sie (0,37 vs 0,96), weil sie mit der Belegmenge wächst.
CREDIBLE_LEVEL = 0.05  # 5 %-Quantil = "mit 95 % Sicherheit mindestens p"

# Schwellen auf p. Als Anhaltspunkt, bei lückenloser Einigkeit:
#   1/1 -> 0,22   2/2 -> 0,37   5/5 -> 0,61   10/10 -> 0,76   20/20 -> 0,87
# "hoch" verlangt damit ~20 Belege, nicht mehr zwei.
P_HIGH = 0.85
P_MEDIUM = 0.60

# Darunter wird das Feld NICHT vorgeschlagen, sondern leer gelassen. Ein
# angenommener Fehlvorschlag landet in money.csv und damit in den nächsten
# Regeln -- ein leeres Feld kostet nur Tipparbeit.
P_MIN_SUGGEST = 0.35

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


def _beta_cdf(x: float, a: int, b: int) -> float:
    """P(Beta(a,b) <= x) für ganzzahlige a, b. Gleich dem Binomial-Oberschwanz
    P(Bin(a+b-1, x) >= a), also eine endliche Summe mit b Gliedern -- und b ist
    hier 'Zahl der Abweichler + 1', meist 1 bis 3. Terme in Logarithmen, damit
    auch Schlüssel mit hunderten Belegen nicht überlaufen."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    n = a + b - 1
    log_x, log_1x = math.log(x), math.log1p(-x)
    return math.fsum(
        math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
                 + j * log_x + (n - j) * log_1x)
        for j in range(a, n + 1)
    )


def beta_lower_bound(count: int, total: int, level: float = CREDIBLE_LEVEL) -> float:
    """Untere Schranke für die Trefferwahrscheinlichkeit einer Regel, die
    `count` von `total` Belegen auf sich vereint: das `level`-Quantil der
    Posteriori Beta(count+1, total-count+1) -- Laplace-Prior Beta(1,1), also
    "vor allen Beobachtungen ist jede Trefferquote gleich plausibel".

    Bei lückenloser Einigkeit (count == total) ist das geschlossen
    level**(1/(total+1)); der allgemeine Fall per Bisektion, 50 Schritte."""
    a, b = count + 1, total - count + 1
    if b == 1:  # keine Abweichler: CDF ist x**(total+1)
        return level ** (1.0 / (total + 1))
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _beta_cdf(mid, a, b) < level:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def confidence_label(p: float) -> str:
    """p -> das Wort, das in der Konfidenz-Spalte der Vorschlagsdatei steht."""
    if p >= P_HIGH:
        return "hoch"
    if p >= P_MEDIUM:
        return "mittel"
    if p >= P_MIN_SUGGEST:
        return "niedrig"
    return "-"


def _mode(counts: Counter) -> tuple[str, int]:
    """Häufigster Wert. Gleichstand alphabetisch aufgelöst, damit rules.json
    bei gleicher Historie Zeichen für Zeichen gleich bleibt und diffbar ist."""
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))


def _field(value: str, count: int, total: int) -> dict:
    return {"value": value, "count": count,
            "p": round(beta_lower_bound(count, total), 3)}


def _rule_entry(counter: Counter) -> dict:
    """Beobachtete Tripel eines Schlüssels -> Regeleintrag mit je Feld einem
    eigenen p. Feldweise und nicht als Tripel, weil ein Schlüssel eine völlig
    sichere Kat und eine hoffnungslose Bem haben kann -- bei freiem Text ist
    das der Normalfall. Ein Tripel-p würde die sichere Kat mit unterdrücken.

    Die Felder werden als PRÄFIX gezählt: p_kat = "Kat stimmt",
    p_ukat = "Kat und UKat stimmen", p_bem = "alle drei stimmen". Damit ist
    p monoton fallend, und UKat wird nur unter der schon gewählten Kat
    gesucht (sonst käme ein inkohärentes Paar heraus)."""
    total = sum(counter.values())

    kat_counts: Counter = Counter()
    for (kat, _u, _b), n in counter.items():
        kat_counts[kat] += n
    kat, kat_n = _mode(kat_counts)

    ukat_counts: Counter = Counter()
    for (k, ukat, _b), n in counter.items():
        if k == kat:
            ukat_counts[ukat] += n
    ukat, ukat_n = _mode(ukat_counts)

    bem_counts: Counter = Counter()
    for (k, u, bem), n in counter.items():
        if k == kat and u == ukat:
            bem_counts[bem] += n
    bem, bem_n = _mode(bem_counts)

    return {
        "total": total,
        "kat": _field(kat, kat_n, total),
        "ukat": _field(ukat, ukat_n, total),
        "bem": _field(bem, bem_n, total),
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
        "credible_level": CREDIBLE_LEVEL,
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
        labels = Counter(confidence_label(v["kat"]["p"]) for v in entries.values())
        detail = ", ".join(f"{labels[k]} {k}" for k in ("hoch", "mittel", "niedrig", "-")
                           if labels[k])
        print(f"  {name:20} {len(entries):5} Regeln (Kat: {detail})")
    print(f"Geschrieben nach {out_path}")


if __name__ == "__main__":
    main()
