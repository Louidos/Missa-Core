# Monitoring-Quellen: Schnittstelle und Formate

Die Server-Alarme ([server-alarme.md](server-alarme.md)) arbeiten mit einer einfachen
Schnittstelle: Jede Quelle liefert eine Liste von **Messwerten**.

```python
@dataclass(frozen=True)
class Messwert:
    ziel: str  # VM, Host oder Dienst, z. B. "ai01" (klein geschrieben)
    metrik: str  # "cpu", "ram", "disk", "laeuft", "erreichbar", "antwortzeit" oder frei
    wert: float  # Zahl; cpu/ram/disk in Prozent, laeuft/erreichbar 1 oder 0
    einheit: str  # "%", "ms" oder ""
    quelle: str  # Name der Quelle
```

Eine neue Quelle ist eine Klasse mit `name` und `async def messwerte() -> list[Messwert]`,
die bei Problemen `QuellenFehler` wirft (siehe `src/missa/integrationen/monitoring.py`).
Sie wird in `server_waechter_bauen` (`src/missa/tools/__init__.py`) eingehängt.

## Eigenes Monitoring-Tool (`MONITORING_URL`)

> **Offen:** Das genaue Format von Louis' Monitoring-Tool ist noch nicht bekannt. Missa
> versteht darum die zwei einfachen Formate unten. Liefert das Tool etwas anderes, schreibt
> Missa beim Abruf „Format nicht erkannt“ ins Log und meldet die Quelle nach 15 Minuten als
> „antwortet nicht“. Dann bitte ein Beispiel der Antwort schicken, damit der Adapter
> angepasst werden kann.

Missa ruft `GET MONITORING_URL` auf (mit `Authorization: Bearer MONITORING_TOKEN`, falls
gesetzt) und erwartet JSON in einem dieser Formate.

**Liste** (empfohlen):

```json
{
  "messwerte": [
    {"ziel": "ai01", "messwert": "cpu", "wert": 93.5},
    {"ziel": "ai01", "messwert": "ram", "wert": 41},
    {"ziel": "int01", "messwert": "temperatur", "wert": 51, "einheit": "°C"}
  ]
}
```

Statt `messwerte` gehen auch `metrics`, `werte`, `values` oder `data`, oder die Liste direkt
ohne Hülle. Feldnamen auch auf Englisch: `host`/`name`/`target` für das Ziel,
`metric`/`type`/`key` für den Messwert, `value` für den Wert, `unit` für die Einheit.

**Verschachtelt:**

```json
{
  "ai01": {"cpu": 93.5, "ram": 41, "disk": 62},
  "wb01": {"cpu": 12, "ram": 70}
}
```

Regeln:

- `cpu`, `ram`, `disk` sind **Prozent** (0–100). Namen wie „Auslastung“, „memory_percent“,
  „Festplatte“ werden erkannt; alles andere bleibt als eigener Name (z. B. `temperatur`)
  und lässt sich ebenso in Regeln verwenden.
- Zahlen dürfen Text sein („41,5“ oder „93 %“). Einträge ohne Zahl werden übersprungen.
- Hinweise auf Auffälliges gibt es nur für Prozentwerte.

## Homepage-Dashboard (`HOMEPAGE_URL`)

> **Annahme, bitte prüfen:** Missa nutzt die internen JSON-Endpunkte, die auch die
> Weboberfläche von [Homepage](https://gethomepage.dev) verwendet. Sie sind nicht offiziell
> dokumentiert und können sich mit einer neuen Homepage-Version ändern.

1. `GET HOMEPAGE_URL/api/services`: Gruppen mit Diensten (auch verschachtelte Gruppen).
2. Für jeden Dienst mit `siteMonitor` in der `services.yaml`:
   `GET HOMEPAGE_URL/api/siteMonitor?groupName=<Gruppe>&serviceName=<Dienst>`. Die Antwort
   sieht so aus: `{"status": 200, "latency": 87.4}`.

Daraus werden pro Dienst `erreichbar` (1, wenn Status 1–499; sonst 0) und `antwortzeit` (ms).
Dienste ohne `siteMonitor` werden übersprungen. Ab Homepage 1.0 muss die Adresse, unter der
ai01 Homepage aufruft, in `HOMEPAGE_ALLOWED_HOSTS` stehen.

## Proxmox (`PROXMOX_URL`)

`GET PROXMOX_URL/api2/json/cluster/resources` mit dem nur lesenden Token
(`Authorization: PVEAPIToken=<TOKEN_ID>=<SECRET>`). Pro Eintrag vom Typ `qemu`, `lxc` und
`node`:

| Messwert | Aus Proxmox |
|----------|-------------|
| `laeuft` | `status` ist `running` (VM, Container) bzw. `online` (Host) |
| `cpu` | `cpu × 100` |
| `ram` | `mem / maxmem × 100` |
| `disk` | `disk / maxdisk × 100`, nur wenn Proxmox `disk` kennt (bei VMs meist nicht) |

Ziel ist der Name der VM (`name`) bzw. des Hosts (`node`), klein geschrieben.
