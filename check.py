#!/usr/bin/env python3
import json
import os
import http.client
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATE_FILE = Path("state.json")
DEFAULT_TOOL_IDS = [47]

TRANSIENT_STATUSES = {400, 403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

class SiteUnavailable(Exception):
    pass


class CampaignClosed(Exception):
    pass


CROUS_URL = os.environ.get("CROUS_URL", "").strip()
if not CROUS_URL:
    sys.exit(
        "CROUS_URL n'est pas defini.\n"
        "Settings > Secrets and variables > Actions > onglet Variables > New repository variable\n"
        "Name: CROUS_URL / Value: l'URL de la carte sur trouverunlogement.lescrous.fr"
    )

parsed = urlparse(CROUS_URL)

bounds_raw = parse_qs(parsed.query).get("bounds", [""])[0]
if not bounds_raw:
    sys.exit(
        f"CROUS_URL ne contient pas de parametre 'bounds': {CROUS_URL}\n"
        "Ouvre trouverunlogement.lescrous.fr, deplace/zoome la carte sur ta zone, "
        "puis recopie l'URL complete de la barre d'adresse."
    )

try:
    w, n, e, s = (float(x) for x in bounds_raw.split("_"))
except ValueError:
    sys.exit(
        f"Le parametre 'bounds' est mal forme: {bounds_raw!r}\n"
        "Format attendu: ouest_nord_est_sud (4 nombres separes par des '_')."
    )
location = [{"lon": w, "lat": n}, {"lon": e, "lat": s}]

# L'id de campagne se lit dans l'URL (.../tools/47/search?...). Il change quand le
# CROUS ouvre une nouvelle campagne, donc on le prend dans l'URL plutot qu'en dur.
match = re.search(r"/tools/(\d+)", parsed.path)
TOOL_IDS = [int(match.group(1))] if match else DEFAULT_TOOL_IDS
print(f"zone={bounds_raw} tools={TOOL_IDS}")


def query(tool_id):
    payload = {
        "idTool": tool_id,
        "need_aggregation": True,
        "page": 0,
        "pageSize": 24,
        "sector": None,
        "occupationModes": [],
        "location": location,
        "residence": None,
        "equipment": [],
        "price": {"max": 100000},
        "area": {"min": 0},
        "adaptedPmr": False,
    }
    req = urllib.request.Request(
        f"https://trouverunlogement.lescrous.fr/api/fr/search/{tool_id}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"tool {tool_id}: HTTP {exc.code} — {detail}", file=sys.stderr)
        # 404 = l'API ne connait plus cet id : la campagne CROUS est terminee.
        if exc.code == 404:
            raise CampaignClosed(str(tool_id)) from exc
        if exc.code in TRANSIENT_STATUSES:
            raise SiteUnavailable(f"tool {tool_id}: HTTP {exc.code}") from exc
        raise
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        raise SiteUnavailable(f"tool {tool_id}: {type(exc).__name__} {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SiteUnavailable(f"tool {tool_id}: non-JSON response") from exc
    results = data.get("results", {})
    total = results.get("total", {}).get("value", 0)
    items = [
        {
            "tool": tool_id,
            "id": it.get("id"),
            "address": it.get("residence", {}).get("address"),
            "rent": it.get("occupationModes", [{}])[0].get("rent", {}).get("min"),
            "bedroomCount": it.get("bedroomCount"),
            "url": f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{it.get('id')}",
        }
        for it in results.get("items", [])
    ]
    return total, items


def fmt(totals_dict):
    if not totals_dict:
        return "none"
    return ", ".join(f"tool{k}={v}" for k, v in sorted(totals_dict.items()))


def write_outputs(**values):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        return
    with open(gh_output, "a") as f:
        for key, value in values.items():
            text = str(value)
            if "\n" in text:
                f.write(f"{key}<<EOF\n{text}\nEOF\n")
            else:
                f.write(f"{key}={text}\n")


previous = {}
if STATE_FILE.exists():
    previous = json.loads(STATE_FILE.read_text())
previous_totals = previous.get("totals")
previous_status = previous.get("status", "ok")

totals = {}
items = []

try:
    for tid in TOOL_IDS:
        t, its = query(tid)
        totals[str(tid)] = t
        items.extend(its)
except SiteUnavailable as exc:
    print(f"skipping run, site unavailable: {exc}", file=sys.stderr)
    sys.exit(0)
except CampaignClosed as exc:
    # On sort en succes exprès : faire echouer le run enverrait un mail d'echec
    # GitHub a chaque declenchement, soit des centaines par jour. On previent
    # une seule fois, via l'etat, puis on reste silencieux.
    print(f"CAMPAGNE FERMEE: la campagne {exc} n'existe plus (HTTP 404).")
    print("Mets a jour CROUS_URL avec la nouvelle URL de trouverunlogement.lescrous.fr.")
    first_time = previous_status != "closed"
    STATE_FILE.write_text(
        json.dumps(
            {"status": "closed", "totals": previous_totals, "items": []},
            indent=2,
            ensure_ascii=False,
        )
    )
    write_outputs(
        changed="false",
        campaign_closed="true" if first_time else "false",
        tools=",".join(str(t) for t in TOOL_IDS),
    )
    sys.exit(0)

# On n'alerte que sur les hausses : une baisse (logement deja pris) n'appelle
# aucune action, et alerter dans les deux sens doublait les mails.
increased = previous_totals is not None and any(
    count > previous_totals.get(tool, 0) for tool, count in totals.items()
)
STATE_FILE.write_text(
    json.dumps({"status": "ok", "totals": totals, "items": items}, indent=2, ensure_ascii=False)
)

print(f"previous={fmt(previous_totals)} current={fmt(totals)} increased={increased}")

summary_lines = [
    f"- [tool {it['tool']}] {it['address']} ({it['bedroomCount']} lit(s), {it['rent']/100 if it['rent'] else 'N/A'}€)\n  🔗 {it['url']}"
    for it in items
]
write_outputs(
    changed="true" if increased else "false",
    campaign_closed="false",
    previous=fmt(previous_totals),
    total=fmt(totals),
    items="\n".join(summary_lines) if summary_lines else "(no listings)",
)
