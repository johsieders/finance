# Finance

Buchhaltung für das eigene Girokonto: aus dem monatlichen DKB-Kontoauszug wird
eine kategorisierte Zeile in `money.csv`, plus ein um ein Jahr in die Zukunft
verschobener Klon für die rollende 12-Monats-Vorschau. Nachfolger des früheren
Google-Apps-Script-Menüs "import".

Die Kategorien schlägt das Programm aus der eigenen Historie vor -- `money.csv`
ist die Quelle der Wahrheit, die Regeln sind ein daraus abgeleiteter Cache.
Korrigiert wird von Hand, und jede Korrektur verbessert die nächsten Vorschläge.

## Monatlicher Ablauf

```bash
# 1. Umsatzliste bei der DKB herunterladen, nach umsatzlisten/ legen

# 2. Regeln aus der Historie neu berechnen
python3 -m finance.build_rules

# 3. Vorschlagsdatei erzeugen: suggestions/<auszug>.suggestion.csv
python3 -m finance.categorize_import

# 4. Kat/UKat/Bem in Excel korrigieren und als CSV zurückspeichern

# 5. Die korrigierte Datei in money.csv eintragen
python3 -m finance.import_dkb
```

Ohne Argumente nimmt jedes Programm die jeweils neueste Datei im vorgesehenen
Ordner; `--help` zeigt, wie man eine andere wählt. `python3 -m
finance.import_dkb --dry-run` zeigt, was passieren würde, ohne zu schreiben.

## Datenablage

Die Daten liegen außerhalb des Repositorys, unter `~/Documents/finance`:

```
finance/
  money/money.csv                  die Buchhaltung selbst
  umsatzlisten/DD-MM-YYYY_...csv   Kontoauszüge, wie von der DKB geliefert
  rules/rules.json                 abgeleiteter Regel-Cache
  suggestions/....suggestion.csv   Vorschläge zum Korrigieren
```

`--test` schaltet den ganzen Baum auf `~/Documents/finance-test` um, eine Kopie
von `finance`. Dort lassen sich vergangene Jahre gefahrlos nachspielen, bevor
etwas die echte Buchhaltung anfasst.

## Die drei Programme

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

### `categorize_import`

Umsatzliste + `rules.json` → Vorschlagsdatei, in der Spaltenstruktur von
`money.csv`, mit vorgeschlagenen Kat/UKat/Bem. Schreibt nichts in `money.csv`.

`MF`, `MT` und `Saldo` bleiben leer -- die entstehen erst beim Eintragen.
Hinten stehen zwei zusätzliche Spalten, `Konfidenz` und `Quelle`: sie sagen,
welcher Vorschlag auf 133 gleichen Buchungen beruht und welcher auf geratener
Tokenüberlappung. `import_dkb` ignoriert sie, `--plain` lässt sie weg.

Nach den Regelstufen folgt als letztes Netz eine unscharfe Stufe, die direkt
gegen `money.csv` läuft (Tokenüberlappung über Empfänger + Verwendungszweck).
Sie liefert nur "niedrig" -- explizit zum Gegenlesen. `--no-fuzzy` schaltet sie
ab.

### `import_dkb`

Korrigierte Vorschlagsdatei → `money.csv`. Jede neue Buchung wird zweimal
eingetragen: als Ist-Zeile und als F-Zeile ein Jahr später. Der damit obsolete
Teil des alten F-Blocks fällt weg, MT und Saldo werden für alles darüber
fortgeschrieben.

Die Vorschlagsdatei kommt aus Excel zurück, also toleriert der Leser, was Excel
anrichtet: eine Vorlaufzeile über der Kopfzeile, `;` oder `,` als Trenner,
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

## Offen

* **Kalibrierung**: Kat unterhalb einer Belegschwelle leer lassen statt einen
  selbstsicher aussehenden Vorschlag zu machen; Konfidenz soll die Menge der
  Belege widerspiegeln, nicht nur deren Einigkeit (2/2 gilt heute als "hoch").
* **Amazon**: Bestellbestätigungs-Mails auswerten, um Bem zu füllen -- die
  Bank weiß nicht, was gekauft wurde, und genau da liegt der Rest der
  Trefferquote.
* **Exaktes Dedup** über die DKB-`Kundenreferenz`, die in der Umsatzliste schon
  mitgeliefert wird. Bräuchte eine neue Spalte in `money.csv`, deshalb
  zurückgestellt.
