# Geburtstage und Kontaktpflege

Missa denkt an wichtige Menschen mit: an ihre Geburtstage und daran, dass du dich
regelmässig meldest.

## Voraussetzungen

Nextcloud ist eingerichtet (`NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD`).
Kontakte und Geburtstage liest Missa per CardDAV aus der App „Kontakte“ – nur lesend.
Optional in `/etc/missa/missa.env`:

```ini
NEXTCLOUD_ADRESSBUCH=Kontakte     # leer = alle Adressbücher
KONTAKTE_GEBURTSTAG_VORLAUF=1     # Tage vorher erinnern (1 = am Vortag, 0 = nur am Tag)
KONTAKTE_GEBURTSTAGE=wichtige     # nur wichtige Kontakte (Standard) oder „alle“
KONTAKTE_ABSTAND_TAGE=30          # Standard-Abstand für wichtige Kontakte
KONTAKTE_PRUEFZEIT=08:00          # tägliche Prüfung, auch Uhrzeit der Erinnerung am Morgen
```

Für Vorschläge freier Zeiten braucht Missa den Kalender (`NEXTCLOUD_KALENDER`), für den
letzten Kontakt per Mail die E-Mail-Anbindung (siehe [email.md](email.md)), für die
Zustellung Nextcloud Talk (`NC_TALK_NOTIFY_CONVERSATION`).

## Geburtstage

Jeden Tag um `KONTAKTE_PRUEFZEIT` schaut Missa in die Kontakte und plant für **wichtige
Kontakte** (siehe unten; mit `KONTAKTE_GEBURTSTAGE=alle` für alle) über den Zeitplaner
(siehe [erinnerungen.md](erinnerungen.md)):

- eine Erinnerung **am Vortag** („🎂 Morgen (Freitag, 13.11.) hat Oma Geburtstag – 85 Jahre!
  Schon an eine Karte gedacht?“),
- eine **am Morgen des Tages** („🎉 Heute hat Oma Geburtstag (85)!“).

Jede Erinnerung entsteht nur einmal, auch wenn die Prüfung täglich läuft, und wird nach
einem Neustart nachgeholt. **29. Februar:** In Jahren ohne Schalttag erinnert Missa am
28. Februar. Geburtstage ohne Jahr (`--MM-TT` oder Jahr 1604) gehen auch – dann ohne Alter.

Im Chat: „Wer hat bald Geburtstag?“ (`geburtstage_anzeigen`).

## Wichtige Kontakte

| Du sagst | Missa macht |
|----------|-------------|
| „Oma ist mir wichtig, mindestens alle 3 Wochen“ | markiert Oma mit einem Abstand von 21 Tagen |
| „Oma ist mir wichtig“ | markiert Oma mit dem Standard-Abstand (30 Tage) |
| „Hab heute Oma angerufen“ | vermerkt den Kontakt (heute, Anruf) – der Code erkennt den Satz |
| „Wann hab ich Oma zuletzt angerufen?“ | nennt Datum und Art des letzten Kontakts (`letzter_kontakt`) |
| „Gestern war ich bei Tom“ | vermerkt den Kontakt mit Datum von gestern |
| „Wie steht's mit meinen Kontakten?“ | Liste mit Abstand und letztem Kontakt |
| „Erinnere mich nicht mehr an Onkel Fritz“ | nimmt ihn aus der Kontaktpflege |

Missa findet die Person über Name, Spitzname (z. B. `NICKNAME:Oma` im Kontakt) oder
E-Mail in den Nextcloud-Kontakten; steht sie dort nicht, merkt sie sich einfach den Namen.

**Letzter Kontakt** kommt automatisch aus deinen Mails (Posteingang und Gesendet, bei der
täglichen Prüfung, und sofort, wenn Missa eine Mail für dich verschickt) und aus deinen
Angaben im Chat. **Für die spätere Telefonie** ist der Anschluss schon da: `POST
/api/kontakte/kontakt` mit `telefon` (oder `email`/`name`), optional `zeit` und `art`
(Standard „Anruf“) – Missa findet die Person über die Telefonnummer im Adressbuch
(`+41 79 …` und `079 …` gelten als gleich) und vermerkt den Kontakt (siehe [api.md](api.md)).

**Abstand überschritten:** Bei der täglichen Prüfung erinnert dich Missa freundlich in ihrem
Charakter (sie formuliert die Nachricht selbst; ist das Sprachmodell nicht erreichbar, nimmt
sie einen festen Text) und schlägt einen freien Zeitpunkt aus deinem Kalender vor – werktags
zwischen 17 und 20 Uhr, am Wochenende zwischen 10 und 19 Uhr, in den nächsten Tagen. Danach
wartet sie ein paar Tage, bevor sie wieder erinnert.

Die Daten liegen in `missa.db` (Tabelle `kontakte_pflege`); Kontakte selbst ändert Missa nicht.

## E-Mails im eigenen Stil

Entwürfe schreibt Missa in deinem Stil (gelernt aus dem Ordner „Gesendet“, nur lesend),
zeigt sie dir im Gespräch und legt sie im Ordner „Entwürfe“ ab. Gesendet wird nur nach
deinem ausdrücklichen Ja zu genau diesem Entwurf – nie aus einer Routine, der Nachtschicht
oder automatisch. Einzelheiten: [email.md](email.md).
