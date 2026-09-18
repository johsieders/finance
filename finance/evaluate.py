"""
evaluate.py — misst, was die Regeln auf ungesehenen Monaten leisten.

Zeitlicher Split, keine Kreuzvalidierung: Regeln aus allen Ist-Buchungen VOR
einem Stichtag, gemessen an den Buchungen NACH ihm. Alles andere wäre
geschönt -- eine zufällige Aufteilung ließe Regeln aus Buchungen lernen, die
zum Vorhersagezeitpunkt noch nicht existierten, und bei monatlich
wiederkehrenden Lastschriften ist das der halbe Datensatz.

Drei Tabellen, in dieser Reihenfolge zu lesen:

  1. TREFFERQUOTE -- Abdeckung (wie oft überhaupt ein Vorschlag) und
     Genauigkeit (wie oft er stimmt), je Feld. Beides zusammen, denn eine
     Regel, die nur schweigt, hat 100 % Genauigkeit.

  2. KALIBRIERUNG -- die eigentliche Frage: hält p sein Versprechen? p ist
     als untere Schranke gebaut, also SOLL die gemessene Genauigkeit je
     Klasse über dem mittleren p liegen. Läge sie darunter, wäre p kaputt.

  3. LABELGRENZEN -- wo ein Schnitt durch p wirklich trennt. Je Kandidat
     die Genauigkeit unter und über ihm; gesucht ist ein grosser Abstand
     zwischen beiden. Unterdrückt wird nichts: auch ein Vorschlag mit
     kleinem p wird gemacht, er heisst dann "unklar". Das Urteil fällt beim
     Korrigieren, nicht hier.

Dazu ein Vergleich der Stufenauswahl (erste Stufe, die trifft, gegen die mit
dem höchsten p), weil das die zweite Änderung der Kalibrierung war.
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter

try:  # als Paketmodul: python -m finance.evaluate
    from . import build_rules, categorize_import
    from . import import_dkb as money_io
except ImportError:  # direkt als Skript aus dem Paketordner gestartet
    import build_rules, categorize_import
    import import_dkb as money_io

DEFAULT_SPLIT = dt.date(2026, 1, 1)
DEFAULT_TEST_MONTHS = 12

# Feinere Klassen als die drei Labels, damit man die Kalibrierungskurve sieht
# und nicht nur drei Punkte davon.
CALIBRATION_BINS = (0.0, 0.25, 0.35, 0.45, 0.60, 0.75, 0.85, 0.95, 1.01)

SWEEP = (0.0, 0.25, 0.35, 0.45, 0.60, 0.75, 0.85)


# ---------------------------------------------------------------------------
# Datensplit
# ---------------------------------------------------------------------------

def split_history(money_rows: list[dict], split: dt.date, test_months: int):
    """(Trainingszeilen, Testzeilen). Beides nur kategorisierte Ist-Buchungen:
    der F-Block ist ein Klon und würde die Messung verdoppeln, und eine noch
    nicht korrigierte Zeile hat keine Wahrheit, gegen die man messen kann."""
    end = build_rules.months_ago(split, -test_months)
    usable = [
        r for r in money_rows
        if r[money_io.MONEY_FLAG_COL] == ""
           and r[money_io.MONEY_DATE_COL] is not None
           and r[money_io.MONEY_KAT_COL]
    ]
    train = [r for r in usable if r[money_io.MONEY_DATE_COL] < split]
    test = [r for r in usable if split <= r[money_io.MONEY_DATE_COL] < end]
    return train, test


# ---------------------------------------------------------------------------
# Messung
# ---------------------------------------------------------------------------

def measure(money_rows: list[dict], split: dt.date, test_months: int,
            lookback_months: int) -> tuple[list[dict], dict]:
    """Ein Datensatz je Testbuchung mit ALLEN treffenden Regeln und ihren
    Roh-p-Werten -- also vor Stufenauswahl und vor dem Schnitt durch die
    Schwelle. Erst das erlaubt es, beides hinterher durchzuprobieren, ohne
    die Regeln jedes Mal neu zu bauen."""
    train, test = split_history(money_rows, split, test_months)
    rules = build_rules.build_rules(train, lookback_months, today=split)
    pool = build_rules.select_recent(train, lookback_months, today=split)
    cat = categorize_import.Categorizer(rules, pool)

    records = []
    for r in test:
        booking = build_rules.Booking.from_money_row(r)
        matches = []
        for index, (tier, entries) in enumerate(cat.tiers):
            key = tier.key(booking)
            if key and key in entries:
                matches.append((index, tier.name, entries[key]))
        fuzzy = None
        if not matches:
            s_ = cat.suggest(booking)
            fuzzy = (s_.kat, s_.ukat, s_.bem) if s_.kat else None
        records.append({
            "truth": (r[money_io.MONEY_KAT_COL], r[money_io.MONEY_UKAT_COL],
                      r[money_io.MONEY_BEM_COL]),
            "matches": matches,
            "fuzzy": fuzzy,
        })

    meta = {"n_train": len(train), "n_test": len(test),
            "split": split, "end": build_rules.months_ago(split, -test_months),
            "rules_total": sum(len(t) for t in rules["tiers"].values())}
    return records, meta


# ---------------------------------------------------------------------------
# Stufenauswahl als austauschbare Strategie -- Tabelle 4 vergleicht sie
# ---------------------------------------------------------------------------

def sel_first(matches, p_min):
    """Das alte Verhalten: die erste Stufe, die überhaupt trifft."""
    return matches[0] if matches else None


def sel_best_p(matches, p_min):
    """Die Stufe mit dem höchsten p, bei Gleichstand die präzisere."""
    return max(matches, key=lambda m: (m[2]["kat"]["p"], -m[0])) if matches else None


def sel_first_over(matches, p_min):
    """Das jetzige Verhalten: die erste Stufe, die die Schwelle besteht."""
    for m in matches:
        if m[2]["kat"]["p"] >= p_min:
            return m
    return matches[0] if matches else None


STRATEGIES = (
    ("erste Stufe, die trifft", sel_first),
    ("hoechstes p", sel_best_p),
    ("erste ueber der Schwelle", sel_first_over),
)


def decide(rec: dict, p_min: float, select=sel_first_over) -> tuple:
    """(Werte, Herkunft, p der Kat) -- genau das, was in der Vorschlagsdatei
    stünde. Unterdrückt wird nichts mehr: p bestimmt nur noch das Label. Die
    Schwelle steckt trotzdem noch in der Stufenauswahl."""
    chosen = select(rec["matches"], p_min)
    if chosen is None:
        return (rec["fuzzy"] or ("", "", ""),
                "unscharf" if rec["fuzzy"] else "none", None)
    _index, tier_name, rule = chosen
    return (tuple(rule[n]["value"] for n in ("kat", "ukat", "bem")),
            tier_name, rule["kat"]["p"])


# ---------------------------------------------------------------------------
# Tabellen
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:5.1f} %" if total else "    -  "


def report_accuracy(records: list[dict], p_min: float) -> None:
    n = len(records)
    decided = [decide(r, p_min) for r in records]
    with_sugg = [(v, rec) for (v, _s, _p), rec in zip(decided, records) if v[0]]
    print(f"\n1. TREFFERQUOTE ({n} Testbuchungen, davon {len(with_sugg)} mit "
          f"Vorschlag = {_pct(len(with_sugg), n).strip()})")
    print(f"   {'Feld':18} {'Genauigkeit':>13} {'gesamt richtig':>15}")
    for depth, label in enumerate(("Kat", "Kat+UKat", "Kat+UKat+Bem")):
        right = sum(v[:depth + 1] == rec["truth"][:depth + 1] for v, rec in with_sugg)
        print(f"   {label:18} {_pct(right, len(with_sugg)):>13} {_pct(right, n):>15}")
    srcs = Counter(src for _v, src, _p in decided)
    print("   Herkunft: " + ", ".join(f"{k} {v}" for k, v in srcs.most_common()))


def report_labels(records: list[dict], p_min: float) -> None:
    """Was die Worte in der Konfidenz-Spalte tatsächlich wert sind. Das ist
    die Tabelle, die beim Korrigieren zählt: sie sagt, welche Zeilen man
    durchwinken kann und welche man lesen muss."""
    print(f"\n2. WAS DIE LABELS WERT SIND (Kat)")
    print(f"   {'Konfidenz':12} {'n':>6} {'Kat richtig':>13}")
    buckets: dict[str, list[bool]] = {}
    for rec in records:
        values, src, p = decide(rec, p_min)
        if src == "none":
            continue
        label = "unscharf" if src == "unscharf" else build_rules.confidence_label(p)
        buckets.setdefault(label, []).append(values[0] == rec["truth"][0])
    for label in ("hoch", "mittel", "niedrig", "unklar", "unscharf"):
        hits = buckets.get(label)
        if not hits:
            continue
        note = "  <- wird vorgeschlagen, aber ausdruecklich als unklar" \
            if label == "unklar" else ""
        print(f"   {label:12} {len(hits):6} {_pct(sum(hits), len(hits)):>13}{note}")


def report_calibration(records: list[dict]) -> None:
    """Die eigentliche Kalibrierungsprobe, ohne Schwelle und ohne
    Stufenauswahl: je Regel, die als erste trifft, p gegen die gemessene
    Genauigkeit."""
    print("\n3. KALIBRIERUNG von p (erste treffende Regel, ohne Schwelle)")
    print("   p ist eine UNTERE Schranke: gemessen >= mittleres p ist richtig,")
    print("   gemessen < mittleres p waere ein Fehler in der Rechnung.")
    print(f"   {'p-Klasse':14} {'n':>6} {'mittleres p':>12} {'Kat richtig':>13}  Urteil")
    rows = [(r["matches"][0][2], r["truth"]) for r in records if r["matches"]]
    for lo, hi in zip(CALIBRATION_BINS, CALIBRATION_BINS[1:]):
        bucket = [(rule, truth) for rule, truth in rows if lo <= rule["kat"]["p"] < hi]
        if not bucket:
            continue
        mean_p = sum(rule["kat"]["p"] for rule, _t in bucket) / len(bucket)
        right = sum(rule["kat"]["value"] == t[0] for rule, t in bucket)
        verdict = "haelt" if right / len(bucket) >= mean_p else "ZU OPTIMISTISCH"
        print(f"   {lo:.2f}-{min(hi, 1.0):.2f}      {len(bucket):6} "
              f"{mean_p:11.2f} {_pct(right, len(bucket)):>13}  {verdict}")


def report_boundaries(records: list[dict]) -> None:
    """Wo trennt ein Schnitt durch p wirklich? Je Kandidat die Genauigkeit
    unter und über ihm. Ein guter Schnitt hat einen grossen Abstand zwischen
    beiden Spalten -- dort trennt das Label Verlaessliches von Geratenem.

    Nur regelgestützte Buchungen: die unscharfe Stufe hat kein p und wäre in
    jeder Zeile dieselbe Beimischung."""
    rows = [(r["matches"][0][2]["kat"], r["truth"][0]) for r in records if r["matches"]]
    print(f"\n4. LABELGRENZEN ({len(rows)} regelgestuetzte Buchungen)")
    print(f"   {'Schnitt':10} {'n drunter':>11} {'richtig':>9}   "
          f"{'n drueber':>11} {'richtig':>9}   Abstand")
    for cut in SWEEP[1:]:
        lo = [(f, t) for f, t in rows if f["p"] < cut]
        hi = [(f, t) for f, t in rows if f["p"] >= cut]
        if not lo or not hi:
            continue
        a = sum(f["value"] == t for f, t in lo) / len(lo)
        b = sum(f["value"] == t for f, t in hi) / len(hi)
        print(f"   p = {cut:.2f}   {len(lo):11} {100*a:8.1f} %   "
              f"{len(hi):11} {100*b:8.1f} %   {100*(b-a):6.1f} pp")


def report_tier_choice(records: list[dict], p_min: float) -> None:
    rule_recs = [r for r in records if r["matches"]]
    n = len(rule_recs)
    print(f"\n5. STUFENAUSWAHL ({n} regelgestuetzte Buchungen, Schwelle {p_min:.2f})")
    print(f"   {'Strategie':26} {'Kat gefuellt':>14} {'richtig/alle':>14} "
          f"{'falsch/alle':>13}")
    for label, select in STRATEGIES:
        filled = right = 0
        for rec in rule_recs:
            values, _src, _p = decide(rec, p_min, select)
            if values[0]:
                filled += 1
                right += values[0] == rec["truth"][0]
        print(f"   {label:26} {_pct(filled, n):>14} {_pct(right, n):>14} "
              f"{_pct(filled - right, n):>13}")
    ambiguous = sum(1 for r in rule_recs if len(r["matches"]) > 1)
    print(f"   ({ambiguous} Buchungen treffen ueberhaupt mehr als eine Stufe -- "
          f"nur dort koennen sich die Strategien unterscheiden)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    money_io.add_test_flag(ap)
    ap.add_argument("money", nargs="?", default="money.csv",
                    help="Dateiname im money-Ordner")
    ap.add_argument("--split", default=money_io.format_de_date(DEFAULT_SPLIT),
                    help="Stichtag TT.MM.JJJJ: davor Training, danach Test")
    ap.add_argument("--test-months", type=int, default=DEFAULT_TEST_MONTHS)
    ap.add_argument("--lookback-months", type=int,
                    default=build_rules.DEFAULT_LOOKBACK_MONTHS)
    ap.add_argument("--p-min", type=float, default=build_rules.P_UNCLEAR,
                    help="Grenze zum Label 'unklar' (Default: der eingebaute Wert)")
    args = ap.parse_args()

    fld = money_io.folders(args.test)
    money_path = money_io.resolve_in(fld.money, args.money, ".csv")
    money_rows = money_io.read_money_csv(money_path)
    split = money_io.parse_de_date(args.split)

    records, meta = measure(money_rows, split, args.test_months, args.lookback_months)
    print(f"{money_path.name}: Training bis {money_io.format_de_date(meta['split'])} "
          f"({meta['n_train']} Buchungen, {meta['rules_total']} Regeln), "
          f"Test bis {money_io.format_de_date(meta['end'])} ({meta['n_test']} Buchungen)")
    if not records:
        print("Keine Testbuchungen im gewaehlten Zeitraum.")
        return
    report_accuracy(records, args.p_min)
    report_labels(records, args.p_min)
    report_calibration(records)
    report_boundaries(records)
    report_tier_choice(records, args.p_min)


if __name__ == "__main__":
    main()
