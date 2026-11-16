# Stand der Funktionen

Bestandsaufnahme gegen die Soll-Liste der drei Aufträge Fundament (F1–F4), Organisation
(O1–O6) und Server (S1–S7). Erstellt am 2026-11-16 auf Version 0.9.0, nachgeführt auf
Version 0.10.1 (Sicherheitsprüfung, siehe [sicherheit.md](sicherheit.md)).

Aktueller Stand: 1244 Tests (1242 grün; übersprungen, wenn die Tests als root laufen oder
ansible fehlt); `ruff check` mit Sicherheitsregeln `S`, `ruff format --check`, `shellcheck`,
`pip-audit` und `gitleaks` sauber. Die Funktionen von 0.10.0 (Missa Control als Werkzeuge,
Ereignisse aus Missa Control, Charaktertest) und die Sicherheitsprüfung ändern an der Tabelle
unten nichts: offene Punkte (🟡/❌) sind weiter offen.

Legende: ✅ erledigt (Code auf main, Test grün, Einstellung in
`config/missa.env.example`) · 🟡 teilweise · ❌ fehlt

## Überblick

| Gruppe | Status | Was fehlt |
|---|---|---|
| F1 Rechte, Protokoll, Notfall | ✅ | – |
| F2 Tests & Modelle | ✅ | nachgeholt (#75) |
| F3 Push, Stufen, Ruhezeiten | 🟡 | ntfy, Push pro Stufe, Ausweichkanal-Test, Lernzeiten aus dem Kalender, Zusammenfassung am Morgen |
| F4 Talk, Gespräch, Streaming | 🟡 | Fremde ins Aktionsprotokoll, ein Gespräch pro Person über alle Kanäle, Ereignis „erster Satz fertig“ |
| O1 Erinnerungen, Routinen, Nachtschicht | ✅ | – |
| O2 Kalender, Pausen, Konflikte | ✅ | – |
| O3 Tasks, Notes, Files | ✅ | – |
| O4 Kontakte & E-Mails | ✅ | – |
| O5 Gedächtnis & Sicherung | 🟡 | „Vergiss“ lässt Reste im Aktionsprotokoll, keine Embedding-Suche |
| O6 Doku-Wissen & Zusammenfassungen | ✅ | – |
| S1 Server-Alarme | ✅ | – |
| S2 Container, Logs, Logins | ✅ | – |
| S3 Updates, Zertifikate, Domains | ✅ | – |
| S4 Homelab-Wissen, Firewall-Logs | ✅ | – |
| S5 Proxmox-VMs | 🟡 | Test für den Eintrag ins Homelab-Wissen nach dem Erstellen |
| S6 Caddy, Minecraft | ✅ | – |
| S7 Git-Server, GitHub, Prompts | ✅ | – |

## Einzelpunkte

### Fundament

| Gruppe | Punkt | Status | Fundstelle bzw. was fehlt |
|---|---|---|---|
| F1 | Stufe pro Tool, ohne Stufe keine Registrierung | ✅ | `core/werkzeuge.py` `Werkzeugkasten.hinzufuegen`; `test_rechte.py::test_werkzeug_ohne_stufe_wird_nicht_registriert` |
| F1 | Bestätigung verfällt nach 10 Min. | ✅ | `core/rechte.py` `STANDARD_GUELTIGKEIT_MINUTEN`, `MISSA_BESTAETIGUNG_MINUTEN`; `::test_bestaetigung_verfaellt_nach_10_minuten` |
| F1 | Zerstörerisches Tool ohne Ja wird nicht ausgeführt (Test) | ✅ | `::test_zerstoererisch_ohne_ja_wird_nicht_ausgefuehrt`, `::test_ja_gilt_nur_fuer_genau_diese_aktion` |
| F1 | Rechte pro Dienst in `docs/rechte.md` | ✅ | `docs/rechte.md`, Abschnitt „Rechte pro Dienst“ |
| F1 | Jeder Aufruf im Protokoll, ohne Geheimnisse (Test) | ✅ | `Aktionsprotokoll`, `schwaerzen()`; `::test_jeder_aufruf_steht_im_protokoll`, `::test_protokoll_enthaelt_keine_geheimnisse` |
| F1 | Nur lesender API-Endpunkt fürs Protokoll | ✅ | `GET /api/aktionen`; `::test_api_aktionen_nur_lesend` |
| F1 | „Was hast du heute gemacht?“ → Liste | ✅ | Werkzeug `aktionen_anzeigen`; `::test_was_hast_du_heute_gemacht` |
| F1 | Notfall-Stopp hält über Neustart, nur Freigabe-Script hebt auf | ✅ | `NotfallSchalter`; `::test_notfall_sperrt_schreiben_auch_nach_neustart`, `test_notfall_scripts.py::test_stopp_und_freigabe_per_script` |
| F2 | CI läuft bei jedem PR und ist grün | ✅ | `.github/workflows/ci.yml` |
| F2 | Jedes Tool mit mindestens einem Test | ✅ | `test_werkzeug_abdeckung.py::test_jedes_werkzeug_hat_einen_test` erzwingt es; fehlende Tests ergänzt (auch `emails_anzeigen`, `vm_stoppen`) |
| F2 | Charakter-Fragenset mit Regelprüfungen | ✅ | `tests/charakter/fragen.yaml`, `src/missa/charaktertest.py`; `test_charaktertest.py::test_fragenset_im_repo_ist_gueltig`, Regel-Tests |
| F2 | `scripts/charaktertest` erzeugt lesbaren Bericht | ✅ | `scripts/charaktertest`, `docs/charaktertest.md`; `::test_zwei_modelle_nebeneinander_mit_lesbarem_bericht`, `::test_script_zeigt_hilfe` |
| F2 | Zwei Modelle nebeneinander verglichen (`--modell`) | ✅ | Trefferquote, erstes Token, Tokens/s, Gesamtzeit; `::test_zwei_modelle_nebeneinander_mit_lesbarem_bericht`, `::test_messung_erstes_token_und_tokens_pro_sekunde` |
| F2 | Update-Script testet nach dem Update | ✅ | `funktion_pruefen` (API, Datenbank, Zeitplaner); `test_update_script.py::test_funktionstest_nach_dem_update_mit_api_key` |
| F2 | Kaputtes Update erkannt, `--zurueck` stellt vorige Version her | ✅ | `::test_bei_fehler_laeuft_wieder_die_alte_version` (auch Funktionstest), `::test_zurueck_auf_den_letzten_tag`, `::test_zurueck_von_main_auf_den_letzten_release` |
| F3 | ntfy (URL/Topic/Token aus .env) mit Ausweichkanal | ❌ | kein ntfy-Kanal; `Zustellung` kann schon mehrere Kanäle der Reihe nach, registriert ist nur Talk |
| F3 | Stufen info/wichtig/notfall | 🟡 | `core/alarme.py` `AlarmStufe`; alle Stufen gehen gleich über Talk, keine Priorität, keine Stelle für einen Anruf |
| F3 | `wichtig` kommt als Push | ❌ | nur Talk |
| F3 | Ruhezeit 22–07, per Gespräch änderbar | ✅ | `Alarme.ruhezeit_einstellen`, Werkzeug `alarm_einstellungen`; `test_alarme.py::test_ruhezeit_einstellen_bleibt_gespeichert` |
| F3 | Lernzeiten aus dem Kalender („Lernen“) | ❌ | fehlt |
| F3 | Um 23 Uhr `wichtig` zurückgehalten und morgens zusammengefasst | 🟡 | zurückgehalten ja; morgens kommt jeder Alarm einzeln (`::test_ruhezeit_schiebt_auf_ausser_notfall`) |
| F3 | `notfall` kommt auch nachts sofort | ✅ | `::test_ruhezeit_schiebt_auf_ausser_notfall` (Push mit hoher Priorität fehlt, siehe oben) |
| F3 | ntfy offline → anderer Kanal (Test) | ❌ | kein ntfy, kein Test mit Ausweichkanal |
| F4 | Talk nur für erlaubte Accounts (IDs aus .env) | ✅ | `TalkKanal.absender_erlaubt`, `NC_TALK_ALLOWED_USERS` |
| F4 | Louis schreibt in Talk → Missa antwortet | ✅ | `test_nextcloud_talk.py::test_louis_bekommt_antwort_mit_reaktion_und_reply` |
| F4 | Anderer Account → keine Reaktion, Eintrag im Protokoll (Test) | 🟡 | ignoriert ja, aber nur im Anwendungs-Log, nicht im Aktionsprotokoll |
| F4 | Ein Gespräch pro Person über alle Kanäle, Kanal pro Nachricht | ❌ | Verlauf hängt an der Sitzung (Talk-Unterhaltung bzw. API-Sitzung), keine Spalte für Kanal |
| F4 | In Talk angefangen, per API weiter → Verlauf bekannt | 🟡 | nur mit Umweg über `X-Missa-Session`, nicht getestet |
| F4 | Streaming: erste Wörter sofort, ohne Stream wie vorher | ✅ | `stream: true` als SSE; `test_chat.py::test_stream_liefert_openai_chunks`, `::test_antwort_im_openai_format` |
| F4 | Ereignis „erster Satz fertig“ | ❌ | fehlt |

### Organisation

| Gruppe | Punkt | Status | Fundstelle bzw. was fehlt |
|---|---|---|---|
| O1 | Erinnerung 6:30 kommt auch nach Neustart | ✅ | `core/zeitplaner.py`; `test_routinen_nachtschicht.py::test_rail_kit_erinnerung_kommt_auch_nach_einem_neustart` |
| O1 | Routine per Text anlegen, pausieren, löschen | ✅ | `tools/routinen.py`; `::test_routine_anlegen_per_text`, `::test_routine_pausieren_und_fortsetzen`, `::test_routine_loeschen_nur_nach_ja` |
| O1 | Nachtaufgabe im Fenster, Ergebnis morgens erwähnt | ✅ | `core/hintergrund.py`; `::test_nachtaufgabe_laeuft_nur_im_fenster`, `::test_ergebnis_am_morgen_und_im_naechsten_gespraech` |
| O1 | Tests inkl. Sommer-/Winterzeit | ✅ | `test_zeitplaner.py::test_wiederkehrend_ueber_die_zeitumstellung_hinweg`, `test_zeitangaben.py`, `::test_routine_ueber_die_zeitumstellung` |
| O1 | Verpasste Aufträge nachholen oder melden | ✅ | `::test_verpasste_erinnerung_wird_nach_dem_start_nachgeholt`, `::test_verpasste_routine_laeuft_nach_dem_neustart_einmal` |
| O2 | „Morgen 14 Uhr Zahnarzt, rot“ | ✅ | `test_kalender_konflikte.py::test_zahnarzt_morgen_14_uhr_rot` |
| O2 | Überschneidung vor dem Eintragen gemeldet, mit Vorschlag | ✅ | `::test_doppelbuchung_wird_vor_dem_eintragen_gemeldet`, `::test_konflikt_in_einem_anderen_kalender` |
| O2 | Tag ohne Lücke → Hinweis auf Pause | ✅ | `::test_tag_ohne_luecke_gibt_hinweis_auf_pause` |
| O2 | Löschen/Verschieben nur nach Ja | ✅ | `test_kalender.py::test_loeschen_nur_nach_ja`, `::test_loeschen_und_verschieben_sind_zerstoererisch` |
| O2 | Mehrere Kalender, Farben, Ortswechsel < 30 Min., Werte per Gespräch | ✅ | `test_kalender_verwaltung.py`, `::test_ortswechsel_braucht_30_minuten`, `::test_regeln_per_gespraech_aendern_und_gespeichert` |
| O3 | Satz im Gespräch → Aufgabe in Tasks | ✅ | `tools/todo_erkennung.py`; `test_notizen_dateien.py::test_satz_im_gespraech_legt_aufgabe_in_nextcloud_an` |
| O3 | „Schreib dir auf …“ → Notiz | ✅ | `::test_schreib_dir_auf_legt_notiz_an` |
| O3 | „Wo ist die Doku zu rp01?“ | ✅ | `::test_wo_ist_die_doku_zu_rp01` |
| O3 | Schreiben/Löschen ausserhalb `Missa/` unmöglich (Test) | ✅ | `::test_schreiben_ausserhalb_von_missa_ist_unmoeglich`, `::test_loeschen_gibt_es_nicht` |
| O3 | Deck nur wenn einfach | ✅ | bewusst weggelassen, begründet in `docs/nextcloud-dateien.md` |
| O4 | Geburtstag eines wichtigen Kontakts → Erinnerung am Vortag und Morgen | ✅ | `test_kontakte_wichtig.py::test_wichtiger_kontakt_vortag_und_morgen` |
| O4 | „Wann hab ich X zuletzt angerufen?“ | ✅ | `::test_wann_hab_ich_oma_zuletzt_angerufen`, `::test_telefonie_fuellt_letzten_kontakt` |
| O4 | Entwurf im Stil, Versand erst nach Ja | ✅ | `test_mail.py::test_entwurf_im_stil_wird_abgelegt_und_angezeigt`, `::test_erst_nach_ja_geht_die_mail_raus` |
| O4 | Senden ohne Bestätigung unmöglich (Test) | ✅ | `::test_kein_werkzeug_kann_senden`, `::test_modell_versucht_alles_und_nichts_geht_raus` |
| O5 | Nach Neustart Erinnerung an Gesagtes von vor zwei Tagen | ✅ | `test_gedaechtnis_sicherung.py::test_nach_neustart_weiss_missa_was_louis_vor_zwei_tagen_gesagt_hat` |
| O5 | Nach Korrektur beim nächsten Mal richtig | ✅ | `::test_nach_korrektur_beim_naechsten_mal_richtig` |
| O5 | „Vergiss X“ → nicht mehr abrufbar | 🟡 | Fakten und Nachrichten werden gelöscht; Text bleibt aber im Aktionsprotokoll (`merken`/`korrigieren`) und ist über `aktionen_anzeigen` und `/api/aktionen` abrufbar |
| O5 | Lokale Suche mit Embeddings auf der CPU | 🟡 | nur SQLite-Volltextsuche (FTS5/bm25), findet keine bedeutungsgleichen Wörter |
| O5 | Backup täglich, 14 behalten, ohne Geheimnisse, Wiederherstellen | ✅ | `core/sicherung.py`, `scripts/wiederherstellen.sh`; `::test_backup_auf_frischer_installation_wiederhergestellt` |
| O6 | „Welche IP hat rp01?“ mit Dateiname | ✅ | `test_doku_dokumente.py::test_welche_ip_hat_rp01_mit_dateiname` |
| O6 | Ohne Treffer keine erfundene Antwort | ✅ | `::test_ohne_treffer_weiss_ich_nicht` |
| O6 | PDF/URL/lange Mail → Zusammenfassung, Rückfrage, Stücke, Nachtschicht | ✅ | `::test_url_von_louis_wird_zusammengefasst`, `::test_pdf_aus_nextcloud`, `::test_sehr_langer_text_in_die_nachtschicht` |

### Server

| Gruppe | Punkt | Status | Fundstelle bzw. was fehlt |
|---|---|---|---|
| S1 | „Ruf mich an, wenn ai01 über 90 %“ → `notfall` | ✅ | `test_server_alarme.py::test_ruf_mich_an_wird_notfall_regel_und_loest_nach_5_minuten_aus` |
| S1 | Auffälliger Anstieg unter Schwellwert (7-Tage-Mittel) | ✅ | `::test_ungewoehnlicher_anstieg_unter_der_grenze_gibt_hinweis` |
| S1 | Ausfall und Rückkehr einer Domain | ✅ | `test_erreichbarkeit.py::test_ausfall_und_rueckkehr_werden_gemeldet` |
| S1 | Kein Alarm im Minutentakt | ✅ | `::test_derselbe_alarm_kommt_nicht_im_minutentakt` |
| S1 | Adapter Monitoring-Tool/Homepage, Proxmox nur lesend | ✅ | `integrationen/monitoring.py`, `docs/monitoring.md`, `scripts/einrichten/proxmox-lesetoken.sh` |
| S2 | „Welche Container laufen auf wb01?“ | ✅ | `test_container.py::test_welche_container_laufen_auf_wb01` |
| S2 | Neustart erst nach Ja, mit Protokoll | ✅ | `::test_neustart_erst_nach_ja_mit_protokoll` |
| S2 | „Warum geht Nextcloud nicht?“ | ✅ | `test_fehlersuche.py::test_warum_geht_nextcloud_nicht` |
| S2 | 10 fehlgeschlagene SSH-Logins → `wichtig` | ✅ | `test_anmeldungen.py::test_zehn_fehlgeschlagene_ssh_logins_sind_wichtig` |
| S2 | sudoers nur für die genannten Befehle (Test) | ✅ | `test_ssh.py::test_sudoers_erlaubt_nur_missa_docker_und_missa_update` (journalctl über Gruppe `systemd-journal`) |
| S3 | Updates pro VM, Sicherheitsupdates markiert | ✅ | `test_updates.py::test_welche_updates_stehen_an` |
| S3 | Wartungsfenster erst nach Ja | ✅ | `::test_wartungsfenster_erscheint_erst_nach_ja` |
| S3 | Updates und Neustart nur nach Ja pro VM, ai01 zuletzt | ✅ | `::test_updates_und_neustart_nur_nach_ja_pro_vm`, `::test_eigener_host_zuletzt_mit_warnung` |
| S3 | Zertifikat in 7 Tagen → Warnung, RDAP, DNS | ✅ | `test_zertifikate.py::test_zertifikat_mit_ablauf_in_7_tagen_warnt`, `::test_rdap_ablauf_und_bootstrap` |
| S4 | Neue VM nach Scan gemeldet | ✅ | `test_homelab.py::test_neue_vm_ip_aenderung_und_verschwunden` |
| S4 | „Welche IP hat rp01?“ | ✅ | `::test_welche_ip_hat_rp01` |
| S4 | Firewall-Zusammenfassung aus Beispiel-Logs (Test) | ✅ | `test_firewall.py::test_was_war_gestern_an_der_firewall_los` |
| S4 | Kein Tool ändert Firewall-Regeln (Test) | ✅ | `::test_es_gibt_kein_werkzeug_das_firewall_regeln_aendert` |
| S5 | Minecraft-Server: Plan → Ja → VM läuft | ✅ | `test_vms.py::test_richte_einen_minecraft_server_ein` |
| S5 | Über den Grenzen abgelehnt (Test) | ✅ | `::test_ueber_den_grenzen_abgelehnt_mit_begruendung` |
| S5 | Löschen ausserhalb des Pools unmöglich (Test) | ✅ | `::test_loeschen_ausserhalb_des_pools_unmoeglich` |
| S5 | Jeder Schritt im Aktionsprotokoll | ✅ | `::test_richte_einen_minecraft_server_ein` |
| S5 | Ergebnis als Eintrag ins Wissen | 🟡 | Code da (`tools/vms.py` ruft `homelab.scannen()`), aber kein Test |
| S6 | Subdomain erst nach Ja aktiv | ✅ | `test_caddy_minecraft.py::test_eintrag_erst_nach_ja_aktiv` |
| S6 | Ungültiger Eintrag nicht aktiv, Caddy läuft weiter | ✅ | `::test_ungueltiger_eintrag_wird_entfernt_caddy_laeuft_weiter` |
| S6 | „Wer ist online?“ | ✅ | `::test_wer_ist_online` |
| S6 | Neustart mit Spielern → Warnung | ✅ | `::test_neustart_mit_spielern_warnt_und_fragt_nach` |
| S7 | Issue auf dem Git-Server anlegen | ✅ | `test_git.py::test_issue_anlegen_im_repo` |
| S7 | Fehlgeschlagener GitHub-Build → Meldung | ✅ | `::test_fehlgeschlagener_build_wird_gemeldet` |
| S7 | Grobe Idee → Prompt, nach Ja als Issue | ✅ | `::test_grobe_idee_wird_prompt_nach_vorlage`, `::test_prompt_erst_nach_ja_als_issue` |

## Weitere Beobachtungen

- `tests/test_gedaechtnis.py::test_kontextvariable_bleibt_sauber` wird rot, wenn
  `tests/test_mail.py` vorher läuft (Kontextvariable `aktuelle_anfrage` wird nicht
  zurückgesetzt). In der normalen Reihenfolge grün; ein Problem der Testisolation.
