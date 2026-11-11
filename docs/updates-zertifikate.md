# Updates, Wartungsfenster, Zertifikate und Domains

Missa weiß, wo Updates anstehen, plant ein Wartungsfenster im Kalender und spielt die
Updates nach deiner Freigabe ein, VM für VM. Ablaufende Zertifikate und Domains meldet sie
rechtzeitig, und sie prüft, ob die DNS-Einträge noch stimmen.

Updates brauchen `SSH_HOSTS` und auf jedem Host den Benutzer `missa`
([container-logs-logins.md](container-logs-logins.md)). Zertifikate und Domains brauchen
`ZERTIFIKAT_DOMAINS` oder `ERREICHBARKEIT_DOMAINS`. Ohne Konfiguration ist alles aus.

## Was du sagen kannst

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Welche Updates stehen an?“ | Liste pro VM, Sicherheitsupdates mit 🔒, ob ein Neustart nötig ist | `updates_anzeigen` (lesen) |
| „Plan ein Wartungsfenster“ | schlägt einen freien Termin im Kalender vor, trägt **nichts** ein | `wartungsfenster_vorschlagen` (lesen) |
| „Ja, trag es ein“ | erst jetzt erscheint der Termin im Kalender | `wartungsfenster_eintragen` (zerstoererisch = braucht dein Ja) |
| „Wie ist der Plan?“ | Reihenfolge der VMs, was schon erledigt ist, das Fenster | `updates_plan` (lesen) |
| „Spiel die Updates auf wb01 ein“ | sagt, was passiert, und wartet auf dein Ja | `updates_einspielen` (zerstoererisch) |
| „Starte wb01 neu“ | eigenes Ja, Neustart nach 1 Minute | `host_neustarten` (zerstoererisch) |
| „Wann laufen die Zertifikate ab?“ | Zertifikate, Domains (RDAP) und DNS-Abgleich | `zertifikate_pruefen` (lesen) |

## Updates erfassen

Jeden Tag um `UPDATES_PRUEFZEIT` (06:40) holt Missa auf jeder VM frische Paketlisten
(`apt-get update`) und liest, was ansteht (`apt list --upgradable`). Sicherheitsupdates
erkennt sie wie apt an der Quelle `…-security`. Sie zählen getrennt und tragen ein 🔒.
Stehen neue Sicherheitsupdates an, kommt einmal ein `info`, für dieselben Pakete nicht
noch einmal. Ob ein Neustart nötig ist, steht in `/var/run/reboot-required`.

## Wartungsfenster

1. **Vorschlag:** Missa sucht in den nächsten drei Wochen an den Tagen `WARTUNG_TAGE`
   (Sa, So) zwischen `WARTUNG_VON` und `WARTUNG_BIS` (10–17 Uhr) eine freie Lücke von
   `WARTUNG_DAUER_MINUTEN` (60) in allen Kalendern. Eingetragen wird dabei nichts.
2. **Eintragen:** erst nach deinem Ja. Dafür hat das Werkzeug die Stufe `zerstoererisch`,
   die Rechte-Schicht fragt nach. Steht in der Zeit schon etwas, sagt Missa das in der
   Rückfrage. Der Termin heißt „Wartung: Server-Updates“ und enthält die Reihenfolge.
3. **Zu Beginn** schreibt Missa dir den Plan und wartet auf dein „Los“ für die erste VM.

## Einspielen

- **VM für VM, jede einzeln nach Ja.** Ein Ja für wb01 gilt nicht für int01.
- Missa führt `apt-get -y upgrade` aus. Geänderte Konfigurationsdateien bleiben, wie sie
  sind (`--force-confold`), es gibt keine Rückfragen und kein `dist-upgrade`.
- Danach sagt Missa, was aktualisiert wurde, was noch offen ist, ob ein Neustart nötig ist
  und welche VM als nächste dran ist.
- **Die eigene VM (`UPDATES_EIGENER_HOST`, ai01) kommt immer zuletzt.** Missa lehnt sie ab,
  solange auf anderen VMs noch Updates offen sind, außer du willst die anderen ausdrücklich
  auslassen. In der Rückfrage warnt sie, dass sie dabei kurz weg sein kann.
- **Ein Neustart braucht ein eigenes Ja.** Er passiert eine Minute nach dem Befehl, damit
  Missa die Antwort noch bekommt. Startet ai01 neu, meldet sich Missa danach mit
  „Ich bin wieder da“.
- Im Hintergrund (Routinen, Nachtschicht) spielt Missa nie etwas ein.
- Jeder Schritt steht im Aktionsprotokoll.

Ein langes Upgrade kann einige Minuten dauern. In Nextcloud Talk kommt die Antwort, sobald
es fertig ist; dauert es länger als `NC_TALK_TIMEOUT`, frag einfach „Wie ist der Plan?“.

**Auf dem Host:** `missa-benutzer.sh` (Version ab 0.7) richtet dafür
`/usr/local/lib/missa/missa-update` ein. sudo darf nur dieses Programm starten, und es kennt
nur drei Dinge: `apt-get update`, `apt-get -y upgrade` und `reboot` (`shutdown -r +1`).
Kein `install`, kein `remove`, kein `dist-upgrade`, keine weiteren Angaben. Auf schon
eingerichteten Hosts das Script einfach noch einmal ausführen.

## Zertifikate und Domains

Jeden Tag um `ZERTIFIKATE_PRUEFZEIT` (07:10) prüft Missa:

- **HTTPS-Zertifikate** aller Domains aus `ZERTIFIKAT_DOMAINS` (Standard: die aus
  `ERREICHBARKEIT_DOMAINS`), mit voller Prüfung und Ablaufdatum aus dem Zertifikat.
- **Ablaufdatum der Domains** per RDAP (`DOMAIN_NAMEN`, Standard: aus den Zertifikats-Domains
  abgeleitet, z. B. `louis-degiorgi.ch`). Welcher Server zuständig ist, steht in der
  offiziellen IANA-Liste (`RDAP_BOOTSTRAP_URL`). **Das ist die einzige Abfrage nach außen
  dieser Prüfung.** Manche Registries, z. B. SWITCH für `.ch`, veröffentlichen kein
  Ablaufdatum. Dann sagt Missa das, statt zu raten.
- **DNS-Einträge** aus `DNS_ERWARTET` gegen die tatsächlichen Werte (A, AAAA, CNAME, MX,
  TXT, NS, CAA), gefragt über den Resolver von ai01.

Gewarnt wird je Schwelle aus `ZERTIFIKAT_WARNUNG_TAGE` (21 und 7 Tage vorher) **genau
einmal** als `wichtig`. Ist etwas schon abgelaufen, kommt ein `notfall`. Ein erneuertes
Zertifikat mit neuem Ablaufdatum setzt die Warnungen zurück. Weicht ein DNS-Eintrag ab,
kommt ein `wichtig` mit erwartetem und gefundenem Wert. Stimmt er wieder, kommt ein `info`.

Beispiel für `DNS_ERWARTET`:

```
DNS_ERWARTET=cloud.louis-degiorgi.ch A 203.0.113.10; git.louis-degiorgi.ch A 203.0.113.10; ki.louis-degiorgi.ch A 203.0.113.10
```

Die nötigen Netzfreigaben stehen in [netzfreigaben.md](netzfreigaben.md).
