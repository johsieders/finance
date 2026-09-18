"""
import_dkb.py — trägt eine korrigierte Vorschlagsdatei in money.csv ein.

Eingabe ist NICHT mehr die rohe DKB-Umsatzliste, sondern die von
categorize_import.py erzeugte und von dir korrigierte
Vorschlagsdatei (suggestions/<auszug>.suggestion.csv). Sie hat dieselbe
Spaltenstruktur wie money.csv, mit ausgefüllten Kat/UKat/Bem. Jede neue
Buchung wird zweimal eingetragen: als Ist-Zeile und -- um ein Jahr in die
Zukunft verschoben -- als F-Zeile der rollenden Vorschau.

Designentscheidungen:

1. GENAU EIN Indexschema: 0-basierte Python-Listen/Dicts. Die Google-Sheets-
   Welt (1-basiert, insertRows/deleteRows, relative Formeln) kommt hier nicht
   mehr vor. MT und Saldo werden direkt berechnet statt über Sheet-Formeln --
   die Rekurrenz ist dieselbe (MT[i] = MT[unten] + 1, Saldo[i] = Betrag[i] +
   Saldo[unten]), nur als Python-Schleife statt als setFormula().

2. Spalten werden über die Kopfzeile erkannt, nicht über feste Positionen --
   das war der Auslöser für den Formatwechsel-Ärger vom 9.11.2024. Einzige
   Ausnahme: die Spalte zwischen UKat und Betrag hat in money.csv keine
   Überschrift. Sie heißt hier "Bem" (Bemerkung) und wird per Positionsanker
   (zwischen UKat und Betrag) erkannt, nicht per Name -- sie hat ja keinen.

3. Die Kopfzeile von money.csv wird beim Schreiben unverändert
   zurückgeschrieben (inklusive der namenlosen Bem-Spalte und der leeren
   Spalten am Zeilenende), und Beträge behalten Tausenderpunkt und "€".
   Sonst formatiert jeder Import die ganze Datei um, und der Diff einer
   31.000-Zeilen-Datei ist nicht mehr lesbar.

4. Kategorisieren passiert nicht mehr hier, sondern in categorize_import.py.
   Dieses Modul kennt keine Regeln mehr -- es rechnet MT/Saldo und rollt den
   F-Block.

5. Dedup weiterhin über Betragsgleichheit, aber als Multimenge (Counter)
   statt als Menge: drei gleich hohe Buchungen am Grenztag fallen nicht mehr
   zu einer zusammen. Verglichen wird genau gegen die vorhandenen Ist-Zeilen
   des Grenztags (nicht mehr gegen die letzten 15 Zeilen, die auch ältere
   Tage enthielten). Buchungen nach dem Grenztag sind per Definition neu.
   Eine wirklich exakte Erkennung bräuchte die DKB-"Kundenreferenz" als neue
   money.csv-Spalte -- bewusst zurückgestellt, siehe way-to-go.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Ordnerstruktur (echt / Test)
# ---------------------------------------------------------------------------

FINANCE_FOLDER = Path("/Users/johannes.siedersleben/Documents/finance")
FINANCE_TEST_FOLDER = Path("/Users/johannes.siedersleben/Documents/finance-test")


@dataclass(frozen=True)
class Folders:
    root: Path
    money: Path
    umsatzlisten: Path
    rules: Path
    suggestions: Path


def folders(test: bool = False) -> Folders:
    """Alle Pfade hängen an genau einer Wurzel -- '--test' schaltet sie um.
    finance-test ist eine Kopie von finance; dort lassen sich vergangene
    Jahre gefahrlos nachspielen."""
    root = FINANCE_TEST_FOLDER if test else FINANCE_FOLDER
    return Folders(
        root=root,
        money=root / "money",
        umsatzlisten=root / "umsatzlisten",
        rules=root / "rules",
        suggestions=root / "suggestions",
    )


def add_test_flag(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--test", action="store_true",
                    help=f"Testumgebung benutzen ({FINANCE_TEST_FOLDER}) statt {FINANCE_FOLDER}")


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

MONEY_COLUMNS = [
    "MF", "MT", "BuDat", "Jahr", "Monat",
    "Kat", "UKat", "Bem",
    "Betrag", "Saldo", "Empfänger", "Text",
]

MONEY_FLAG_COL = "MF"  # "" = Ist-Buchung, "F" = Future/Vorschau
MONEY_MT_COL = "MT"
MONEY_DATE_COL = "BuDat"
MONEY_YEAR_COL = "Jahr"
MONEY_MONTH_COL = "Monat"
MONEY_AMOUNT_COL = "Betrag"
MONEY_SALDO_COL = "Saldo"
MONEY_EMPF_COL = "Empfänger"
MONEY_TEXT_COL = "Text"
MONEY_KAT_COL = "Kat"
MONEY_UKAT_COL = "UKat"
MONEY_BEM_COL = "Bem"

MONEY_DATE_COLS = (MONEY_DATE_COL,)
MONEY_AMOUNT_COLS = (MONEY_AMOUNT_COL, MONEY_SALDO_COL)
MONEY_INT_COLS = (MONEY_MT_COL, MONEY_YEAR_COL, MONEY_MONTH_COL)

FUTURE_FLAG = "F"

# Felder aus der DKB-CSV, per Name gesucht (nicht Position).
DKB_DATE_COL = "Buchungsdatum"
DKB_STATUS_COL = "Status"
DKB_STATUS_BOOKED = "Gebucht"
DKB_EMPF_COL = "Zahlungsempfänger*in"
DKB_TEXT_COL = "Verwendungszweck"
DKB_AMOUNT_COL = "Betrag (€)"
DKB_REQUIRED = [DKB_DATE_COL, DKB_STATUS_COL, DKB_EMPF_COL, DKB_TEXT_COL, DKB_AMOUNT_COL]

SUGGESTION_SUFFIX = ".suggestion.csv"
SUGGESTION_DELIMITER = ";"  # was deutsches Excel beim CSV-Export erzeugt

NBSP = " "


# ---------------------------------------------------------------------------
# Parsing-Hilfsfunktionen
# ---------------------------------------------------------------------------

def parse_de_amount(s: str) -> float:
    """'-1.500,00 €' / '-40,97' / '500' -> float. Entspricht stringToNumber()."""
    s = s.replace("€", "").replace(NBSP, "").strip()
    s = s.replace(".", "").replace(",", ".")
    return float(s)


def format_de_amount(x: float) -> str:
    """float -> '-9.198,34 €' -- genau das Format, das money.csv schon enthält."""
    body = f"{abs(x):,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return f"{'-' if x < 0 else ''}{body} €"


def parse_de_date(s: str) -> dt.date:
    """'DD.MM.YY' oder 'DD.MM.YYYY' -> date. Entspricht parseCustomDate()."""
    d, m, y = s.strip().split(".")
    year = int(y)
    if year < 100:
        year += 2000 if year < 70 else 1900
    return dt.date(year, int(m), int(d))


def format_de_date(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def shift_one_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        # 29. Februar -> 28. Februar im Folgejahr
        return d.replace(year=d.year + 1, day=28)


def _find_header_line(lines: list[str], marker: str, what: str) -> int:
    """Index der Kopfzeile. Vor ihr dürfen beliebige Zeilen stehen: die DKB
    schreibt Konto/Zeitraum/Kontostand davor, Excel schreibt beim Export
    gelegentlich den Dateinamen davor."""
    for i, line in enumerate(lines):
        if marker in line:
            return i
    raise ValueError(f"{what}: keine Kopfzeile mit '{marker}' gefunden.")


def _sniff_delimiter(header_line: str) -> str:
    return ";" if header_line.count(";") > header_line.count(",") else ","


def _csv_from(lines: list[str], header_idx: int) -> io.StringIO:
    """Der Teil der Datei ab der Kopfzeile, als Datei-Objekt -- nicht als
    Zeilenliste: nur so überlebt ein Zeilenumbruch innerhalb eines
    Verwendungszwecks das Einlesen."""
    return io.StringIO("\n".join(lines[header_idx:]))


# ---------------------------------------------------------------------------
# DKB-CSV einlesen
# ---------------------------------------------------------------------------

def read_dkb_csv(path: Path) -> list[dict]:
    """Liest eine DKB-Umsatzliste: überspringt die Metadaten-Zeilen (Konto /
    Zeitraum / Kontostand) vor der eigentlichen Kopfzeile, erkennt den
    Trenner (';' oder ',') automatisch, validiert die benötigten Spalten."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header_idx = _find_header_line(lines, '"Buchungsdatum"', f"DKB-CSV {path.name}")
    delim = _sniff_delimiter(lines[header_idx])

    reader = csv.DictReader(_csv_from(lines, header_idx), delimiter=delim)
    fieldnames = reader.fieldnames or []
    missing = [c for c in DKB_REQUIRED if c not in fieldnames]
    if missing:
        raise ValueError(
            f"DKB-CSV: erwartete Spalten fehlen: {missing}. "
            f"Gefundene Spalten: {fieldnames}. "
            "Format hat sich vermutlich geändert -- DKB_* Konstanten oben prüfen."
        )
    return [row for row in reader if row.get(DKB_DATE_COL)]


def find_latest_by_date_prefix(folder: Path, pattern: str) -> Path:
    """Entspricht findLatestIBANString(): nimmt die Datei mit dem jüngsten
    Datum 'DD-MM-YYYY' am Namensanfang."""
    candidates: list[tuple[dt.date, Path]] = []
    for p in folder.glob(pattern):
        try:
            d = dt.datetime.strptime(p.name[:10], "%d-%m-%Y").date()
        except ValueError:
            continue
        candidates.append((d, p))
    if not candidates:
        raise FileNotFoundError(f"Keine Datei '{pattern}' in {folder} gefunden.")
    candidates.sort(key=lambda t: (t[0], t[1].name))
    return candidates[-1][1]


def find_latest_dkb_file(folder: Path, iban: str) -> Path:
    return find_latest_by_date_prefix(folder, f"*{iban}.csv")


def find_latest_suggestion_file(folder: Path) -> Path:
    return find_latest_by_date_prefix(folder, f"*{SUGGESTION_SUFFIX}")


# ---------------------------------------------------------------------------
# money.csv einlesen / schreiben
# ---------------------------------------------------------------------------

def money_column_index(header: list[str]) -> dict[str, int]:
    """Spaltenname -> Position. Die namenlose Spalte zwischen UKat und Betrag
    wird per Positionsanker als 'Bem' erkannt (sie hat keinen Namen, kann also
    nicht per Name gefunden werden). Leere Spalten am Zeilenende werden
    ignoriert."""
    idx = {name: i for i, name in enumerate(header) if name}
    if MONEY_BEM_COL not in idx:
        try:
            ukat_idx, betrag_idx = idx["UKat"], idx["Betrag"]
        except KeyError as exc:
            raise ValueError(f"Kopfzeile ohne Spalte {exc}: {header}") from exc
        if betrag_idx != ukat_idx + 2 or header[ukat_idx + 1] != "":
            raise ValueError(
                f"namenlose Spalte zwischen UKat und Betrag nicht gefunden "
                f"(Kopfzeile: {header}). Bitte manuell prüfen."
            )
        idx[MONEY_BEM_COL] = ukat_idx + 1
    missing = [c for c in MONEY_COLUMNS if c not in idx]
    if missing:
        raise ValueError(f"Spalten fehlen: {missing} (Kopfzeile: {header})")
    return idx


def read_money_file(path: Path) -> tuple[list[str], list[dict]]:
    """(Kopfzeile wortwörtlich, Zeilen als dicts). Die Kopfzeile wird für
    write_money_csv() aufbewahrt, damit die Datei beim Schreiben nicht
    umformatiert wird."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = money_column_index(header)
        rows = [
            _coerce_money_row({n: (raw[i] if i < len(raw) else "") for n, i in idx.items()})
            for raw in reader if raw and any(raw)
        ]
    return header, rows


def read_money_csv(path: Path) -> list[dict]:
    return read_money_file(path)[1]


def read_suggestion_csv(path: Path) -> list[dict]:
    """Liest die korrigierte Vorschlagsdatei. Gleiche Struktur wie
    money.csv; MF/MT/Saldo sind leer und werden hier berechnet. Zusätzliche
    Spalten am Zeilenende (Konfidenz, Quelle) werden ignoriert, eine
    Vorlaufzeile vor der Kopfzeile ebenfalls."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header_idx = _find_header_line(lines, MONEY_DATE_COL, f"Vorschlagsdatei {path.name}")
    delim = _sniff_delimiter(lines[header_idx])
    reader = csv.reader(_csv_from(lines, header_idx), delimiter=delim)
    header = next(reader)
    idx = money_column_index(header)

    rows = []
    for raw in reader:
        if not raw or not any(raw):
            continue
        lineno = header_idx + reader.line_num
        row = _coerce_money_row({n: (raw[i] if i < len(raw) else "") for n, i in idx.items()})
        if row[MONEY_FLAG_COL]:
            raise ValueError(
                f"{path.name} Zeile {lineno}: Spalte {MONEY_FLAG_COL} ist "
                f"'{row[MONEY_FLAG_COL]}', erwartet wird eine leere (Ist-)Buchung. "
                "F-Zeilen entstehen hier automatisch."
            )
        if row[MONEY_DATE_COL] is None or row[MONEY_AMOUNT_COL] is None:
            raise ValueError(f"{path.name} Zeile {lineno}: BuDat oder Betrag fehlt.")
        rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: keine Buchungen gefunden.")
    return rows


def _coerce_money_row(row: dict) -> dict:
    row = {col: row.get(col, "") for col in MONEY_COLUMNS}
    row[MONEY_DATE_COL] = parse_de_date(row[MONEY_DATE_COL]) if row[MONEY_DATE_COL] else None
    for col in MONEY_INT_COLS:
        row[col] = int(row[col]) if str(row[col]).strip() else None
    for col in MONEY_AMOUNT_COLS:
        row[col] = _to_float(row[col])
    return row


def _to_float(s) -> float | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return parse_de_amount(str(s))


def _format_money_value(col: str, value) -> str:
    if value is None or value == "":
        return ""
    if col in MONEY_DATE_COLS:
        return format_de_date(value)
    if col in MONEY_AMOUNT_COLS:
        return format_de_amount(value)
    if col in MONEY_INT_COLS:
        return str(int(value))
    return str(value)


def write_money_csv(path: Path, rows: list[dict], header: list[str] | None = None,
                    delimiter: str = ",") -> None:
    """Schreibt money.csv. `header` ist die wortwörtliche Kopfzeile aus
    read_money_file() -- damit bleiben die namenlose Bem-Spalte und die leeren
    Spalten am Zeilenende erhalten und der Diff bleibt lesbar."""
    header = header or MONEY_COLUMNS
    idx = money_column_index(header)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(header)
        for row in rows:
            out = [""] * len(header)
            for col, i in idx.items():
                out[i] = _format_money_value(col, row.get(col))
            writer.writerow(out)


# ---------------------------------------------------------------------------
# Kernlogik
# ---------------------------------------------------------------------------

def find_boundary(rows: list[dict]) -> int:
    """Index der ersten Ist-Zeile (MF == ""), analog zu findFirstRowWithValue()."""
    for i, row in enumerate(rows):
        if row[MONEY_FLAG_COL] == "":
            return i
    raise ValueError("Keine Ist-Zeile gefunden -- besteht money.csv nur aus F-Zeilen?")


def _cents(x: float) -> int:
    return round(x * 100)


def select_new_bookings(money_rows: list[dict], new_rows: list[dict],
                        boundary: int) -> list[dict]:
    """Die Zeilen aus `new_rows`, die noch nicht in money.csv stehen.

    Der Auszug überlappt mit dem letzten Import: alles vor dem Grenztag (dem
    Buchungsdatum der jüngsten Ist-Zeile) ist erfasst, alles danach ist neu,
    und genau am Grenztag wird über Betragsgleichheit entschieden -- als
    Multimenge, damit mehrere gleich hohe Buchungen erhalten bleiben."""
    base_day = money_rows[boundary][MONEY_DATE_COL]

    already: Counter = Counter()
    for row in money_rows[boundary:]:
        if row[MONEY_DATE_COL] != base_day:
            break
        already[_cents(row[MONEY_AMOUNT_COL])] += 1

    candidates = sorted(
        (r for r in new_rows if r[MONEY_DATE_COL] >= base_day),
        key=lambda r: r[MONEY_DATE_COL], reverse=True,  # stabil: Reihenfolge je Tag bleibt
    )

    fresh = []
    for r in candidates:
        if r[MONEY_DATE_COL] == base_day:
            amount = _cents(r[MONEY_AMOUNT_COL])
            if already[amount] > 0:
                already[amount] -= 1
                continue
        row = dict(r)
        row[MONEY_FLAG_COL] = ""
        row[MONEY_YEAR_COL] = row[MONEY_DATE_COL].year
        row[MONEY_MONTH_COL] = row[MONEY_DATE_COL].month
        fresh.append(row)
    return fresh


def run_import(money_rows: list[dict], new_rows: list[dict]) -> tuple[list[dict], int]:
    """Ein Import-Lauf. `new_rows` sind Zeilen in money.csv-Struktur (aus der
    korrigierten Vorschlagsdatei). Gibt (neue Zeilenliste, Anzahl neu
    importierter Buchungen) zurück; `money_rows` wird nicht verändert."""

    boundary = find_boundary(money_rows)
    new_real_rows = select_new_bookings(money_rows, new_rows, boundary)

    n_new = len(new_real_rows)
    if n_new == 0:
        return money_rows, 0

    # MT / Saldo der neuen Ist-Zeilen: Anschluss an die (bisherige) Zeile darunter.
    below = money_rows[boundary]
    mt_base = below[MONEY_MT_COL]
    for offset, row in enumerate(new_real_rows):  # offset 0 = oberste/neueste
        row[MONEY_MT_COL] = mt_base + (n_new - offset)
    running = below[MONEY_SALDO_COL]
    for row in reversed(new_real_rows):  # unten (ältest) -> oben aufsummieren
        running = running + row[MONEY_AMOUNT_COL]
        row[MONEY_SALDO_COL] = running

    # F-Block: obsolete Zeilen (BuDat <= jüngstes neues Datum) von unten her entfernen.
    f_block = money_rows[:boundary]
    newest_new_day = new_real_rows[0][MONEY_DATE_COL]
    keep_upto = 0
    for i in range(len(f_block) - 1, -1, -1):
        if f_block[i][MONEY_DATE_COL] > newest_new_day:
            keep_upto = i + 1
            break
    kept_f = [dict(r) for r in f_block[:keep_upto]]

    # MT/Saldo des behaltenen F-Blocks komplett fortschreiben, nicht nur die
    # unterste Zeile: im Sheet waren das relative Formeln, die sich beim Patchen
    # der Ankerzeile selbst neu berechnet haben. Hier muss die Rekurrenz
    # (MT[i] = MT[unten] + 1, Saldo[i] = Betrag[i] + Saldo[unten]) explizit nach
    # oben durchlaufen, sonst behalten alle Zeilen darüber ihre Vor-Import-Werte.
    # Leeres Betrag-Feld zählt als 0 -- im F-Block gibt es solche Zeilen.
    running_mt = new_real_rows[0][MONEY_MT_COL]
    running_saldo = new_real_rows[0][MONEY_SALDO_COL]
    for row in reversed(kept_f):  # unten (ältest) -> oben
        running_mt += 1
        running_saldo = (row[MONEY_AMOUNT_COL] or 0.0) + running_saldo
        row[MONEY_MT_COL] = running_mt
        row[MONEY_SALDO_COL] = running_saldo

    # Neue F-Zeilen: Kopie der neuen Ist-Zeilen, +1 Jahr -- inklusive Kat/UKat/Bem.
    # Ersetzt den bisherigen manuellen "in den F-Bereich kopieren"-Schritt.
    new_f_rows = []
    for row in new_real_rows:
        clone = dict(row)
        clone[MONEY_FLAG_COL] = FUTURE_FLAG
        clone[MONEY_DATE_COL] = shift_one_year(row[MONEY_DATE_COL])
        clone[MONEY_YEAR_COL] = clone[MONEY_DATE_COL].year
        clone[MONEY_MONTH_COL] = clone[MONEY_DATE_COL].month
        new_f_rows.append(clone)

    mt_anchor = kept_f[0][MONEY_MT_COL] if kept_f else new_real_rows[0][MONEY_MT_COL]
    for offset, row in enumerate(new_f_rows):
        row[MONEY_MT_COL] = mt_anchor + (n_new - offset)
    saldo_anchor = kept_f[0][MONEY_SALDO_COL] if kept_f else new_real_rows[0][MONEY_SALDO_COL]
    running = saldo_anchor
    for row in reversed(new_f_rows):
        running = running + row[MONEY_AMOUNT_COL]
        row[MONEY_SALDO_COL] = running

    result = new_f_rows + kept_f + new_real_rows + money_rows[boundary:]
    return result, n_new


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_in(folder: Path, name, suffix: str) -> Path:
    """Bloßer Dateiname -> Pfad im vorgesehenen Ordner.

    Die CLI-Argumente sind Dateinamen, keine Pfade: '--money money.csv' meint
    <folder>/money.csv. Die Endung darf weggelassen werden ('--money money'
    geht auch), anzugeben ist sie aber das Natürlichere -- so kann man den Namen
    direkt aus `ls` oder per Tab-Completion übernehmen, was bei Namen wie
    'money-aug24 - DKB.csv' der einzige fehlerfreie Weg ist.

    Ein absoluter Pfad wird unverändert übernommen (Notausgang für Dateien
    außerhalb der Ordnerstruktur)."""
    p = Path(name)
    if not p.suffix:
        p = p.with_suffix(suffix)
    return p if p.is_absolute() else folder / p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_test_flag(ap)
    ap.add_argument("--money", default="money.csv", help="Dateiname im money-Ordner")
    ap.add_argument("--suggestion",
                    help="Dateiname im suggestions-Ordner (Default: neueste Datei dort)")
    ap.add_argument("--out", help="Zieldateiname im money-Ordner (Default: --money überschreiben)")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, was passieren würde")
    args = ap.parse_args()

    fld = folders(args.test)
    money_path = resolve_in(fld.money, args.money, ".csv")
    if args.suggestion:
        suggestion_path = resolve_in(fld.suggestions, args.suggestion, ".csv")
    else:
        suggestion_path = find_latest_suggestion_file(fld.suggestions)
        print(f"Gefundene Vorschlagsdatei: {suggestion_path.name}")

    header, money_rows = read_money_file(money_path)
    new_rows = read_suggestion_csv(suggestion_path)

    boundary = find_boundary(money_rows)
    base_day = money_rows[boundary][MONEY_DATE_COL]
    result, n_new = run_import(money_rows, new_rows)

    print(f"money.csv: {len(money_rows)} Zeilen, jüngste Ist-Buchung {format_de_date(base_day)}.")
    print(f"Vorschlag: {len(new_rows)} Buchungen -> {n_new} neu.")
    n_blank = sum(1 for r in new_rows if not r[MONEY_KAT_COL])
    if n_blank:
        # categorize_import lässt Kat nur dann leer, wenn gar keine Stufe
        # getroffen hat (Konfidenz "-"); dünn belegte Vorschläge macht es
        # trotzdem und nennt sie "unklar". Erwähnen lohnt sich, denn solche
        # Zeilen zählen für build_rules nicht mit: die Lücke pflanzt sich fort.
        print(f"{n_blank} Buchung(en) ohne Kat -- unkategorisiert übernommen; "
              f"sie zählen erst für build_rules, wenn Kat gefüllt ist.")
    if n_new == 0 or args.dry_run:
        return

    out_path = resolve_in(fld.money, args.out, ".csv") if args.out else money_path
    write_money_csv(out_path, result, header)
    print(f"Geschrieben nach {out_path} ({len(result)} Zeilen).")


if __name__ == "__main__":
    main()
