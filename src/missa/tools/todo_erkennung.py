"""Erkennt im Gespräch, dass Louis noch etwas tun muss („muss noch Rail-Kit bestellen“).

Das Modell läuft auf der CPU und ist klein – darum hilft der Code mit: Steht in
Louis' Nachricht ein To-do, kommt ein Hinweis in den Systemprompt, die Aufgabe mit
``aufgabe_erstellen`` anzulegen und das kurz zu erwähnen. Angelegt wird sie vom
Modell (als Werkzeug), damit Louis widersprechen kann und nichts doppelt entsteht.
"""

from __future__ import annotations

import re

from missa.core.werkzeuge import im_hintergrund

FUELLWOERTER = (
    r"(?:noch|unbedingt|dringend|endlich|heute|morgen|bald|mal|demnächst|nachher|später"
    r"|wieder|auch)"
)
MUSTER = [
    # „muss noch …“, „sollte morgen unbedingt …“ (mit oder ohne „ich“)
    re.compile(
        rf"\b(?:muss|müsste|sollte)\s+(?:{FUELLWOERTER}\s+)+(?P<was>[^.!?,;\n]{{3,80}})",
        re.IGNORECASE,
    ),
    # „ich muss Milch kaufen“
    re.compile(r"\bich\s+(?:muss|müsste|sollte)\s+(?P<was>[^.!?,;\n]{3,80})", re.IGNORECASE),
    re.compile(
        r"\b(?:nicht\s+vergessen|darf\s+nicht\s+vergessen)[,:]?\s+(?:dass\s+ich\s+|zu\s+)?"
        r"(?P<was>[^.!?,;\n]{3,80})",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:to-?do|aufgabe)\s*:\s*(?P<was>[^.!?\n]{3,80})", re.IGNORECASE),
]
#: Endungen, die zum Satz und nicht zur Aufgabe gehören.
SCHLUSS = re.compile(r"\s+(?:und\s+so|oder\s+so|irgendwie|eigentlich|halt|ja)$", re.IGNORECASE)
NICHT_AUFGABE = re.compile(
    r"^(?:los|weg|schlafen|arbeiten|gehen|aufhören|nicht|nichts|ich|du|wir|kurz weg)\b",
    re.IGNORECASE,
)


def todo_finden(text: str) -> str | None:
    """Die Aufgabe aus einem Satz wie „muss noch Rail-Kit bestellen“ – oder ``None``."""
    for satz in re.split(r"(?<=[.!?\n])\s+", str(text)):
        if satz.rstrip().endswith("?") or re.search(r"\bmuss\s+nicht\b", satz, re.IGNORECASE):
            continue
        for muster in MUSTER:
            if treffer := muster.search(satz):
                was = SCHLUSS.sub("", " ".join(treffer.group("was").split())).strip(" -")
                if len(was.split()) < 2 or NICHT_AUFGABE.match(was):
                    continue
                return was[0].upper() + was[1:]
    return None


def aufgaben_hinweis(frage: str) -> str | None:
    """Kontextquelle: Hinweis an das Modell, wenn Louis' Nachricht ein To-do enthält."""
    if im_hintergrund.get():
        return None
    if (was := todo_finden(frage)) is None:
        return None
    return (
        "## Mögliche Aufgabe\n"
        f"Louis hat gerade gesagt, dass er noch etwas tun muss: „{was}“. Lege dafür mit "
        "aufgabe_erstellen eine Aufgabe an (Titel kurz, Fälligkeit nur, wenn er eine nennt) und "
        "erwähne in deiner Antwort in einem kurzen Satz, dass du sie angelegt hast – dann kann "
        "er widersprechen. Meint er etwas anderes (z. B. nur eine Frage), lass es."
    )
