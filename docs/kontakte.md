# Geburtstage und Kontaktpflege

Missa denkt an wichtige Menschen mit: an ihre Geburtstage und daran, dass du dich
regelmässig meldest.

## Voraussetzungen

Nextcloud ist eingerichtet (`NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD`).
Kontakte und Geburtstage liest Missa per CardDAV aus der App „Kontakte“ – nur lesend.
Optional in `/etc/missa/missa.env`:

```ini
NEXTCLOUD_ADRESSBUCH=Kontakte     # leer = alle Adressbücher
KONTAKTE_GEBURTSTAG_VORLAUF=3     # Tage vorher erinnern (0 = nur am Tag)
KONTAKTE_PRUEFZEIT=08:00          # tägliche Prüfung
```

Für Vorschläge freier Zeiten braucht Missa den Kalender (`NEXTCLOUD_KALENDER`), für den
letzten Kontakt per Mail die E-Mail-Anbindung (siehe [email.md](email.md)), für die
Zustellung Nextcloud Talk (`NC_TALK_NOTIFY_CONVERSATION`).

## Geburtstage

Jeden Tag um `KONTAKTE_PRUEFZEIT` schaut Missa in die Kontakte und plant über den
Zeitplaner (siehe [erinnerungen.md](erinnerungen.md)):

- eine Erinnerung **einige Tage vorher** („🎂 In 3 Tagen (Freitag, 13.11.) hat Oma
  Geburtstag – 85 Jahre! Schon an eine Karte gedacht?“),
- eine **am Tag selbst** („🎉 Heute hat Oma Geburtstag (85)!“).

Jede Erinnerung entsteht nur einmal, auch wenn die Prüfung täglich läuft, und wird nach
einem Neustart nachgeholt. **29. Februar:** In Jahren ohne Schalttag erinnert Missa am
28. Februar. Geburtstage ohne Jahr (`--MM-TT` oder Jahr 1604) gehen auch – dann ohne Alter.

Im Chat: „Wer hat bald Geburtstag?“ (`geburtstage_anzeigen`).

## Wichtige Kontakte

| Du sagst | Missa macht |
|----------|-------------|
| „Oma ist mir wichtig, mindestens alle 3 Wochen“ | markiert Oma mit einem Abstand von 21 Tagen |
| „Hab gerade mit Oma telefoniert“ | vermerkt den Kontakt (heute, Telefon) |
| „Gestern war ich bei Tom“ | vermerkt den Kontakt mit Datum von gestern |
| „Wie steht's mit meinen Kontakten?“ | Liste mit Abstand und letztem Kontakt |
| „Erinnere mich nicht mehr an Onkel Fritz“ | nimmt ihn aus der Kontaktpflege |

Missa findet die Person über Name, Spitzname (z. B. `NICKNAME:Oma` im Kontakt) oder
E-Mail in den Nextcloud-Kontakten; steht sie dort nicht, merkt sie sich einfach den Namen.

**Letzter Kontakt** kommt automatisch aus deinen Mails (Posteingang und Gesendet, bei der
täglichen Prüfung, und sofort, wenn Missa eine Mail für dich verschickt) und aus deinen
Angaben im Chat. Anrufe kommen später dazu.

**Abstand überschritten:** Bei der täglichen Prüfung erinnert dich Missa freundlich in ihrem
Charakter (sie formuliert die Nachricht selbst; ist das Sprachmodell nicht erreichbar, nimmt
sie einen festen Text) und schlägt einen freien Zeitpunkt aus deinem Kalender vor – werktags
zwischen 17 und 20 Uhr, am Wochenende zwischen 10 und 19 Uhr, in den nächsten Tagen. Danach
wartet sie ein paar Tage, bevor sie wieder erinnert.

Die Daten liegen in `missa.db` (Tabelle `kontakte_pflege`); Kontakte selbst ändert Missa nicht.
