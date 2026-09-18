# Finance

Buchhaltung für das eigene Girokonto: aus dem monatlichen DKB-Kontoauszug wird
eine kategorisierte Zeile in `money.csv`, plus ein um ein Jahr in die Zukunft
verschobener Klon für die rollende 12-Monats-Vorschau. Nachfolger des früheren
Google-Apps-Script-Menüs "import".

Die Kategorien schlägt das Programm aus der eigenen Historie vor -- `money.csv`
ist die Quelle der Wahrheit, die Regeln sind ein daraus abgeleiteter Cache.
Korrigiert wird von Hand, und jede Korrektur verbessert die nächsten Vorschläge.

## Einrichtung

Alle Aufrufe unten benutzen den Interpreter des Projekt-venv und sind aus dem
Repository-Wurzelverzeichnis gedacht. Das ist derselbe Interpreter, den
PyCharm benutzt -- so sehen Editor und Terminal dieselben Pakete.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Einzige externe Abhängigkeit ist pandas, und die braucht nur `finance.pivot`;
die vier anderen Programme laufen mit der Standardbibliothek. Getestet mit
3.11 und 3.13; `python3` ist nicht immer das, was man denkt, daher der
ausgeschriebene Interpreter.

## Monatlicher Ablauf

```bash
# 1. Umsatzliste bei der DKB herunterladen, nach umsatzlisten/ legen

# 2. Regeln aus der Historie neu berechnen
.venv/bin/python -m finance.build_rules

# 3. Vorschlagsdatei erzeugen: suggestions/<auszug>.suggestion.csv
.venv/bin/python -m finance.categorize_import

# 4. Kat/UKat/Bem korrigieren (am besten in PyCharm); Excel, Numbers o.ä können Probleme mit Sonderzeichen oder Delimitern verursachen.

# 5. Die korrigierte Datei in money.csv eintragen
.venv/bin/python -m finance.import_dkb
```

Ohne Argumente nimmt jedes Programm die jeweils neueste Datei im vorgesehenen
Ordner; `--help` zeigt, wie man eine andere wählt.
`.venv/bin/python -m finance.import_dkb --dry-run` zeigt, was passieren
würde, ohne zu schreiben.

## Datenablage

Die Daten liegen außerhalb des Repositorys, unter `~/Documents/finance`:

```
finance/
  money/money.csv                  die Buchhaltung selbst
  money/money.html                 Pivot-Ansicht, von finance.pivot erzeugt
  money/money-baender.html         Baender-Ansicht, von finance.display_money
  umsatzlisten/DD-MM-YYYY_...csv   Kontoauszüge, wie von der DKB geliefert
  rules/rules.json                 abgeleiteter Regel-Cache
  suggestions/....suggestion.csv   Vorschläge zum Korrigieren
```

`--test` schaltet den ganzen Baum auf `~/Documents/finance-test` um, eine Kopie
von `finance`. Dort lassen sich vergangene Jahre gefahrlos nachspielen, bevor
etwas die echte Buchhaltung anfasst.

## Die drei Programme des Monatslaufs

### `build_rules`

`money.csv` + Zeitraum (Default 36 Monate) → `rules.json`.

Ausgewertet werden nur Ist-Buchungen mit gefüllter Kat -- frisch importierte,
noch nicht korrigierte Zeilen sollen die Regeln nicht verfälschen. Regeln
stehen in Stufen, von präzise nach grob abgefragt:

| Stufe | Schlüssel | wofür |
|---|---|---|
| `paypal_submerchant` | Händler aus dem Verwendungszweck | bei PayPal ist der Empfänger immer "PayPal Europe ..."; der echte Händler steht im Zweck |
| `zweck` | Empfänger + normalisierter Verwendungszweck | trennt gleiche Empfänger mit mehreren Sachverhalten |
| `empf` | Empfänger allein | Auffangnetz |

Die Stufe `zweck` ist der Grund, warum die Gemeinde Tuntenhausen funktioniert:
sie bucht Grundsteuer, Wasser, Abwasser und Abfall für zwei Häuser über
denselben Empfänger und dasselbe SEPA-Mandat -- unterscheidbar allein am
Verwendungszweck. Gemessen (Training bis 2025, Test 2026, nur Zeilen mit
bekanntem Empfänger):

| | Empfänger allein | mit Verwendungszweck |
|---|---|---|
| Kat | 73,2 % | **81,2 %** |
| Kat + UKat | 68,1 % | **75,2 %** |
| Kat + UKat + Bem | 48,2 % | 54,8 % |

Bem ist freier Text und deckelt deshalb das Tripel.

Der Normalisierer des Verwendungszwecks ist bewusst konservativ: er entfernt
nur Datumsangaben und Jahreszahlen (die machen jeden Zweck einmalig und damit
nutzlos). "Lerchenweg 19 A" und "Lerchenweg 13" sind zwei verschiedene Häuser,
"VZ" und "VA" zwei verschiedene Sachverhalte -- beides bleibt stehen.

Eine neue Stufe ist eine Schlüsselfunktion `Booking -> str | None`, die vor die
bestehenden gehängt wird; sie darf sich auf beliebige externe Quellen stützen
(z.B. eine aus Amazon-Mails gebaute Tabelle). `Booking` ist der gemeinsame
Nenner von `money.csv`-Zeile und DKB-Zeile -- ohne diese Brücke müsste jede
Stufe zweimal geschrieben werden, einmal je Spaltenschema.

#### Konfidenz: was eine Regel verspricht

Jedes Feld jeder Regel trägt ein `p` -- die untere Schranke eines einseitigen
95%-Kredibilitätsintervalls der Posteriori `Beta(count+1, total-count+1)`,
also "mit 95 % Sicherheit trifft diese Regel in mindestens p der Fälle".

Der rohe Anteil `count/total` kann das nicht leisten, weil er Belegmenge nicht
von Einigkeit unterscheidet: 2/2 und 133/135 sind beide "einig", aber nur das
zweite ist ein Versprechen. Die Schranke trennt sie, weil sie mit der
Belegmenge wächst:

| Belege | 1/1 | 2/2 | 5/5 | 10/10 | 20/20 | 133/135 |
|---|---|---|---|---|---|---|
| `p` | 0,22 | 0,37 | 0,61 | 0,76 | 0,87 | 0,95 |

Daraus die Labels -- `hoch` verlangt jetzt ~20 Belege, nicht mehr zwei:

| `p` | Label | gemessene Kat-Trefferquote |
|---|---|---|
| >= 0,85 | `hoch` | 99,2 % |
| >= 0,60 | `mittel` | 96,1 % |
| >= 0,35 | `niedrig` | 90,7 % |
| < 0,35 | `unklar` | 56,2 % |

Die unscharfe Stufe hat kein `p` und trägt deshalb ihr eigenes Label
`unscharf` (67,7 %) -- mit "niedrig" in einen Topf geworfen wäre die Spalte
wieder so unbrauchbar wie vorher.

**Unterdrückt wird nichts.** Auch unterhalb von 0,35 wird vorgeschlagen, das
Label heißt dann `unklar`. Ein Vorschlag, der in 56 % der Fälle stimmt, ist
mehr wert als ein leeres Feld, solange danebensteht, dass man ihm nicht
glauben soll: korrigieren geht schneller als tippen. Das Urteil fällt beim
Durchsehen, nicht im Programm.

Dass 0,35 die richtige Stelle für den Schnitt ist, sagt Tabelle 4 von
`evaluate`: dort trennt das Label am schärfsten, 56,2 % darunter gegen
95,4 % darüber, ein Abstand von 39 Prozentpunkten. Bei 0,45 sind es nur noch
27, bei 0,85 knapp 17.

**Je Feld, nicht je Tripel.** `p` wird für Kat, Kat+UKat und das Tripel
getrennt gerechnet, denn ein Schlüssel kann eine völlig sichere Kat und eine
hoffnungslose Bem haben -- bei freiem Text ist das der Normalfall. Beispiel
aus dem echten Auszug: `Siedersleben,Johannes,Prof.Dr.`, 54 Belege,
Kat 0,95 / UKat 0,73 / Bem 0,12. Alle drei Felder werden gefüllt, aber das
Label der Zeile kommt aus der Kat -- ein `hoch` kann also eine Bem
enthalten, die auf `p` = 0,12 ruht. Die drei `p` stehen deshalb einzeln in
der Spalte `Quelle`; wer Bem prüfen will, liest dort nach.

**Belegmenge schlägt in beide Richtungen aus.** `Johannes Siedersleben` hat
267 Belege und trotzdem `p` = 0,32, weil es eigene Umbuchungen in viele
Kategorien sind: `unklar`. Die Stufe `zweck` fängt daraus die eindeutigen
Fälle wieder auf (eine bestimmte Zweck-Signatur, 4 Belege, `p` = 0,55).

Die Grenze geht deshalb in die *Stufenauswahl* ein und nicht erst hinter sie:
genommen wird die erste Stufe, die sie besteht. Sonst verdeckte eine
dünn belegte feine Regel eine dicht belegte grobe und man bekäme ein
`unklar`, wo die Historie eindeutig ist. Nach dem höchsten `p`
auszuwählen wäre der naheliegende nächste Schritt und ist gemessen schlechter
(85,0 % gegen 86,5 %) -- `p` kennt nur Häufigkeiten, dass eine Zweck-Signatur
den Sachverhalt schärfer fasst als der Empfänger allein steht in keinem
Zähler.

### `categorize_import`

Umsatzliste + `rules.json` → Vorschlagsdatei, in der Spaltenstruktur von
`money.csv`, mit vorgeschlagenen Kat/UKat/Bem. Schreibt nichts in `money.csv`.

`MF`, `MT` und `Saldo` bleiben leer -- die entstehen erst beim Eintragen.
Hinten stehen zwei zusätzliche Spalten, `Konfidenz` und `Quelle`: sie sagen,
welcher Vorschlag auf 133 gleichen Buchungen beruht und welcher auf geratener
Tokenüberlappung. `import_dkb` ignoriert sie, `--plain` lässt sie weg.
`Quelle` nennt Stufe, Schlüssel, Belegzahl und die drei `p` -- beim Korrigieren
sagt das, ob eine leere Kat an dünner Historie oder an Uneinigkeit liegt.

Nach den Regelstufen folgt als letztes Netz eine unscharfe Stufe, die direkt
gegen `money.csv` läuft (Tokenüberlappung über Empfänger + Verwendungszweck).
Sie liefert nur "niedrig" -- explizit zum Gegenlesen. `--no-fuzzy` schaltet sie
ab.

### `import_dkb`

Korrigierte Vorschlagsdatei → `money.csv`. Jede neue Buchung wird zweimal
eingetragen: als Ist-Zeile und als F-Zeile ein Jahr später. Der damit obsolete
Teil des alten F-Blocks fällt weg, MT und Saldo werden für alles darüber
fortgeschrieben.

Die Vorschlagsdatei kann aus einem Tabellenprogramm zurückkommen, also
toleriert der Leser, was Excel & Co. anrichten: eine Vorlaufzeile über der Kopfzeile, `;` oder `,` als Trenner,
zweistellige Jahre, Beträge mit oder ohne `€`. Was Excel nicht darf: die Spalte
`MF` füllen oder Datum bzw. Betrag leeren -- dann bricht der Import mit
Zeilennummer ab.

`money.csv` wird mit seiner ursprünglichen Kopfzeile zurückgeschrieben, Beträge
im Format `-9.198,34 €`. Sonst formatiert jeder Import die ganze Datei um und
der Diff einer 15.000-Zeilen-Datei ist nicht mehr lesbar.

Dedup: Der neue Auszug überlappt mit dem letzten Import. Alles vor dem Grenztag
(dem Buchungsdatum der jüngsten Ist-Zeile) ist erfasst, alles danach ist neu,
und genau am Grenztag wird über Betragsgleichheit entschieden -- als
Multimenge, damit mehrere gleich hohe Buchungen am selben Tag nicht zu einer
zusammenfallen. Denselben Auszug zweimal zu importieren ergibt 0 neue
Buchungen.

## money.csv

| Spalte | Bedeutung |
|---|---|
| `MF` | leer = Ist-Buchung, `F` = Vorschau |
| `MT` | laufende Nummer, von unten nach oben um 1 steigend |
| `BuDat`, `Jahr`, `Monat` | Buchungsdatum, `TT.MM.JJJJ` |
| `Kat`, `UKat` | Kategorie und Unterkategorie |
| *(ohne Namen)* | Bemerkung; im Code `Bem`, per Positionsanker zwischen `UKat` und `Betrag` erkannt |
| `Betrag`, `Saldo` | deutsches Format mit `€` |
| `Empfänger`, `Text` | Zahlungsempfänger und Verwendungszweck |

Die Datei ist absteigend nach Datum sortiert: oben der F-Block, darunter die
Ist-Buchungen. `Saldo[i] = Betrag[i] + Saldo[i+1]`.

## `pivot` -- hinsehen, was da eigentlich steht

```bash
.venv/bin/python -m finance.pivot                  # HTML schreiben und oeffnen
.venv/bin/python -m finance.pivot --from-year 2020 # nur die letzten Jahre
.venv/bin/python -m finance.pivot --no-cents --no-open
```

Zeilen Kat/UKat, Spalten Jahr/Monat, Werte die Betragssummen -- beide Achsen
einzeln auf- und zuklappbar: ein Klick auf eine Kat zeigt ihre UKat, ein Klick
auf ein Jahr seine Monate. Zugeklappt also Kat x Jahr, voll aufgeklappt
UKat x Monat. Das Terminal zeigt dieselbe Tabelle zugeklappt und auf die
letzten Jahre beschnitten.

Nur Ist-Buchungen: der F-Block ist ein verschobener Klon, mitgezaehlt
erschiene jeder Betrag doppelt.

Die HTML-Datei landet **neben ihrer Quelle**: `money.csv` →
`money/money.html`, nicht im Repository -- sie enthaelt die vollstaendige
Buchhaltung. Der Name folgt der Eingabe, damit `--test` und eine abweichende
Quelldatei nicht auf derselben Ausgabe landen. Sie ist in sich geschlossen:
kein CDN, kein Netz, die Klappmechanik sind zwanzig Zeilen JavaScript in der
Datei.

Kat und UKat werden getrimmt, sonst stuende `"MF "` als eigene Kategorie neben
`"MF"`. Solche Faelle werden im Terminal gemeldet statt stillschweigend
verschluckt -- in `money.csv` sind sie Tippfehler, und `build_rules` macht
daraus ein eigenes Tripel. Beim ersten Lauf waren es drei: `Kat='MF '`,
`UKat=' BR'`, `UKat='GU '`.

## `display_money` -- Kat als Baender ueber die Jahre

```bash
.venv/bin/python -m finance.display_money                        # die 8 groessten Kat
.venv/bin/python -m finance.display_money --kat EK FG L1 SA      # eine Auswahl
.venv/bin/python -m finance.display_money --from-year 2016 --top 5
.venv/bin/python -m finance.display_money --no-andere --no-open
```

Ein Band je Kat, Jahre von links nach rechts, das laufende Jahr rechts. Die
Dicke ist der Jahresbetrag: was hereinkommt stapelt sich ueber der Nulllinie,
was hinausgeht darunter. Oben liest man also die Einnahmenseite des Jahres,
unten die Ausgabenseite, und der Abstand zwischen beiden ist der Saldo.

Eingabe ist ein Zeitraum und eine Menge von Kat. Ohne `--kat` nimmt das
Programm die `--top N` groessten und faltet den Rest zu `Andere` zusammen,
damit das Bild vollstaendig bleibt; mit `--kat` zeigt es nur die Auswahl
(`--andere` holt den Rest wieder dazu, `--no-andere` laesst ihn auch im
Default weg). Mehr als **acht** Kat lehnt es ab: so viele Farbtoene hat die
Palette, bevor zwei davon fuer Farbenblinde gleich aussehen.

Ein Klick auf ein Band oder einen Legendeneintrag klappt die UKat auf -- am
besten eine Kat auf einmal, alle zusammen sind neunzig Baender und kein Bild
mehr. Aufgeklappt wird **neu gestapelt** und das Band nicht von innen
unterteilt, denn unterteilen geht nicht: in gut einem Fuenftel aller
Kat-Jahre haben die UKat gemischte Vorzeichen (Kat = +100 aus +150 und -50),
und dann ist die Summe der UKat-Dicken groesser als die Dicke der Kat.
Solange alle UKat das Vorzeichen ihrer Kat haben, bleibt die Huelle darum
exakt dieselbe; wo sie es nicht tun, aendert sie sich -- das ist die
Auskunft, dass in dieser Kategorie beides steckt.

Zeigen auf ein Jahr nennt alle Posten dieses Jahres samt Saldo, der Knopf
`Tabelle` zeigt dieselben Zahlen ohne Farbe. Ein Band braucht zwei Jahre;
fuer ein einzelnes ist `pivot` die richtige Ansicht.

Gelesen und aggregiert wird mit `pivot.load` und `pivot.pivot`, damit Tabelle
und Baender nicht auseinanderlaufen -- die Saldo-Zeile hier ist dieselbe wie
die Gesamt-Zeile dort. Die HTML-Datei landet **neben ihrer Quelle**:
`money.csv` -> `money/money-baender.html`, nicht im Repository. In sich
geschlossen, kein CDN, kein Netz.

## `evaluate` -- die Zahlen nachrechnen

```bash
.venv/bin/python -m finance.evaluate                     # Training bis 01.01.2026, Test 2026
.venv/bin/python -m finance.evaluate --split 01.01.2025 --test-months 12
```

Zeitlicher Split, keine Kreuzvalidierung: Regeln aus allen Ist-Buchungen vor
dem Stichtag, gemessen an denen danach. Eine zufällige Aufteilung wäre
geschönt -- sie ließe Regeln aus Buchungen lernen, die zum
Vorhersagezeitpunkt noch nicht existierten, und bei monatlich wiederkehrenden
Lastschriften ist das der halbe Datensatz.

Fünf Tabellen: Abdeckung und Genauigkeit je Feld; was die Labels wert sind
(die Tabelle oben); die Kalibrierungsprobe (`p` ist eine untere Schranke, die
gemessene Quote muss also *über* dem mittleren `p` liegen -- sonst ist die
Rechnung kaputt); die Labelgrenzen, je Kandidat die Genauigkeit unter und
über ihm; der Vergleich der Stufenauswahl. Alle Zahlen in diesem README kommen von hier und sind mit
`--split`/`--test-months` reproduzierbar.

## Offen

* **Die unscharfe Stufe** ist das schwächste Glied: 198 von 853 Testbuchungen
  landen dort, mit 67,7 % Kat-Trefferquote, und sie hat kein `p`, das die
  Grenze anwenden könnte. Tokenüberlappung durch Nachbarschaft in einem
  Einbettungsraum zu ersetzen würde "REWE SAGT DANKE" neben "REWE Markt GmbH
  Fil. 4711" legen; Gewicht über die Ähnlichkeit ergäbe auch endlich ein `p`.
* **Erstbuchungen ohne Historie** (122 von 853) kann keine Stufe treffen, weil
  die Antwort nicht in `money.csv` steht: dass "Zooplus" Tierbedarf ist, weiß
  nur Weltwissen. Das ist die einzige Stelle, an der ein Sprachmodell etwas
  beiträgt, was die Historie nicht hergibt -- und nur dort.
* **Amazon**: Bestellbestätigungs-Mails auswerten, um Bem zu füllen -- die
  Bank weiß nicht, was gekauft wurde, und genau da liegt der Rest der
  Trefferquote.
* **Exaktes Dedup** über die DKB-`Kundenreferenz`, die in der Umsatzliste schon
  mitgeliefert wird. Bräuchte eine neue Spalte in `money.csv`, deshalb
  zurückgestellt.
