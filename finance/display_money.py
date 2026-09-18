"""
display_money.py — Kat/UKat als Bänder über die Jahre.

Ein Band je Kat, Jahre von links nach rechts, das laufende Jahr rechts. Die
Dicke eines Bandes ist der Betrag des Jahres: was hereinkommt, stapelt sich
über der Nulllinie, was hinausgeht darunter. Man sieht also nicht nur, wie
groß eine Kategorie ist, sondern wie sich das Konto im Laufe der Jahre
zusammensetzt -- die Gesamtdicke oben ist die Einnahmenseite des Jahres, die
unten die Ausgabenseite, und der Versatz der Nulllinie ist der Saldo.

Eingabe ist ein Zeitraum (--from-year/--to-year) und eine Menge von Kat
(--kat). Ohne --kat nimmt das Programm die --top N größten und faltet den
Rest zu "Andere" zusammen, damit das Bild vollständig bleibt.

Ein Klick auf ein Band klappt es in seine UKat auf. Dabei wird neu gestapelt
statt das Band von innen zu unterteilen, denn unterteilen geht nicht: in gut
einem Fünftel aller Kat-Jahre haben die UKat gemischte Vorzeichen (Kat = +100
aus +150 und -50), und dann ist die Summe der UKat-Dicken größer als die
Dicke der Kat. Beim Aufklappen wandern die UKat deshalb als eigene Posten in
den Stapel: solange alle UKat das Vorzeichen ihrer Kat haben, bleibt die
Hülle exakt dieselbe, und wo sie es nicht tun, ändert sie sich -- das ist
keine Panne, sondern die Auskunft, dass in dieser Kategorie beides steckt.

Farben: acht Töne, mehr gibt die Palette nicht her, ohne dass zwei davon für
Farbenblinde gleich aussehen. Darum die Obergrenze von acht Kat plus dem
grauen "Andere". Die UKat einer Kat bekommen Abstufungen ihres Kat-Tons,
Identität also aus dem Farbton und Unteridentität aus der Helligkeit.

Nur Ist-Buchungen, wie in pivot: der F-Block ist ein verschobener Klon und
würde jeden Betrag doppelt zählen. Gelesen und aggregiert wird mit
pivot.load und pivot.pivot, damit Tabelle und Bänder nicht auseinanderlaufen.

Ausgabe ist eine einzelne HTML-Datei neben ihrer Quelle, money.csv ->
money-baender.html. Nicht im Repository, denn sie enthält die Buchhaltung.
Kein CDN, kein Netz. Das Stapeln steckt im Dokument und nicht hier, weil das
Aufklappen neu stapeln muss: gut zweihundert Zeilen JavaScript statt der
zwanzig in pivot. Das ist der Preis fürs Aufklappen -- ohne es wäre ein
fertiges SVG aus Python weniger Code.
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

try:  # als Paketmodul: python -m finance.display_money
    from . import import_dkb as money_io
    from . import pivot
except ImportError:  # direkt als Skript aus dem Paketordner gestartet
    import import_dkb as money_io
    import pivot

REST_NAME = "Andere"
DEFAULT_TOP = 8
MAX_SLOTS = 8  # so viele kategoriale Töne hat die Palette, siehe Modulkopf


# ---------------------------------------------------------------------------
# Auswahl und Datenaufbereitung
# ---------------------------------------------------------------------------

def global_order(df: pd.DataFrame) -> list[str]:
    """Alle Kat nach Größe, über die ganze Datei und nicht über den gewählten
    Zeitraum gerechnet. Diese Reihenfolge bestimmt Farbe und Platz im Stapel,
    und sie muss vom Zeitraum unabhängig sein: sonst wechselt eine Kat ihre
    Farbe, sobald man --from-year verschiebt, und wer sich "FG ist orange"
    gemerkt hat, liest das nächste Bild falsch."""
    g = df.pivot_table(index="Kat", columns="Jahr", values="Betrag",
                       aggfunc="sum", fill_value=0.0)
    return list(g.abs().sum(axis=1).sort_values(ascending=False).index)


def select_kats(kat_year: pd.DataFrame, wanted: list[str] | None,
                top: int, all_kats: list[str],
                order: list[str]) -> tuple[list[str], list[str]]:
    """Die Kat, die ein Band bekommen, und die, die übrig bleiben.

    Ausgewählt wird nach Größe im Zeitraum -- --top 8 soll die acht größten
    dieses Zeitraums zeigen. Sortiert wird danach aber nach der globalen
    Reihenfolge, und zwar für Farbe und Stapel gemeinsam: die Palette
    garantiert ihre Unterscheidbarkeit nur für benachbarte Farbplätze, also
    müssen benachbarte Bänder auch benachbarte Plätze haben."""
    weight = kat_year.abs().sum(axis=1)
    by_size = [k for k in weight.sort_values(ascending=False).index
               if round(float(weight[k]), 2) != 0]

    if wanted is None:
        chosen = by_size[:top]
    else:
        # Eine Kat, die es gibt, die aber im gewählten Zeitraum nichts
        # gebucht hat, ist nicht unbekannt -- diese beiden Fälle zu
        # verwechseln schickt einen auf die Suche nach einem Tippfehler,
        # den es nicht gibt.
        available = set(kat_year.index)
        unknown = [k for k in wanted if k not in set(all_kats)]
        if unknown:
            raise SystemExit(
                f"Unbekannte Kat: {', '.join(unknown)}\n"
                f"Vorhanden sind: {', '.join(all_kats)}")
        outside = [k for k in wanted if k not in available]
        if outside:
            raise SystemExit(
                f"Im gewählten Zeitraum ohne Buchungen: {', '.join(outside)}\n"
                f"Dort gebucht haben: {', '.join(sorted(available))}")
        # in der Größenordnung, nicht in der Reihenfolge der Kommandozeile
        chosen = [k for k in by_size if k in set(wanted)]
        empty = [k for k in wanted if k not in set(chosen)]
        if empty:
            print(f"  Ohne Buchungen im Zeitraum, daher kein Band: "
                  f"{', '.join(empty)}")

    if len(chosen) > MAX_SLOTS:
        raise SystemExit(
            f"{len(chosen)} Kat gewählt, aber nur {MAX_SLOTS} Farben, die sich "
            f"auch für Farbenblinde unterscheiden. Bitte auf {MAX_SLOTS} kürzen "
            f"(--kat) oder --top {MAX_SLOTS} nehmen; der Rest kommt als "
            f"\"{REST_NAME}\" ins Bild.")
    rank = {k: i for i, k in enumerate(order)}
    return (sorted(chosen, key=lambda k: rank[k]),
            sorted((k for k in by_size if k not in set(chosen)),
                   key=lambda k: rank[k]))


def build_data(tables: dict[str, pd.DataFrame], kats: list[str],
               rest: list[str], with_rest: bool) -> dict:
    """Die Zahlen, die das Dokument zum Stapeln braucht: je Posten eine Reihe
    von Jahreswerten. Das Stapeln selbst passiert im Browser."""
    kat_year, ukat_year = tables["kat_year"], tables["ukat_year"]
    years = [int(y) for y in kat_year.columns]

    def series(row) -> list[float]:
        return [round(float(row[y]), 2) for y in kat_year.columns]

    def side(values: list[float]) -> str:
        """Auf welcher Seite der Nulllinie der Posten zu Hause ist. Jahre ohne
        Buchung erben diese Seite, damit ein Band dort auf Dicke null
        zusammenläuft statt über die Nulllinie zu springen."""
        return "-" if sum(values) < 0 else "+"

    items = []
    for slot, kat in enumerate(kats, start=1):
        values = series(kat_year.loc[kat])
        ukats = []
        for ukat in sorted(u for k, u in ukat_year.index if k == kat):
            uv = series(ukat_year.loc[(kat, ukat)])
            if round(sum(abs(v) for v in uv), 2) == 0:
                continue
            ukats.append({"name": ukat, "values": uv,
                          "total": round(sum(uv), 2), "side": side(uv)})
        items.append({"name": kat, "slot": slot, "values": values,
                      "total": round(sum(values), 2), "side": side(values),
                      # eine einzige UKat aufzuklappen zeigt nur dasselbe Band
                      # mit anderem Namen -- das ist kein Aufklappen
                      "ukats": ukats if len(ukats) > 1 else []})

    if with_rest and rest:
        values = series(kat_year.loc[rest].sum())
        items.append({"name": REST_NAME, "slot": 0, "values": values,
                      "total": round(sum(values), 2), "side": side(values),
                      "ukats": [{"name": k, "values": series(kat_year.loc[k]),
                                 "total": round(float(kat_year.loc[k].sum()), 2),
                                 "side": side(series(kat_year.loc[k]))}
                                for k in rest]})
    return {"years": years, "items": items}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

# Farben aus der validierten Acht-Ton-Palette, je Modus eigene Stufen (nicht
# die hellen Töne im Dunkeln wiederverwendet). --gap ist die Flächenfarbe: die
# Bänder werden damit umrandet, das ist der 2px-Spalt zwischen ihnen und keine
# Kontur.
#
# --s0 ist das Grau für "Andere". Es sieht verkehrt aus -- im Hellen dunkel,
# im Dunklen hell -- und das ist der Punkt: "Andere" liegt im Stapel immer
# außen und grenzt darum je nach --top an einen beliebigen der acht Töne. Ein
# mittleres Grau ist von mehreren davon für Farbenblinde nicht zu
# unterscheiden (gegen Magenta gemessene ΔE 0,9), ein Grau am anderen Ende
# der Helligkeit von allen. Gemessen mit dem Palettenprüfer, nicht geschätzt:
# schlechtestes ΔE 10,1 hell und 11,2 dunkel gegen alle acht Plätze.
LIGHT_VARS = """
  --bg:#fff; --fg:#1a1a1a; --line:#dcdcdc; --head:#f4f4f5; --grid:#e1e0d9;
  --axis:#c3c2b7; --muted:#898781; --accent:#2f5d8a; --gap:#fff;
  --tint:#fff; --shade:#000;
  --s0:#52524e; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
"""

DARK_VARS = """
  --bg:#16181c; --fg:#e6e6e6; --line:#33363d; --head:#1f2228; --grid:#2c2c2a;
  --axis:#383835; --muted:#898781; --accent:#7aa7d4; --gap:#16181c;
  --tint:#e6e6e6; --shade:#000;
  --s0:#b5b5af; --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
"""

# Die dunklen Werte stehen zweimal im Dokument, unter der Systemeinstellung
# und unter data-theme="dark". Einmal genügt nicht: die Medienabfrage allein
# hört auf das Betriebssystem, der Schalter am <html> allein nicht darauf.
# Geschrieben werden sie trotzdem nur einmal, hier.
CSS = (":root {" + LIGHT_VARS + "}\n"
       '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {'
       + DARK_VARS + "} }\n"
       ':root[data-theme="dark"] {' + DARK_VARS + "}\n" + """
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size:16px; margin:0 0 4px; }
.meta { color:var(--muted); font-size:12px; margin-bottom:12px; max-width:70ch; }
.bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
button { font:inherit; padding:4px 10px; border:1px solid var(--line);
         background:var(--head); color:var(--fg); border-radius:6px; cursor:pointer; }
button:hover { border-color:var(--accent); color:var(--accent); }
button[aria-pressed="true"] { border-color:var(--accent); color:var(--accent); }
.legend { display:flex; gap:4px 14px; flex-wrap:wrap; margin:0 0 10px; padding:0;
          list-style:none; }
.legend li { display:flex; align-items:center; gap:6px; cursor:pointer;
             border-radius:6px; padding:2px 6px; margin-left:-6px; }
.legend li:hover { background:var(--head); }
.legend .sw { width:11px; height:11px; border-radius:3px; flex:none; }
.legend .tw { color:var(--muted); font-size:10px; width:1em; }
.legend .amt { color:var(--muted); font-variant-numeric:tabular-nums; }
.frame { border:1px solid var(--line); border-radius:8px; background:var(--bg);
         position:relative; }
svg { display:block; width:100%; height:auto; }
.ribbon { cursor:pointer; }
.ribbon path { stroke:var(--gap); stroke-width:1.5; }
.ribbon:hover path { stroke:var(--fg); stroke-width:1; }
.rlabel { pointer-events:none; font-size:11px; font-weight:600; }
.tick { fill:var(--muted); font-size:10px; font-variant-numeric:tabular-nums; }
.xtick { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.xtick.now { fill:var(--fg); font-weight:600; }
.gridline { stroke:var(--grid); stroke-width:1; }
.zeroline { stroke:var(--axis); stroke-width:1; }
.cross { stroke:var(--fg); stroke-width:1; opacity:.35; }
.axcap { fill:var(--muted); font-size:10px; }
#tip { position:absolute; pointer-events:none; opacity:0; transition:opacity .08s;
       background:var(--bg); border:1px solid var(--line); border-radius:8px;
       padding:7px 9px; font-size:12px; min-width:150px;
       box-shadow:0 2px 10px rgba(0,0,0,.14); z-index:9; }
#tip h4 { margin:0 0 5px; font-size:12px; color:var(--muted); font-weight:600; }
#tip table { border-collapse:collapse; }
#tip td { padding:1px 0; white-space:nowrap; }
#tip td.k { width:14px; }
#tip td.k i { display:block; width:10px; height:2px; border-radius:1px; }
#tip td.n { color:var(--muted); padding-right:10px; }
#tip td.v { text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }
#tip tr.sum td { border-top:1px solid var(--line); padding-top:3px; }
#tip tr.sum td.n { color:var(--fg); }
table.data { border-collapse:collapse; font-variant-numeric:tabular-nums;
             font-size:12px; margin-top:12px; }
table.data th, table.data td { padding:3px 8px; border-bottom:1px solid var(--line);
                               white-space:nowrap; text-align:right; }
table.data th.n, table.data td.n { text-align:left; }
table.data thead th { background:var(--head); position:sticky; top:0; }
table.data tr.sum td, table.data tr.sum th { font-weight:600; background:var(--head); }
.tablewrap { overflow:auto; max-height:60vh; border:1px solid var(--line);
             border-radius:8px; }
.hide { display:none; }
""")

JS = r"""
const YEARS = DATA.years, ITEMS = DATA.items;
const M = {top: 16, right: 18, bottom: 30, left: 62};
const H = 460;
const expanded = new Set();
const nf0 = new Intl.NumberFormat('de-DE', {maximumFractionDigits: 0});
const svg = document.getElementById('chart');
const tip = document.getElementById('tip');
const NS = 'http://www.w3.org/2000/svg';

function el(name, attrs) {
  const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function eur(v) { return nf0.format(Math.round(v)) + ' €'; }

// Die Farbe eines Postens: die Kat ihren Ton, die UKat Abstufungen davon --
// heller bis dunkler in alphabetischer und damit stabiler Reihenfolge, nicht
// nach Größe (sonst wäre die Helligkeit eine zweite Darstellung des Betrags).
function shade(slot, j, n) {
  const base = 'var(--s' + slot + ')';
  if (n <= 1) return base;
  const p = -28 + 56 * (j / (n - 1));
  const target = p < 0 ? 'var(--tint)' : 'var(--shade)';
  return 'color-mix(in oklab, ' + base + ', ' + target + ' ' + Math.abs(p).toFixed(0) + '%)';
}

// Die sichtbaren Posten in Stapelreihenfolge: eine zugeklappte Kat ist einer,
// eine aufgeklappte liefert ihre UKat an genau ihrer Stelle.
function leaves() {
  const out = [];
  for (const it of ITEMS) {
    if (expanded.has(it.name) && it.ukats.length) {
      it.ukats.forEach((u, j) => out.push({
        name: u.name, parent: it.name, values: u.values, total: u.total,
        side: u.side, color: shade(it.slot, j, it.ukats.length)
      }));
    } else {
      out.push({
        name: it.name, parent: null, values: it.values, total: it.total,
        side: it.side, color: shade(it.slot, 0, 1), item: it
      });
    }
  }
  return out;
}

// Stapeln: je Jahr wachsen die positiven Werte von der Null nach oben und die
// negativen nach unten. Ein Jahr ohne Buchung bekommt Dicke null an der
// Stelle, an der der Posten gerade steht -- so laeuft das Band dort zusammen
// und springt nicht auf die andere Seite.
function stack(ls) {
  const bands = ls.map(() => []);
  const up = [], down = [];
  YEARS.forEach((y, i) => {
    let pos = 0, neg = 0;
    ls.forEach((l, k) => {
      const v = l.values[i];
      if (v > 0) { bands[k].push([pos, pos + v]); pos += v; }
      else if (v < 0) { bands[k].push([neg + v, neg]); neg += v; }
      else { const c = l.side === '-' ? neg : pos; bands[k].push([c, c]); }
    });
    up.push(pos); down.push(neg);
  });
  return {bands, max: Math.max(0, ...up), min: Math.min(0, ...down)};
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * p) return m * p;
  return 10 * p;
}

// Glaettung ueber die Mittelpunkte: die Kurve bleibt zwischen je zwei
// Datenpunkten und kann darum nicht ueber ein Nachbarband hinausschwingen.
function smooth(pts, cmd) {
  if (pts.length < 3) return pts.map((p, i) => (i ? 'L' : cmd) + p[0] + ',' + p[1]).join(' ');
  let d = cmd + pts[0][0] + ',' + pts[0][1];
  for (let i = 1; i < pts.length - 1; i++) {
    const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2;
    d += ' Q' + pts[i][0] + ',' + pts[i][1] + ' ' + mx + ',' + my;
  }
  const n = pts.length - 1;
  return d + ' Q' + pts[n][0] + ',' + pts[n][1] + ' ' + pts[n][0] + ',' + pts[n][1];
}

let X = [], geom = null;

function draw() {
  const W = Math.max(520, svg.clientWidth || svg.parentNode.clientWidth || 900);
  const plotW = W - M.left - M.right, plotH = H - M.top - M.bottom;
  const ls = leaves(), st = stack(ls);
  const span = (st.max - st.min) || 1;
  const y = v => M.top + (st.max - v) * (plotH / span);
  X = YEARS.map((_, i) => YEARS.length === 1 ? M.left + plotW / 2
    : M.left + i * (plotW / (YEARS.length - 1)));

  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.replaceChildren();

  // Raster und Nulllinie zuerst, damit die Baender darueber liegen
  const step = niceStep(span / 6);
  const g = el('g', {});
  for (let v = Math.ceil(st.min / step) * step; v <= st.max + 1e-9; v += step) {
    const yy = y(v).toFixed(1);
    if (Math.abs(v) >= 1e-9)
      g.appendChild(el('line', {class: 'gridline',
                                x1: M.left, x2: M.left + plotW, y1: yy, y2: yy}));
    const t = el('text', {class: 'tick', x: M.left - 8, y: yy, 'text-anchor': 'end',
                          'dominant-baseline': 'middle'});
    t.textContent = Math.abs(st.max) >= 20000 ? nf0.format(v / 1000) : nf0.format(v);
    g.appendChild(t);
  }
  const cap = el('text', {class: 'axcap', x: M.left - 8, y: M.top - 4, 'text-anchor': 'end'});
  cap.textContent = Math.abs(st.max) >= 20000 ? 'T€' : '€';
  g.appendChild(cap);
  svg.appendChild(g);

  // Baender
  const bands = el('g', {});
  const labels = [];
  ls.forEach((l, k) => {
    const b = st.bands[k];
    const top = X.map((x, i) => [+x.toFixed(1), +y(b[i][1]).toFixed(1)]);
    const bot = X.map((x, i) => [+x.toFixed(1), +y(b[i][0]).toFixed(1)]).reverse();
    const grp = el('g', {class: 'ribbon'});
    // Der Spalt zwischen zwei Baendern ist ein Strich in Flaechenfarbe. Bei
    // duennen Baendern muss er mitschrumpfen, sonst frisst er das Band auf.
    let thick = 0;
    for (let i = 0; i < b.length; i++) thick = Math.max(thick, y(b[i][0]) - y(b[i][1]));
    grp.appendChild(el('path', {
      d: smooth(top, 'M') + ' ' + smooth(bot, 'L') + ' Z',
      fill: l.color, 'fill-opacity': .88,
      'stroke-width': Math.min(1.5, thick / 3).toFixed(2)
    }));
    if (l.item && l.item.ukats.length) {
      grp.addEventListener('click', () => {
        expanded.has(l.name) ? expanded.delete(l.name) : expanded.add(l.name);
        draw(); legend();
      });
    } else if (l.parent) {
      grp.addEventListener('click', () => { expanded.delete(l.parent); draw(); legend(); });
    }
    const t = el('title', {});
    t.textContent = l.name + ' — ' + eur(l.total) +
      (l.item && l.item.ukats.length ? ' (Klick: UKat)' : l.parent ? ' (Klick: zu)' : '');
    grp.appendChild(t);
    bands.appendChild(grp);

    // Direkte Beschriftung im dicksten Jahr, aber nur wo der Name wirklich
    // hineinpasst -- abgeschnittene Schrift ist schlechter als keine, den
    // Rest tragen Legende, Tooltip und Tabelle.
    let best = 0;
    for (let i = 1; i < b.length; i++)
      if (b[i][1] - b[i][0] > b[best][1] - b[best][0]) best = i;
    const h = y(b[best][0]) - y(b[best][1]);
    const w = textWidth(l.name);
    if (h >= 13 && X[best] - w / 2 > M.left + 2 && X[best] + w / 2 < M.left + plotW - 2)
      labels.push({name: l.name, x: X[best], y: y(b[best][1]) + h / 2, w: w, h: h});
  });

  // Die dicksten Baender beschriften zuerst; was sich mit einer schon
  // gesetzten Beschriftung ueberschneidet, bleibt weg. Zwei uebereinander
  // gedruckte Namen sind schlechter zu lesen als einer, und Legende,
  // Tooltip und Tabelle nennen ohnehin jeden Posten.
  const taken = [];
  labels.sort((a, b2) => b2.h - a.h);
  for (const L of labels) {
    const box = [L.x - L.w / 2 - 3, L.y - 8, L.x + L.w / 2 + 3, L.y + 8];
    if (taken.some(t => box[0] < t[2] && t[0] < box[2] && box[1] < t[3] && t[1] < box[3]))
      continue;
    taken.push(box);
    const lab = el('text', {
      class: 'rlabel', x: L.x, y: L.y.toFixed(1),
      'text-anchor': 'middle', 'dominant-baseline': 'middle',
      fill: '#fff', stroke: 'rgba(0,0,0,.55)', 'stroke-width': '2.5',
      style: 'paint-order:stroke'
    });
    lab.textContent = L.name;
    bands.appendChild(lab);
  }
  svg.appendChild(bands);

  // Die Nulllinie zuletzt, sonst verdecken sie die Bänder -- und sie ist die
  // Linie, auf die man in diesem Bild als erstes schaut.
  svg.appendChild(el('line', {class: 'zeroline', x1: M.left, x2: M.left + plotW,
                              y1: y(0).toFixed(1), y2: y(0).toFixed(1)}));

  // Jahre unten; das letzte Jahr ist hervorgehoben, es ist das laufende
  const xs = el('g', {});
  const everyN = Math.ceil(YEARS.length / Math.max(Math.floor(plotW / 42), 1));
  YEARS.forEach((yr, i) => {
    if (i % everyN && i !== YEARS.length - 1) return;
    const t = el('text', {class: 'xtick' + (i === YEARS.length - 1 ? ' now' : ''),
                          x: X[i], y: H - M.bottom + 16, 'text-anchor': 'middle'});
    t.textContent = yr;
    xs.appendChild(t);
  });
  svg.appendChild(xs);

  const cross = el('line', {class: 'cross hide', id: 'cross',
                            y1: M.top, y2: M.top + plotH});
  svg.appendChild(cross);
  geom = {ls, st, y, plotW, plotH, W};

}

let _ctx = null;
function textWidth(s) {
  if (!_ctx) {
    _ctx = document.createElement('canvas').getContext('2d');
    _ctx.font = '600 11px system-ui, -apple-system, sans-serif';
  }
  return _ctx.measureText(s).width;
}

// Ein Tooltip fuer alle Posten des Jahres: der Zeiger muss nie ein Band
// treffen, nur ein Jahr.
function onMove(ev) {
  if (!geom) return;
  const r = svg.getBoundingClientRect();
  const sx = (ev.clientX - r.left) * (geom.W / r.width);
  let i = 0;
  for (let k = 1; k < X.length; k++) if (Math.abs(X[k] - sx) < Math.abs(X[i] - sx)) i = k;
  const cross = document.getElementById('cross');
  cross.classList.remove('hide');
  cross.setAttribute('x1', X[i]); cross.setAttribute('x2', X[i]);

  const rows = geom.ls.map((l, k) => ({l, v: l.values[i]}))
    .filter(o => Math.round(o.v * 100) !== 0)
    .sort((a, b) => Math.abs(b.v) - Math.abs(a.v));
  const head = document.createElement('h4');
  head.textContent = YEARS[i];
  const tbl = document.createElement('table');
  for (const o of rows.slice(0, 14)) {
    const tr = tbl.insertRow();
    const k = tr.insertCell(); k.className = 'k';
    const key = document.createElement('i'); key.style.background = o.l.color;
    k.appendChild(key);
    const n = tr.insertCell(); n.className = 'n';
    n.textContent = o.l.parent ? o.l.parent + ' / ' + o.l.name : o.l.name;
    const v = tr.insertCell(); v.className = 'v'; v.textContent = eur(o.v);
  }
  const tr = tbl.insertRow(); tr.className = 'sum';
  tr.insertCell().className = 'k';
  const n = tr.insertCell(); n.className = 'n'; n.textContent = 'Saldo';
  const v = tr.insertCell(); v.className = 'v';
  v.textContent = eur(rows.reduce((s, o) => s + o.v, 0));
  tip.replaceChildren(head, tbl);
  tip.style.opacity = 1;
  // Am rechten Rand kippt das Faehnchen nach links, statt sich an den Rand
  // zu druecken -- sonst deckt es genau die Linie zu, die es erklaert.
  const px = X[i] * (r.width / geom.W);
  const w = tip.offsetWidth;
  const left = (px + 14 + w > r.width - 4) ? px - 14 - w : px + 14;
  tip.style.left = Math.max(4, Math.min(left, r.width - w - 4)) + 'px';
  tip.style.top = Math.max(4, Math.min(ev.clientY - r.top - 10,
                                       r.height - tip.offsetHeight - 4)) + 'px';
}
function onLeave() {
  tip.style.opacity = 0;
  const c = document.getElementById('cross');
  if (c) c.classList.add('hide');
}

function legend() {
  const ul = document.getElementById('legend');
  ul.replaceChildren();
  for (const it of ITEMS) {
    const li = document.createElement('li');
    const tw = document.createElement('span');
    tw.className = 'tw';
    tw.textContent = it.ukats.length ? (expanded.has(it.name) ? '▾' : '▸') : '';
    const sw = document.createElement('span');
    sw.className = 'sw'; sw.style.background = shade(it.slot, 0, 1);
    const nm = document.createElement('span'); nm.textContent = it.name;
    const am = document.createElement('span');
    am.className = 'amt'; am.textContent = eur(it.total);
    li.append(tw, sw, nm, am);
    if (it.ukats.length) {
      li.title = it.ukats.length + ' UKat -- Klick klappt auf';
      li.addEventListener('click', () => {
        expanded.has(it.name) ? expanded.delete(it.name) : expanded.add(it.name);
        draw(); legend();
      });
    }
    ul.appendChild(li);
  }
}

function collapseAll() { expanded.clear(); draw(); legend(); }
function toggleTable(btn) {
  const t = document.getElementById('tablewrap');
  const on = t.classList.toggle('hide');
  btn.setAttribute('aria-pressed', String(!on));
}

svg.addEventListener('pointermove', onMove);
svg.addEventListener('pointerleave', onLeave);
addEventListener('resize', draw);
draw(); legend();
"""


def _table_view(data: dict) -> str:
    """Die Tabelle zu den Bändern: dieselben Zahlen ohne Farbe, für den Fall,
    dass die Farben nicht reichen -- und das ist bei drei der acht Töne auf
    weißem Grund so."""
    years = data["years"]
    head = "".join(f"<th>{y}</th>" for y in years)
    rows = []
    for it in data["items"]:
        cells = "".join(f"<td>{pivot.de(v, False)}</td>" for v in it["values"])
        rows.append(f'<tr><th class="n">{html.escape(it["name"])}</th>{cells}'
                    f'<td>{pivot.de(it["total"], False)}</td></tr>')
        for u in it["ukats"]:
            cells = "".join(f"<td>{pivot.de(v, False)}</td>" for v in u["values"])
            rows.append(f'<tr><th class="n">&nbsp;&nbsp;{html.escape(u["name"])}</th>'
                        f'{cells}<td>{pivot.de(u["total"], False)}</td></tr>')
    totals = [sum(it["values"][i] for it in data["items"]) for i in range(len(years))]
    cells = "".join(f"<td>{pivot.de(v, False)}</td>" for v in totals)
    rows.append(f'<tr class="sum"><th class="n">Saldo</th>{cells}'
                f'<td>{pivot.de(sum(totals), False)}</td></tr>')
    return (f'<div class="tablewrap hide" id="tablewrap"><table class="data">'
            f'<thead><tr><th class="n">Kat / UKat</th>{head}<th>Gesamt</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render(data: dict, info: dict, title: str, meta_extra: str = "") -> str:
    years = data["years"]
    meta = (f"{info['n_used']} Ist-Buchungen, {years[0]}&ndash;{years[-1]}. "
            f"Dicke ist der Jahresbetrag, über der Nulllinie die Einnahmen, "
            f"darunter die Ausgaben. Klick auf ein Band oder einen "
            f"Legendeneintrag klappt die UKat auf -- am besten eine Kat auf "
            f"einmal, alle zusammen sind zu viele Bänder für ein Bild.")
    if meta_extra:
        meta += " " + meta_extra
    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style></head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">{meta}</div>
<div class="bar">
  <button onclick="collapseAll()">Alle UKat zu</button>
  <button aria-pressed="false" onclick="toggleTable(this)">Tabelle</button>
</div>
<ul class="legend" id="legend"></ul>
<div class="frame">
  <svg id="chart" role="img" aria-label="{html.escape(title)}"></svg>
  <div id="tip" role="status"></div>
</div>
{_table_view(data)}
<script>const DATA = {json.dumps(data, ensure_ascii=False)};{JS}</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Terminal-Ansicht
# ---------------------------------------------------------------------------

def print_table(data: dict, n_years: int) -> None:
    """Kat x Jahr für den Blick ohne Browser, die jüngsten Jahre rechts wie
    im Bild."""
    years = data["years"][-n_years:]
    off = len(data["years"]) - len(years)
    width = max(12, *(len(it["name"]) for it in data["items"]))
    print(" " * width + "".join(f"{y:>12}" for y in years) + f"{'Gesamt':>14}")
    for it in data["items"]:
        cells = "".join(f"{pivot.de(v, False):>12}" for v in it["values"][off:])
        print(f"{it['name']:<{width}}{cells}{pivot.de(it['total'], False):>14}")
    totals = [sum(it["values"][i] for it in data["items"])
              for i in range(len(data["years"]))]
    cells = "".join(f"{pivot.de(v, False):>12}" for v in totals[off:])
    print(f"{'Saldo':<{width}}{cells}{pivot.de(sum(totals), False):>14}")
    if off:
        print(f"({off} ältere Jahre nur im HTML)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    money_io.add_test_flag(ap)
    ap.add_argument("money", nargs="?", default="money.csv",
                    help="Dateiname im money-Ordner")
    ap.add_argument("-o", "--out",
                    help="Zieldatei (Default: neben der Quelle, "
                         "money.csv -> money-baender.html)")
    ap.add_argument("--from-year", type=int, help="Jahre davor weglassen")
    ap.add_argument("--to-year", type=int, help="Jahre danach weglassen")
    ap.add_argument("--kat", nargs="+", metavar="KAT",
                    help=f"welche Kat ein Band bekommen (max. {MAX_SLOTS}); "
                         f"Default sind die --top größten")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help=f"ohne --kat: die N größten Kat (Default {DEFAULT_TOP})")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--andere", action="store_true",
                     help=f"auch mit --kat den Rest als \"{REST_NAME}\" zeigen")
    grp.add_argument("--no-andere", action="store_true",
                     help=f"\"{REST_NAME}\" weglassen, nur die gewählten Kat")
    ap.add_argument("--terminal-years", type=int, default=6,
                    help="wie viele Jahre die Terminal-Ansicht zeigt")
    ap.add_argument("--no-open", action="store_true",
                    help="HTML nur schreiben, nicht im Browser öffnen")
    args = ap.parse_args()

    fld = money_io.folders(args.test)
    money_path = money_io.resolve_in(fld.money, args.money, ".csv")
    df, info = pivot.load(money_path)
    all_kats = sorted(df["Kat"].unique())
    order = global_order(df)  # vor dem Jahresfilter, damit Farben stabil sind
    if args.from_year is not None:
        df = df[df["Jahr"] >= args.from_year]
    if args.to_year is not None:
        df = df[df["Jahr"] <= args.to_year]
    info["n_used"] = len(df)
    if df.empty:
        print("Keine Ist-Buchungen im gewählten Zeitraum.")
        return

    if df["Jahr"].nunique() < 2:
        print(f"Nur das Jahr {int(df['Jahr'].iloc[0])} im Zeitraum. Ein Band "
              f"braucht mindestens zwei Jahre, sonst hat es keine Länge -- "
              f"für ein einzelnes Jahr ist pivot die richtige Ansicht.")
        return

    tables = pivot.pivot(df)
    kats, rest = select_kats(tables["kat_year"], args.kat, args.top, all_kats,
                             order)
    if not kats:
        print("Keine Kat mit Buchungen im gewählten Zeitraum.")
        return
    # Ohne --kat ist "Andere" der Rest eines vollständigen Bildes und gehört
    # dazu; mit --kat hat man sich entschieden und will nur die Auswahl.
    with_rest = args.andere or (args.kat is None and not args.no_andere)

    data = build_data(tables, kats, rest, with_rest)
    print(f"{money_path.name}: {info['n_used']} Ist-Buchungen "
          f"({info['n_f']} F-Zeilen übergangen)")
    for warning in info["whitespace"]:
        print(f"  Achtung, in money.csv zu korrigieren: {warning}")
    if rest and not with_rest:
        print(f"  {len(rest)} weitere Kat nicht im Bild: {', '.join(rest)}")
    print()
    print_table(data, args.terminal_years)

    out_path = (Path(args.out) if args.out
                else money_path.with_name(money_path.stem + "-baender.html"))
    title = "money.csv — Kat/UKat als Bänder über die Jahre"
    extra = ""
    last = data["years"][-1]
    months = sorted(df[df["Jahr"] == last]["Monat"].unique())
    if len(months) < 12:
        extra = (f"{last} ist ein Rumpfjahr: Buchungen bis "
                 f"{pivot.MONTH_NAMES[max(months) - 1]}.")
    out_path.write_text(render(data, info, title, extra), encoding="utf-8")
    print(f"\nGeschrieben nach {out_path}")

    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out_path)], check=False)


if __name__ == "__main__":
    main()
