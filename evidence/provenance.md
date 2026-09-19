---
title: "Entwicklung und Priorit\u00e4t"
created: 2026-09-08
updated: 2026-09-08
type: evidence
tags: [research]
sources: ["notes/discussion-001.md"]
---

# Entwicklungsnachweise

## Aktueller Stand
Jan berichtet eine Implementierung vor vielen Monaten und erwartet einen frühen Konzept-Commit. Repository ist jetzt lokal unter /home/jan/Work/arios verfügbar. Erste historische Quellbelege wurden gefunden; keine weltweite Priorität behaupten.

## Erste Git-Befunde
Alle folgenden Zeiten sind Git-Metadaten in UTC, kein unabhängiger Nachweis öffentlicher Verfügbarkeit.

| Commit | Author- und Committer-Zeit | Befund |
|---|---|---|
| `612c283acd9b2f2b74bd15e8852bc8790a2dd7c0` | 2026-02-22T21:07:11Z | `deactivate_skill(skill_name, extract)` und ursprüngliches `read_inactive_context` vorhanden. Delegierte Sichtung findet außerdem referenztragende ACC-Stubs und Skill-Scope-Ersatz durch Extract. |
| `976c2a7198d0127ad16f3141c95b651b36d7fc5e` | 2026-02-27T00:14:58Z | `forget_search`, `forget_apply(tool_call_ids, reason)`, Archivierung und `[forgotten: ... reason ... ref:...]` im Quelltext. |
| `a7afc1d3c39284f470c6d7817a9c863e14c16842` | 2026-03-05T08:32:42Z | Delegierte Sichtung findet Konfigurationseinbindung, Snapshot-Processor und Forget-Cleanup. Commit-Metadaten direkt gegengeprüft. |

Historische Pfade: `arios_platform/arios_platform/tools/forget/__init__.py`, `arios_platform/arios_platform/tools/skills/__init__.py`, `arios_platform/tools/context/__init__.py` (alte Lage), `arios_platform/arios_platform/context/cf.py`, `arios_platform/arios_platform/context/acc.py`.

Die ersten beiden Funktionssignaturen, der Reason-Stub, Archivierung und die alte Recovery-Funktion wurden zusätzlich zur delegierten Analyse direkt mit git show geprüft.

## Integrationsvorbehalt
Laut delegierter Historienanalyse importiert der Stand vom 27. Februar bereits Snapshot/Cleanup-Helfer, deren Implementierung in der untersuchten HEAD-Historie erst am 5. März auftaucht. Der frühe Quelltext ist deshalb kein Nachweis eines damals ausführbaren Gesamtsystems. Keine historische Version wurde ausgeführt.

Die Commit-Nachricht vom 5. März nennt Wiederherstellung eines Standes vom 3. März. Das ist keine unabhängig geprüfte Datierung. Parallele/duplizierte Historien enthalten andere Hashes; hier werden Vorfahren des Referenz-HEAD verwendet.

Recovery wurde laut delegierter Analyse mit `cf2c56803fffd753c47e4aed4b53c77b5733425e` im August in `context_recover` umbenannt; nicht mit erstmaliger Einführung verwechseln.

## Zu sichern, sobald der Repo-Pfad vorliegt
- Exakte Commit-IDs, Diffs und damalige Dateien; Author- und Committer-Zeit getrennt erfassen.
- Spätere Änderungen und heutige Implementierung vom damaligen Funktionsumfang trennen.
- Unabhängig datierbare Belege, etwa damalige öffentliche Pushes, Releases, Nachrichten oder archivierte Veröffentlichungen, falls vorhanden und zur Einsicht freigegeben.
- Damalige Rechercheunterlagen samt Umfang und Zeitpunkt.

## Aussagegrenzen
Lokale Git-Zeitstempel sind veränderbar. Ein alter Commit kann Entwicklung dokumentieren, beweist allein aber weder unabhängige Datierung noch weltweite Erstentdeckung. Unabhängige Entwicklung, erste öffentliche Offenlegung und Neuheit gegenüber verwandten Arbeiten sind unterschiedliche Fragen.

[Start](../index.md) · [Gespräch](../notes/discussion-001.md) · [Literatur](../literature/candidates.md)
