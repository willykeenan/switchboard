# Switchboard — Agents view

Read-only local process and session census for the board. This package does not
replace the board, workflow database or source owner.

The view shows recent Codex/Claude session activity from local event logs, an OS
process census, and optional CPU/GPU job history when those adapters are present.
Processes include other runtimes and unregistered work. Restricted fields and
unpublished progress cannot be invented.

```sh
python3 -m unittest discover -s agents/tests -v
```

`AgentsRoutes.handle_get(handler)` handles `/agents`, `/agents.css`, `/agents.js`
and `/api/agents`. It returns false for every unrelated route. The existing
server and port remain the only canonical service.

## Limits

* Session status is recent event metadata, not provider-side telemetry.
* Shared runtime PIDs are not attributed to individual tasks.
* Command arguments and environments never enter the API.
* All process information is OS-visible and read-only.
* Optional `psutil` enables the live process census; without it the census is empty.
