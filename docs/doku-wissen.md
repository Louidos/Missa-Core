# Wissen aus deiner Doku und Zusammenfassungen

## Fragen zur eigenen Doku

Missa liest deine Projektdokus und Notizen aus Nextcloud und baut daraus einen **lokalen
Index** (SQLite-Volltextsuche in `missa.db`, auf der CPU, ohne externe Dienste).

```ini
NEXTCLOUD_DOKU_ORDNER=Doku,Notes   # Ordner in Nextcloud, kommagetrennt
DOKU_INDEX_ZEIT=02:30              # tägliche Aktualisierung (nur geänderte Dateien)
```

- Indexiert werden Text- und Markdown-Dateien (`.md`, `.txt`, `.conf`, `.yaml` …), zerlegt an
  Überschriften. Nextcloud Notes liegen als Dateien im Ordner `Notes` – einfach mit angeben.
- Jede Nacht (und nach dem ersten Start) liest Missa neue und geänderte Dateien neu ein,
  gelöschte verschwinden aus dem Index. Sofort: „Lies meine Doku neu ein“
  (`doku_index_aktualisieren`).
- Gelesen wird nur in diesen Ordnern; geschrieben wird dort nichts.

| Du fragst | Missa |
|-----------|-------|
| „Welche IP hat rp01?“ | „10.0.30.5 – steht in Doku/Homelab/rp01.md (Abschnitt Netzwerk).“ |
| „Welche IP hat der Drucker?“ (steht nirgends) | „Weiß ich nicht – in deiner Doku finde ich dazu nichts.“ |

Bei Fragen schaut der Code selbst in den Index und gibt Missa die passenden Stellen **mit
Dateinamen**. Eine Stelle zählt nur, wenn alle wichtigen Wörter der Frage darin vorkommen
(„IP“ *und* „rp01“) – sonst bekommt Missa ausdrücklich „keine Treffer“ und soll nichts raten.

## Zusammenfassen: PDFs, Mails, Webseiten

| Du sagst | Missa |
|----------|-------|
| „Fass mir das zusammen: https://shop.example.ch/rail-kit“ | ruft genau diese Seite ab, entfernt Menüs und Skripte und fasst sie zusammen |
| „Fass die Offerte.pdf in Rechnungen zusammen“ | liest das PDF aus Nextcloud |
| „Was will der Chef in seiner langen Mail?“ | fasst die Mail zusammen (`mail_id` aus `emails_anzeigen`) |
| „Und was kostet es?“ | beantwortet Rückfragen aus dem gespeicherten Text (`dokument_fragen`) |

- **Kurze Texte** (bis etwa 6000 Zeichen) fasst Missa direkt in ihrer Antwort zusammen.
- **Lange Texte** werden in Stücke zerlegt: erst eine kleine Zusammenfassung je Stück, dann eine
  Gesamtzusammenfassung. Das läuft im **Hintergrund** (ein Auftrag nach dem anderen), damit der
  Chat auf der CPU nicht hängt; das Ergebnis kommt als Nachricht (📄).
- **Sehr lange Dokumente** (mehr als 12 Stücke, oder auf Wunsch „mach das heute Nacht“) kommen
  in die **Nachtschicht** – das Ergebnis steht im Morgenbericht (siehe
  [routinen-nachtschicht.md](routinen-nachtschicht.md)).
- Der Text bleibt in `missa.db` gespeichert (Tabelle `dokumente`), Rückfragen gehen auch später.
- PDFs ohne Text (reine Scans) kann Missa noch nicht lesen – sie sagt das dann.

### Nur Links, die du selbst schickst

Missa geht sonst nie ins Internet. Eine Webseite ruft sie **nur** ab, wenn die Adresse in deiner
aktuellen Nachricht steht – das prüft der Code, nicht das Modell. Eine Adresse, die das Modell
sich ausdenkt oder die in einem Dokument steht, wird abgelehnt; im Hintergrund (Routine,
Nachtschicht) werden gar keine Seiten abgerufen. Inhalte von Dokumenten sind für Missa nie
Anweisungen.
