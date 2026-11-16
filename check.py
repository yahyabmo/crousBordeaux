#!/usr/bin/env python3
import html
import json
import os
import http.client
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATE_FILE = Path("state.json")
DEFAULT_TOOL_IDS = [47]

TRANSIENT_STATUSES = {400, 403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# Un 404 isole est une panne passagere de l'API, pas une fin de campagne.
# On n'alerte qu'apres ce nombre de 404 consecutifs (~1h au rythme de 2 min).
NOT_FOUND_STREAK_ALERT = 30

# Une annonce absente d'un seul releve peut etre un creux passager de l'API
# (reponse 200 mais liste vide). On ne declare une disparition qu'apres ce
# nombre de releves consecutifs sans elle : ~4 min au rythme de 2 min.
GONE_CONFIRM_RUNS = 2


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

NOW = datetime.now(timezone.utc)


def parse_zone(url):
    """Une URL de carte du CROUS -> une zone interrogeable."""
    parsed = urlparse(url)

    bounds_raw = parse_qs(parsed.query).get("bounds", [""])[0]
    if not bounds_raw:
        sys.exit(
            f"Cette URL ne contient pas de parametre 'bounds': {url}\n"
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

    # L'id de campagne se lit dans l'URL (.../tools/47/search?...). Il change quand
    # le CROUS ouvre une nouvelle campagne, donc on le prend dans l'URL.
    match = re.search(r"/tools/(\d+)", parsed.path)
    tool_id = int(match.group(1)) if match else DEFAULT_TOOL_IDS[0]

    label = parse_qs(parsed.query).get("locationName", [""])[0].strip() or bounds_raw
    return {
        "label": label,
        "url": url,
        # "Pessac (33600)" -> "Pessac" : ce qui s'affiche dans les titres.
        "city": re.sub(r"\s*\(.*\)\s*$", "", label).strip() or label,
        "tool_id": tool_id,
        "location": [{"lon": w, "lat": n}, {"lon": e, "lat": s}],
    }


# Plusieurs zones se mettent bout a bout dans CROUS_URL, separees par un espace,
# un retour a la ligne ou une virgule : une URL de carte par commune surveillee.
ZONES = [parse_zone(u) for u in re.split(r"[\s,]+", CROUS_URL) if u]
print("zones: " + " | ".join(f"{z['label']} (tool {z['tool_id']})" for z in ZONES))


def query(zone):
    tool_id = zone["tool_id"]
    payload = {
        "idTool": tool_id,
        "need_aggregation": True,
        "page": 0,
        "pageSize": 24,
        "sector": None,
        "occupationModes": [],
        "location": zone["location"],
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

    # L'API repond "[]" (une liste, pas un objet) quand l'id de campagne n'existe
    # pas du tout. Ce n'est pas une reponse exploitable.
    if not isinstance(data, dict):
        raise CampaignClosed(str(tool_id))

    results = data.get("results") or {}
    total = (results.get("total") or {}).get("value", 0)
    items = [
        {
            "tool": tool_id,
            "id": it.get("id"),
            "address": it.get("residence", {}).get("address"),
            "city": zone["city"],
            "rent": (it.get("occupationModes") or [{}])[0].get("rent", {}).get("min"),
            "bedroomCount": it.get("bedroomCount"),
            "url": f"https://trouverunlogement.lescrous.fr/tools/{tool_id}/accommodations/{it.get('id')}",
        }
        for it in (results.get("items") or [])
    ]
    return total, items


def fmt(totals_dict):
    if not totals_dict:
        return "none"
    return " | ".join(f"{k}: {v}" for k, v in sorted(totals_dict.items()))


def write_outputs(**values):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        for key, value in values.items():
            print(f"--- {key} ---\n{value}")
        return
    with open(gh_output, "a") as f:
        for key, value in values.items():
            text = str(value)
            if "\n" in text:
                f.write(f"{key}<<__CROUS_EOF__\n{text}\n__CROUS_EOF__\n")
            else:
                f.write(f"{key}={text}\n")


# ---------------------------------------------------------------- mise en forme

def money(cents):
    """62505 -> '625,05 EUR' ; 62500 -> '625 EUR'. Le CROUS renvoie des centimes."""
    if not cents:
        return "loyer non precise"
    euros, rest = divmod(int(cents), 100)
    amount = f"{euros}" if rest == 0 else f"{euros},{rest:02d}"
    return f"{amount} €/mois"


def beds(n):
    if not n:
        return None
    return f"{n} lit" if n == 1 else f"{n} lits"


def duration(since_iso):
    """Depuis quand l'annonce etait en ligne, en francais court."""
    if not since_iso:
        return None
    try:
        since = datetime.fromisoformat(since_iso)
    except ValueError:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    minutes = int((NOW - since).total_seconds() // 60)
    if minutes < 1:
        return "moins d'une minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {mins:02d}" if mins else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h" if hours else f"{days} j"


def cities_of(items):
    """['Pessac', 'Talence'] -> 'Pessac et Talence'. Ordre d'apparition conserve."""
    seen, out = set(), []
    for it in items:
        city = it.get("city")
        if city and city not in seen:
            seen.add(city)
            out.append(city)
    if not out:
        return "ta zone"
    if len(out) == 1:
        return out[0]
    return ", ".join(out[:-1]) + " et " + out[-1]


def line_of(it):
    """'Pessac · 1 lit · 625 EUR/mois'. La ville saute si l'adresse la porte deja."""
    city = it.get("city") or ""
    address = (it.get("address") or "").upper()
    if city and city.upper() in address:
        city = ""
    bits = [city, beds(it.get("bedroomCount")), money(it.get("rent"))]
    if it.get("lasted"):
        bits.append(f"resté {it['lasted']}")
    return " · ".join(b for b in bits if b)


CARD = """      <tr><td style="padding:0 0 12px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border:1px solid #e3e6ea;border-radius:10px;">
          <tr><td style="padding:16px 18px;">
            <div style="font:600 16px/1.35 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#16191d;">{address}</div>
            <div style="font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#5c6670;padding-top:4px;">{line}</div>
            {button}
          </td></tr>
        </table>
      </td></tr>"""

BUTTON = """<div style="padding-top:14px;"><a href="{url}" style="display:inline-block;background:#1b5e9e;color:#ffffff;text-decoration:none;font:600 14px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:11px 18px;border-radius:7px;">Voir l'annonce</a></div>"""


ZONE_BUTTON = """<a href="{url}" style="display:inline-block;background:#ffffff;border:1px solid #c4ccd4;color:#1b5e9e;text-decoration:none;font:600 14px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:10px 16px;border-radius:7px;margin:0 8px 8px 0;">{label}</a>"""


def zone_buttons_html(totals):
    """Un bouton par commune vers sa page de recherche, avec le total du moment."""
    if not ZONES:
        return ""
    buttons = []
    for zone in ZONES:
        count = totals.get(zone["label"])
        if count is None:
            label = f"Tout voir à {zone['city']}"
        elif count == 0:
            label = f"{zone['city']} · aucun"
        else:
            label = f"{zone['city']} · {count} logement" + ("s" if count > 1 else "")
        buttons.append(ZONE_BUTTON.format(url=html.escape(zone["url"], quote=True), label=html.escape(label)))
    return (
        '    <tr><td style="padding:6px 0 2px 0;font:600 13px/1.6 -apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#5c6670;\">Voir par moi-même sur le site du CROUS</td></tr>\n"
        '    <tr><td style="padding:8px 0 4px 0;">' + "".join(buttons) + "</td></tr>"
    )


def zone_buttons_text(totals):
    if not ZONES:
        return ""
    lines = ["Voir par moi-meme sur le site du CROUS :"]
    for zone in ZONES:
        count = totals.get(zone["label"])
        suffix = "" if count is None else f" ({count})"
        lines.append(f"  {zone['city']}{suffix} : {zone['url']}")
    return "\n".join(lines)


def html_mail(title, intro, items, footer, with_button=True, zones_block=""):
    cards = "\n".join(
        CARD.format(
            address=html.escape(it.get("address") or "Adresse non precisee"),
            line=html.escape(line_of(it)),
            button=BUTTON.format(url=html.escape(it.get("url") or "", quote=True)) if with_button else "",
        )
        for it in items
    )
    return f"""<!doctype html>
<html lang="fr"><body style="margin:0;padding:24px 12px;background:#f4f6f8;">
  <table role="presentation" cellpadding="0" cellspacing="0" align="center" width="100%" style="max-width:540px;margin:0 auto;">
    <tr><td style="padding:0 0 4px 0;font:700 21px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#16191d;">{html.escape(title)}</td></tr>
    <tr><td style="padding:0 0 18px 0;font:400 15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#5c6670;">{html.escape(intro)}</td></tr>
{cards}
{zones_block}
    <tr><td style="padding:14px 2px 0 2px;border-top:1px solid #e3e6ea;font:400 13px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#8a949e;">{footer}</td></tr>
  </table>
</body></html>"""


def text_mail(intro, items, footer, with_url=True, zones_block=""):
    lines = [intro, ""]
    for it in items:
        lines.append(f"• {it.get('address') or 'Adresse non precisee'}")
        lines.append(f"  {line_of(it)}")
        if with_url and it.get("url"):
            lines.append(f"  {it['url']}")
        lines.append("")
    if zones_block:
        lines.append(zones_block)
        lines.append("")
    lines.append(re.sub(r"<[^>]+>", "", footer.replace("<br>", "\n")))
    return "\n".join(lines)


# ------------------------------------------------------------------- etat avant

previous = {}
if STATE_FILE.exists():
    previous = json.loads(STATE_FILE.read_text())
previous_totals = previous.get("totals")
previous_items = {str(it["id"]): it for it in previous.get("items", []) if it.get("id") is not None}
# Annonces absentes des derniers releves, en attente de confirmation.
pending_gone = {str(k): v for k, v in (previous.get("pending_gone") or {}).items()}

totals = {}
items = []

seen = set()
try:
    for zone in ZONES:
        t, its = query(zone)
        totals[zone["label"]] = t
        # Deux zones qui se chevauchent peuvent renvoyer la meme annonce.
        for it in its:
            if str(it["id"]) not in seen:
                seen.add(str(it["id"]))
                items.append(it)
except SiteUnavailable as exc:
    print(f"skipping run, site unavailable: {exc}", file=sys.stderr)
    sys.exit(0)
except CampaignClosed as exc:
    # Un 404 peut etre une panne passagere : on ne crie qu'au bout de plusieurs
    # echecs d'affilee. On sort toujours en succes, sinon GitHub envoie un mail
    # d'echec a chaque declenchement (plus de 700 par jour au rythme de 2 min).
    streak = previous.get("not_found_streak", 0) + 1
    alert = streak == NOT_FOUND_STREAK_ALERT
    state = dict(previous)
    state["not_found_streak"] = streak
    if alert:
        state["status"] = "closed"
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"HTTP 404 sur la campagne {exc} ({streak}/{NOT_FOUND_STREAK_ALERT} d'affilee)")
    write_outputs(
        changed="false",
        gone="false",
        campaign_closed="true" if alert else "false",
        tools=",".join(sorted({str(z["tool_id"]) for z in ZONES})),
    )
    sys.exit(0)

# ------------------------------------------------- derivee positive et negative

current_ids = {str(it["id"]) for it in items}

new_items = []
for it in items:
    key = str(it["id"])
    was_pending = pending_gone.pop(key, None)
    if key in previous_items:
        # Deja vue au releve precedent : on garde sa date de premiere apparition.
        it["first_seen"] = previous_items[key].get("first_seen") or NOW.isoformat()
    elif was_pending:
        # Elle avait disparu d'un ou deux releves puis est revenue : c'est un
        # creux de l'API, pas une nouvelle annonce. Pas d'alerte.
        it["first_seen"] = was_pending.get("first_seen") or NOW.isoformat()
        print(f"annonce {key} reapparue apres {was_pending.get('missing_runs')} releve(s) — pas d'alerte")
    else:
        it["first_seen"] = NOW.isoformat()
        new_items.append(it)

# Les annonces du releve precedent qui ne sont plus la entrent en quarantaine.
for key, it in previous_items.items():
    if key not in current_ids and key not in pending_gone:
        entry = dict(it)
        entry["missing_runs"] = 0
        pending_gone[key] = entry

gone_items = []
still_pending = {}
for key, entry in pending_gone.items():
    entry = dict(entry)
    entry["missing_runs"] = entry.get("missing_runs", 0) + 1
    if entry["missing_runs"] >= GONE_CONFIRM_RUNS:
        entry["lasted"] = duration(entry.get("first_seen"))
        gone_items.append(entry)
    else:
        still_pending[key] = entry

STATE_FILE.write_text(
    json.dumps(
        {
            "status": "ok",
            "not_found_streak": 0,
            "totals": totals,
            "seen_ids": sorted(current_ids),
            "items": items,
            "pending_gone": still_pending,
        },
        indent=2,
        ensure_ascii=False,
    )
)

print(
    f"previous={fmt(previous_totals)} current={fmt(totals)} "
    f"nouveaux={len(new_items)} partis={len(gone_items)} en_attente={len(still_pending)}"
)

# ------------------------------------------------------------------- les envois

footer = (
    f"Total surveillé : {html.escape(fmt(totals))}<br>Avant : {html.escape(fmt(previous_totals))}"
)
zones_html = zone_buttons_html(totals)
zones_txt = zone_buttons_text(totals)

outputs = {
    "changed": "true" if new_items else "false",
    "gone": "true" if gone_items else "false",
    "campaign_closed": "false",
    "count": str(len(new_items)),
    "gone_count": str(len(gone_items)),
    "previous": fmt(previous_totals),
    "total": fmt(totals),
    "tools": ",".join(sorted({str(z["tool_id"]) for z in ZONES})),
}

if new_items:
    where = cities_of(new_items)
    if len(new_items) == 1:
        it = new_items[0]
        outputs["subject_new"] = f"CROUS · 1 logement à {where} — {money(it.get('rent'))}"
        intro = "Une annonce vient d'apparaître. Elle peut partir en quelques minutes."
    else:
        outputs["subject_new"] = f"CROUS · {len(new_items)} logements à {where}"
        intro = f"{len(new_items)} annonces viennent d'apparaître. Elles peuvent partir en quelques minutes."
    outputs["html_new"] = html_mail(f"Crous à {where}", intro, new_items, footer, zones_block=zones_html)
    outputs["text_new"] = text_mail(intro, new_items, footer, zones_block=zones_txt)

if gone_items:
    where = cities_of(gone_items)
    if len(gone_items) == 1:
        lasted = gone_items[0].get("lasted")
        suffix = f" — resté en ligne {lasted}" if lasted else ""
        outputs["subject_gone"] = f"CROUS · parti à {where}{suffix}"
    else:
        outputs["subject_gone"] = f"CROUS · {len(gone_items)} logements partis à {where}"
    intro = (
        "Ces annonces ne sont plus en ligne. Rien à faire — "
        "c'est pour savoir combien de temps une offre tient."
    )
    outputs["html_gone"] = html_mail(f"Parti à {where}", intro, gone_items, footer, with_button=False, zones_block=zones_html)
    outputs["text_gone"] = text_mail(intro, gone_items, footer, with_url=False, zones_block=zones_txt)

write_outputs(**outputs)
