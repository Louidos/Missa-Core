# E-Mail-Entwürfe im eigenen Stil

Missa schreibt E-Mails so, wie du schreibst, legt sie im Entwürfe-Ordner deines Mailkontos ab
und zeigt sie dir im Chat. **Gesendet wird nur, wenn du ausdrücklich zustimmst.**

## Einrichtung

In `/etc/missa/missa.env` (Vorlage: [config/missa.env.example](../config/missa.env.example)):

```ini
MAIL_IMAP_HOST=mail.example.ch
MAIL_IMAP_PORT=993
MAIL_SMTP_HOST=mail.example.ch
MAIL_SMTP_PORT=465
MAIL_USER=louis@example.ch
MAIL_PASSWORD=<App-Passwort des Mailkontos>
MAIL_ABSENDER=Louis Degiorgi <louis@example.ch>
MAIL_ORDNER_ENTWUERFE=Drafts
MAIL_ORDNER_GESENDET=Sent
```

Danach `sudo systemctl restart missa-core`. Das Passwort steht nur in dieser Datei (Rechte
`640`, Gruppe `missa`), nie im Repo. Wie die Ordner auf deinem Server heißen, siehst du im
Mailprogramm (oft `Drafts`/`Sent`, manchmal `Entwürfe`/`Gesendet` oder `INBOX.Drafts`).

## So geht es im Chat

1. „Schreib dem Chef, dass ich morgen später komme“ – Missa sucht die Adresse (Gedächtnis,
   frühere Mails, später auch Kontakte), schreibt den Text in deinem Stil und legt ihn im
   Entwürfe-Ordner ab.
2. Unter ihrer Antwort steht der Entwurf **wortwörtlich** mit Empfänger, Betreff und Text,
   dazu die Frage **„Soll ich das abschicken?“**
3. Du antwortest „Ja, abschicken“ (oder „senden #3“) – erst dann geht die Mail raus, landet
   in „Gesendet“ und verschwindet aus den Entwürfen.

Weitere Beispiele: „Antworte Anna, dass ich komme“ (Antwort auf eine Mail, mit „Re:“ und
Verweis auf die Originalmail), „Mach den Betreff kürzer“, „Verwirf den Entwurf“,
„Welche Entwürfe sind offen?“, „Was ist neu im Posteingang?“

Kennt Missa eine Adresse nicht, fragt sie nach. Tipp: „Merk dir: Mein Chef ist Peter Muster
(peter.muster@firma.ch)“ – dann klappt „Schreib dem Chef“ beim nächsten Mal.

## Die Senden-Sperre (im Code, nicht im Prompt)

- Das Sprachmodell hat **kein Werkzeug zum Senden**. Es kann Entwürfe nur anlegen, ändern,
  anzeigen und verwerfen.
- Den Entwurf unter der Antwort hängt der **Code** an, nicht das Modell – du siehst also
  genau das, was gesendet würde. Der Code merkt sich eine Prüfsumme dieser Fassung.
- Gesendet wird nur, wenn **deine neue Nachricht** (vor dem Modell geprüft) eine kurze,
  eindeutige Zustimmung ist: „Ja, abschicken“, „Ja“, „Schick ab“, „Schick diese Mail“,
  „Sende die Mail ab“, „senden #3“. „Ja, aber ändere …“, „Noch nicht“, „Schick die Mail an
  Peter“ (ein anderer Empfänger ist eine Änderung) oder längere Sätze zählen nicht.
- Ein knappes „Ja“ gilt nur direkt nach dem gezeigten Entwurf; „senden #3“ gilt, solange
  du genau diese Fassung in diesem Gespräch gesehen hast.
- **Ändert sich der Entwurf**, ist eine neue Zustimmung nötig – Missa zeigt die neue Fassung
  und fragt erneut.
- Text in einer empfangenen Mail, im Entwurf selbst oder in Missas Antwort kann nie als
  Zustimmung zählen. Jeder Entwurf wird höchstens einmal gesendet.

## Dein Schreibstil

Missa leitet aus deinen gesendeten Mails ein Profil ab (Anrede, Grussformel, Unterschrift,
Länge, Du/Sie, Schweizer Rechtschreibung, Emojis): „Lern meinen Schreibstil“ (Werkzeug
`schreibstil_lernen`). Es liegt als Markdown-Datei in `MAIL_STIL_DATEI`
(Standard `/opt/missa/data/schreibstil.md`) und darf frei bearbeitet werden.

Eigene Wünsche gehen per Chat: „Schreib förmlicher an Lehrer“ → steht unter
„Eigene Regeln“ und bleibt auch beim Neu-Lernen erhalten. „Vergiss die Regel mit den
Lehrern“ entfernt sie wieder. Geht es im Chat um Mails, bekommt Missa das Profil automatisch
mit.
