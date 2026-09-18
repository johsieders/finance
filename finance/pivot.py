"""
pivot.py — Pivot-Tabelle über money.csv, zum Auf- und Zuklappen.

Zeilen Kat/UKat, Spalten Jahr/Monat, Werte die Summe der Beträge. Jahre
absteigend, das laufende zuerst. Beide
Achsen sind zweistufig und beide lassen sich einzeln aufklappen: ein Klick
auf eine Kat zeigt ihre UKat, ein Klick auf ein Jahr seine Monate. Zugeklappt
sieht man also Kat x Jahr, voll aufgeklappt UKat x Monat.

Nur Ist-Buchungen. Der F-Block ist ein um ein Jahr verschobener Klon der
Vergangenheit -- mitgezählt würde jeder Betrag doppelt erscheinen, einmal im
Ist und einmal in der Vorschau.

Ausgabe ist eine einzelne HTML-Datei, die NEBEN den Daten landet
(~/Documents/finance/pivot.html), nicht im Repository: sie enthält die
vollständige Buchhaltung, und die hat in git nichts zu suchen. Kein CDN,
kein Netz, keine externe Abhängigkeit im Dokument -- Klappmechanik sind
zwanzig Zeilen JavaScript in der Datei selbst.

Gelesen wird mit import_dkb.read_money_csv und nicht mit pandas.read_csv:
money.csv hat eine namenlose Spalte (Bem), deutsche Beträge mit "€" und
Zeilenumbrüche innerhalb von Textfeldern. Der vorhandene Leser kennt das
alles schon, pandas müsste es nachgebaut bekommen.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
from pathlib import Path

import pandas as pd

try:  # als Paketmodul: python -m finance.pivot
    from . import import_dkb as money_io
except ImportError:  # direkt als Skript aus dem Paketordner gestartet
    import import_dkb as money_io

EMPTY_KAT = "(ohne Kat)"
EMPTY_UKAT = "(ohne UKat)"
MONTH_NAMES = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
               "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")

DEFAULT_TERMINAL_YEARS = 6  # im Terminal passen nicht 23 Jahre nebeneinander


# ---------------------------------------------------------------------------
# Laden und pivotieren
# ---------------------------------------------------------------------------

def load(money_path: Path, from_year: int | None = None,
         to_year: int | None = None) -> tuple[pd.DataFrame, dict]:
    """money.csv -> DataFrame der Ist-Buchungen mit Kat/UKat/Jahr/Monat/Betrag.
    Das zweite Ergebnis sind die Zahlen, die man beim Lesen der Tabelle kennen
    muss: was weggelassen wurde und warum."""
    rows = money_io.read_money_csv(money_path)
    ist = [r for r in rows if r[money_io.MONEY_FLAG_COL] == ""]

    # Kat/UKat werden getrimmt, sonst erscheint "MF " als eigene Kategorie
    # neben "MF". Das ist keine Kosmetik, sondern eine Aggregation: zwei
    # Zeilen derselben Kategorie müssen in derselben Zeile landen. Gemeldet
    # wird es trotzdem -- in money.csv ist es ein Tippfehler, und
    # build_rules macht daraus ein eigenes Tripel.
    df = pd.DataFrame({
        "Kat": [r[money_io.MONEY_KAT_COL].strip() or EMPTY_KAT for r in ist],
        "UKat": [r[money_io.MONEY_UKAT_COL].strip() or EMPTY_UKAT for r in ist],
        "Jahr": [r[money_io.MONEY_YEAR_COL] for r in ist],
        "Monat": [r[money_io.MONEY_MONTH_COL] for r in ist],
        "Betrag": [r[money_io.MONEY_AMOUNT_COL] for r in ist],
    })

    info = {
        "whitespace": _whitespace_variants(ist),
        "n_rows": len(rows),
        "n_ist": len(ist),
        "n_f": len(rows) - len(ist),
        "n_no_amount": int(df["Betrag"].isna().sum()),
        "n_no_kat": int((df["Kat"] == EMPTY_KAT).sum()),
        "n_no_date": int(df["Jahr"].isna().sum() + df["Monat"].isna().sum()),
    }

    df = df.dropna(subset=["Jahr", "Monat"])
    df["Betrag"] = df["Betrag"].fillna(0.0)
    df = df.astype({"Jahr": int, "Monat": int})
    if from_year is not None:
        df = df[df["Jahr"] >= from_year]
    if to_year is not None:
        df = df[df["Jahr"] <= to_year]
    info["n_used"] = len(df)
    return df, info


def _whitespace_variants(ist: list[dict]) -> list[str]:
    """Kat-/UKat-Werte, die sich von einem anderen nur durch Leerzeichen
    unterscheiden. Nach dem Trimmen sieht man sie in der Tabelle nicht mehr,
    also müssen sie im Klartext gemeldet werden."""
    found = []
    for col, label in ((money_io.MONEY_KAT_COL, "Kat"),
                       (money_io.MONEY_UKAT_COL, "UKat")):
        values = {r[col] for r in ist if r[col]}
        for v in sorted(values):
            if v != v.strip() and v.strip() in values:
                n = sum(1 for r in ist if r[col] == v)
                found.append(f"{label}={v!r} ({n}x) neben {v.strip()!r}")
    return found


def pivot(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Die vier Auflösungen, die eine klappbare Tabelle braucht: das Kreuz
    aus (Kat, UKat) x (Jahr, Monat) und die drei Teilsummen darüber. Alle
    drei aus der feinsten Tabelle aggregiert und nicht einzeln aus den
    Rohdaten -- so können sie nicht auseinanderlaufen."""
    fine = df.pivot_table(index=["Kat", "UKat"], columns=["Jahr", "Monat"],
                          values="Betrag", aggfunc="sum", fill_value=0.0)
    # In pandas 2 ist groupby(axis=1) abgekündigt, daher über die Transponierte.
    ukat_year = fine.T.groupby(level="Jahr").sum().T
    return {
        "ukat_month": fine,
        "ukat_year": ukat_year,
        "kat_month": fine.groupby(level="Kat").sum(),
        "kat_year": ukat_year.groupby(level="Kat").sum(),
    }


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------

def de(x: float, cents: bool = True) -> str:
    """Deutsches Zahlenformat. Genau 0 wird zu "" -- eine Pivot-Tabelle ist
    überwiegend leer, und 0,00 in jeder zweiten Zelle macht sie unlesbar."""
    if x is None or pd.isna(x) or round(x, 2) == 0:
        return ""
    digits = 2 if cents else 0
    body = f"{abs(x):,.{digits}f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return ("-" if x < 0 else "") + body


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --line:#dcdcdc; --head:#f4f4f5; --sub:#fafafa;
        --neg:#b3261e; --pos:#1a7f37; --accent:#2f5d8a; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#16181c; --fg:#e6e6e6; --line:#33363d; --head:#1f2228; --sub:#1a1d22;
  --neg:#ff7b72; --pos:#57d364; --accent:#7aa7d4; } }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size:16px; margin:0 0 4px; }
.meta { color:#888; font-size:12px; margin-bottom:12px; }
.bar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
button { font:inherit; padding:4px 10px; border:1px solid var(--line);
         background:var(--head); color:var(--fg); border-radius:6px; cursor:pointer; }
button:hover { border-color:var(--accent); color:var(--accent); }
.wrap { overflow:auto; max-height:calc(100vh - 150px); border:1px solid var(--line);
        border-radius:8px; }
table { border-collapse:separate; border-spacing:0; font-variant-numeric:tabular-nums; }
th, td { padding:3px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
thead th { position:sticky; background:var(--head); z-index:3; text-align:right;
           border-bottom:1px solid var(--line); }
thead tr:first-child th { top:0; }
thead tr:last-child th { top:26px; }
th.rowhead { left:0; z-index:5; text-align:left; }
th.rowhead.ukat { left:74px; }
td.num { text-align:right; }
td.num.neg { color:var(--neg); }
td.num.pos { color:var(--pos); }
th.yhead { cursor:pointer; text-align:center; user-select:none; }
th.yhead:hover { color:var(--accent); }
tr.kat { background:var(--bg); font-weight:600; cursor:pointer; }
tr.kat:hover th.name { color:var(--accent); }
tr.ukat { background:var(--sub); }
tr.total { font-weight:600; background:var(--head); position:sticky; bottom:0; z-index:4; }
th.name { position:sticky; left:0; background:inherit; text-align:left; z-index:2; }
th.name.ind { left:74px; min-width:110px; font-weight:400; }
th.name.kat { min-width:74px; }
td.ytot, th.ytot { border-left:1px solid var(--line); }
td.gtot, th.gtot { border-left:2px solid var(--accent); font-weight:600; }
.hide { display:none; }
.tw { display:inline-block; width:1em; color:#999; }
"""

JS = """
function toggleYear(th) {
  const open = th.classList.toggle('open');
  document.querySelectorAll('.m.y' + th.dataset.year)
          .forEach(e => e.classList.toggle('hide', !open));
  th.colSpan = open ? Number(th.dataset.months) + 1 : 1;
  th.querySelector('.tw').textContent = open ? '\\u25be' : '\\u25b8';
}
function toggleKat(tr) {
  const open = tr.classList.toggle('open');
  document.querySelectorAll('tr.ukat.k' + tr.dataset.kat)
          .forEach(e => e.classList.toggle('hide', !open));
  tr.querySelector('.tw').textContent = open ? '\\u25be' : '\\u25b8';
}
function allYears(open) {
  document.querySelectorAll('th.yhead')
          .forEach(th => { if (th.classList.contains('open') !== open) toggleYear(th); });
}
function allKats(open) {
  document.querySelectorAll('tr.kat')
          .forEach(tr => { if (tr.classList.contains('open') !== open) toggleKat(tr); });
}
"""


def _num_cell(value: float, kind: str, year: int | None, cents: bool) -> str:
    """kind: 'm' Monat (klappbar), 'y' Jahressumme, 'g' Gesamtsumme."""
    cls = ["num"]
    if kind == "m":
        cls += ["m", f"y{year}", "hide"]
    elif kind == "y":
        cls.append("ytot")
    else:
        cls.append("gtot")
    if round(value or 0, 2) < 0:
        cls.append("neg")
    elif round(value or 0, 2) > 0:
        cls.append("pos")
    return f'<td class="{" ".join(cls)}">{de(value, cents)}</td>'


def _row_cells(month_row, year_row, total: float, years, months, cents: bool) -> str:
    out = []
    for y in years:
        for mth in months[y]:
            out.append(_num_cell(month_row.get((y, mth), 0.0), "m", y, cents))
        out.append(_num_cell(year_row.get(y, 0.0), "y", y, cents))
    out.append(_num_cell(total, "g", None, cents))
    return "".join(out)


def render(tables: dict[str, pd.DataFrame], info: dict, title: str,
           cents: bool = True) -> str:
    fine, ukat_year = tables["ukat_month"], tables["ukat_year"]
    kat_month, kat_year = tables["kat_month"], tables["kat_year"]

    # Jahre absteigend: das laufende Jahr steht links, ohne Scrollen.
    # Die Monate innerhalb eines Jahres bleiben aufsteigend (Jan links) --
    # ein Jahr liest man von Januar an, die Jahresreihe vom Jetzt aus.
    years = sorted({y for y, _m in fine.columns}, reverse=True)
    months = {y: sorted(m for yy, m in fine.columns if yy == y) for y in years}
    kats = sorted(kat_year.index)

    # --- Kopf: Jahre, darunter die (zunächst verborgenen) Monate ---
    h1, h2 = [], []
    h1.append('<th class="rowhead" rowspan="2">Kat</th>')
    h1.append('<th class="rowhead ukat" rowspan="2">UKat</th>')
    for y in years:
        h1.append(f'<th class="yhead" data-year="{y}" data-months="{len(months[y])}" '
                  f'colspan="1" onclick="toggleYear(this)">'
                  f'<span class="tw">&#9656;</span>{y}</th>')
        for mth in months[y]:
            h2.append(f'<th class="m y{y} hide">{MONTH_NAMES[mth - 1]}</th>')
        h2.append(f'<th class="ytot">&Sigma;</th>')
    h1.append('<th class="gtot" rowspan="2">Gesamt</th>')

    # --- Körper: je Kat eine Zeile, darunter die verborgenen UKat-Zeilen ---
    body = []
    for i, kat in enumerate(kats):
        body.append(
            f'<tr class="kat k{i}" data-kat="{i}" onclick="toggleKat(this)">'
            f'<th class="name kat" colspan="2"><span class="tw">&#9656;</span>'
            f'{html.escape(str(kat))}</th>'
            + _row_cells(kat_month.loc[kat], kat_year.loc[kat],
                         float(kat_year.loc[kat].sum()), years, months, cents)
            + "</tr>"
        )
        for ukat in sorted(u for k, u in fine.index if k == kat):
            body.append(
                f'<tr class="ukat k{i} hide">'
                f'<th class="name"></th>'
                f'<th class="name ind">{html.escape(str(ukat))}</th>'
                + _row_cells(fine.loc[(kat, ukat)], ukat_year.loc[(kat, ukat)],
                             float(ukat_year.loc[(kat, ukat)].sum()),
                             years, months, cents)
                + "</tr>"
            )

    body.append(
        '<tr class="total"><th class="name" colspan="2">Gesamt</th>'
        + _row_cells(kat_month.sum(), kat_year.sum(),
                     float(kat_year.sum().sum()), years, months, cents)
        + "</tr>"
    )

    meta = (f"{info['n_used']} Ist-Buchungen, {years[-1]}&ndash;{years[0]}. "
            f"{info['n_f']} F-Zeilen der Vorschau übergangen")
    for n, what in ((len(info["whitespace"]), "Kategorien mit Leerzeichen "
                                                "(zusammengefasst, siehe Terminal)"),
                    (info["n_no_kat"], "ohne Kat"),
                    (info["n_no_amount"], "ohne Betrag (als 0 gezählt)"),
                    (info["n_no_date"], "ohne Datum (weggelassen)")):
        if n:
            meta += f", {n} {what}"
    meta += ". Klick auf Kat oder Jahr klappt auf."

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style></head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">{meta}</div>
<div class="bar">
  <button onclick="allKats(true)">Alle Kat auf</button>
  <button onclick="allKats(false)">Alle Kat zu</button>
  <button onclick="allYears(true)">Alle Jahre auf</button>
  <button onclick="allYears(false)">Alle Jahre zu</button>
</div>
<div class="wrap"><table>
<thead><tr>{''.join(h1)}</tr><tr>{''.join(h2)}</tr></thead>
<tbody>{''.join(body)}</tbody>
</table></div>
<script>{JS}</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Terminal-Ansicht
# ---------------------------------------------------------------------------

def print_collapsed(tables: dict[str, pd.DataFrame], n_years: int,
                    cents: bool) -> None:
    """Die zugeklappte Tabelle, Kat x Jahr, für den Blick ohne Browser.
    Nur die letzten `n_years` Jahre -- 23 Spalten passen in kein Terminal."""
    ky = tables["kat_year"]
    shown = list(ky.columns)[-n_years:][::-1]  # jüngstes Jahr links, wie im HTML
    view = ky[shown].copy()
    view["Gesamt"] = ky.sum(axis=1)
    view.loc["Gesamt"] = view.sum()
    print(view.map(lambda v: de(v, cents)).to_string())
    if len(ky.columns) > len(shown):
        print(f"({len(ky.columns) - len(shown)} ältere Jahre nur im HTML)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    money_io.add_test_flag(ap)
    ap.add_argument("money", nargs="?", default="money.csv",
                    help="Dateiname im money-Ordner")
    ap.add_argument("-o", "--out", help="Zieldatei (Default: <finance>/pivot.html)")
    ap.add_argument("--from-year", type=int, help="Jahre davor weglassen")
    ap.add_argument("--to-year", type=int, help="Jahre danach weglassen")
    ap.add_argument("--no-cents", action="store_true",
                    help="auf ganze Euro runden, schmalere Spalten")
    ap.add_argument("--no-open", action="store_true",
                    help="HTML nur schreiben, nicht im Browser öffnen")
    ap.add_argument("--terminal-years", type=int, default=DEFAULT_TERMINAL_YEARS,
                    help="wie viele Jahre die Terminal-Ansicht zeigt")
    args = ap.parse_args()

    fld = money_io.folders(args.test)
    money_path = money_io.resolve_in(fld.money, args.money, ".csv")
    df, info = load(money_path, args.from_year, args.to_year)
    if df.empty:
        print("Keine Ist-Buchungen im gewählten Zeitraum.")
        return

    tables = pivot(df)
    cents = not args.no_cents
    print(f"{money_path.name}: {info['n_used']} Ist-Buchungen "
          f"({info['n_f']} F-Zeilen übergangen)")
    for warning in info["whitespace"]:
        print(f"  Achtung, in money.csv zu korrigieren: {warning}")
    print()
    print_collapsed(tables, args.terminal_years, cents)

    # Neben die Daten, nicht ins Repository: die Datei enthält alles.
    out_path = Path(args.out) if args.out else fld.root / "pivot.html"
    title = f"money.csv — Kat/UKat × Jahr/Monat"
    out_path.write_text(render(tables, info, title, cents), encoding="utf-8")
    print(f"\nGeschrieben nach {out_path}")

    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out_path)], check=False)


if __name__ == "__main__":
    main()
