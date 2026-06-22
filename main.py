"""
Chatbot AI Proxy — CO LABS CONSEILS
Intermediaire entre le widget Odoo frontend et l'API Google Gemini.

Endpoints :
  POST /api/query  -- traduit une question NL en intent JSON Odoo
  GET  /widget.js  -- sert le widget avec l'URL du proxy integree
  GET  /health     -- healthcheck
"""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

load_dotenv()

GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "https://chatbot.odoo.com").split(",")
ODOO_URL        = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_API_KEY    = os.environ.get("ODOO_API_KEY", "")

# URL publique du proxy
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
if not PUBLIC_URL:
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

app = FastAPI(title="Chatbot AI Proxy", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS + ["http://localhost:8069"],
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/widget.js", response_class=Response)
async def serve_widget():
    js_file = STATIC_DIR / "chatbot_widget.js"
    if not js_file.exists():
        raise HTTPException(404, detail="widget not found")
    content = js_file.read_text(encoding="utf-8")
    content = content.replace("%%PROXY_URL%%", PUBLIC_URL)
    return Response(content, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})


# ── Modèles Odoo exposés ──────────────────────────────────────────────────────
KNOWN_MODELS = {
    "sale.order":            "Commandes clients (sale.order)",
    "sale.order.line":       "Lignes de commande (sale.order.line)",
    "account.move":          "Factures/Avoirs (account.move)",
    "account.move.line":     "Lignes de facture (account.move.line)",
    "res.partner":           "Contacts/Clients (res.partner)",
    "product.template":      "Produits (product.template)",
    "product.product":       "Variantes produits (product.product)",
    "res.users":             "Utilisateurs (res.users)",
    "crm.lead":              "Opportunites CRM (crm.lead)",
    "purchase.order":        "Commandes fournisseurs (purchase.order)",
    "stock.picking":         "Transferts stock (stock.picking)",
    "project.task":          "Taches projet (project.task)",
    "hr.employee":           "Employes (hr.employee)",
    "account.analytic.line": "Lignes analytiques (account.analytic.line)",
}

SYSTEM_PROMPT_TPL = """Assistant analytique Odoo 19.3. Reponds UNIQUEMENT en JSON selon les types ci-dessous.

TYPES (choisir selon la question) :
count → "combien" : {{"type":"count","model":"M","domain":[...],"title":"T"}}
sum   → "CA/total" : {{"type":"sum","model":"M","domain":[...],"field":"amount_total","title":"T","unit":"EUR"}}
query → "liste/affiche" : {{"type":"query","model":"M","domain":[...],"fields":["name","partner_id","amount_total","state"],"field_labels":{{"name":"N","partner_id":"Client","amount_total":"Montant HT","state":"Statut"}},"orderby":"id desc","limit":10,"title":"T"}}
chart → "par X/graphique" : {{"type":"chart","chart_type":"bar","model":"M","domain":[...],"groupby":["partner_id"],"field":"amount_total","title":"T","x_label":"Client","y_label":"CA"}}
synthesis → "tableau de bord/synthese/bilan" : {{"type":"synthesis","title":"T","kpis":[{{"label":"CA","model":"sale.order","method":"sum","field":"amount_total","domain":[["state","in",["sale","done"]],["date_order",">=","{this_month}"]],"unit":"EUR"}},{{"label":"Commandes","model":"sale.order","method":"count","domain":[["state","=","sale"]]}}],"chart":{{"chart_type":"bar","model":"sale.order","domain":[["state","in",["sale","done"]]],"groupby":["partner_id"],"field":"amount_total","title":"Top clients"}}}}
multi → "vs/comparaison/taux/croisement/progression" : steps executes en sequence, $id reference un resultat precedent
  sum/count/query/chart/calc(formule arithmetique)/collect(liste ids pour filtrer)
  Ex: {{"type":"multi","title":"T","steps":[{{"id":"a","type":"sum","model":"sale.order","field":"amount_total","domain":[["date_order",">=","{this_month}"],["state","in",["sale","done"]]],"label":"CA mois","unit":"EUR"}},{{"id":"b","type":"sum","model":"sale.order","field":"amount_total","domain":[["date_order",">=","{last_month}"],["date_order","<","{this_month}"],["state","in",["sale","done"]]],"label":"CA mois prec","unit":"EUR"}},{{"id":"c","type":"calc","formula":"($a-$b)/$b*100","label":"Evolution","unit":"%"}}]}}
  collect+croisement: {{"id":"ids","type":"collect","model":"sale.order","domain":[["state","=","sale"]],"field":"partner_id","display":false}} puis {{"domain":[...["partner_id","in","$ids"]]}}
message → hors Odoo : {{"type":"message","message":"..."}}

REGLES :
- Dates ISO "2026-01-01" | sale.order state: draft/sent/sale/done/cancel | factures: move_type="out_invoice" state="posted"
- payment_state="not_paid"|"partial"|"paid" | OR: ["|",c1,c2] | AND implicite
- field_labels toujours en francais metier | unit="EUR" pour montants | limit 10 par defaut

MODELES : {models}
DATE : {now} | semaine: {week_start} | mois: {this_month} | mois prec: {last_month}
"""


class QueryRequest(BaseModel):
    question: str
    available_models: list[str] = []
    conversation_history: list[dict] = []


@app.get("/health")
async def health():
    info = {"status": "ok", "model": GEMINI_MODEL, "configured": bool(GEMINI_API_KEY)}
    if GEMINI_API_KEY:
        # Lister les modeles disponibles pour cette cle
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1/models",
                    params={"key": GEMINI_API_KEY, "pageSize": 20},
                )
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                info["available_models"] = models
            else:
                info["models_error"] = f"{r.status_code}: {r.text[:100]}"
        except Exception as e:
            info["models_error"] = str(e)
    return info


@app.post("/api/query")
async def query(req: QueryRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(503, detail="GEMINI_API_KEY non configuree sur le proxy.")

    # Modeles disponibles
    available = req.available_models or list(KNOWN_MODELS.keys())
    models_ctx = "\n".join(
        f"- {label}" for m, label in KNOWN_MODELS.items() if m in available
    )

    # Dates de reference
    now        = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    this_month = now.replace(day=1).strftime("%Y-%m-%d")
    last_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")

    system = SYSTEM_PROMPT_TPL.format(
        models=models_ctx,
        now=now.strftime("%Y-%m-%d %H:%M"),
        week_start=week_start,
        this_month=this_month,
        last_month=last_month,
    )

    # Construire le contenu multi-turn pour Gemini
    contents = []
    for turn in req.conversation_history[-6:]:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": req.question}]})

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.1,
            "responseMimeType": "application/json",  # Gemini force JSON valide
        },
    }

    endpoint = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    resp = None
    for attempt, wait in enumerate([0, 5, 15]):
        if wait:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                endpoint,
                params={"key": GEMINI_API_KEY},
                json=payload,
            )
        if resp.status_code not in (429, 503):
            break

    if resp.status_code != 200:
        if resp.status_code in (429, 503):
            raise HTTPException(503, detail="Le service IA est temporairement surchargee. Reessayez dans 30 secondes.")
        raise HTTPException(502, detail=f"Gemini error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        candidate = data["candidates"][0]
        # Verifier si la reponse a ete tronquee
        finish_reason = candidate.get("finishReason", "")
        raw = candidate["content"]["parts"][0]["text"]
        intent = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"type": "message", "message": "Je n'ai pas pu formuler une reponse complete. Essayez de reformuler votre question de facon plus simple."})
    except (KeyError, IndexError) as e:
        return JSONResponse({"type": "message", "message": "Le service IA n'a pas retourne de reponse valide. Reessayez."})
    _ = finish_reason  # utilisé si besoin de log futur

    # ── Exécution Odoo côté serveur ──────────────────────────────────────────
    async with httpx.AsyncClient(timeout=30) as odoo_client:
        result = await execute_odoo(odoo_client, intent)
    return JSONResponse(result)


# ── Helpers Odoo server-side ──────────────────────────────────────────────────

async def odoo_call(client: httpx.AsyncClient, model: str, method: str, **kwargs):
    if not ODOO_URL or not ODOO_API_KEY:
        raise HTTPException(503, detail="ODOO_URL ou ODOO_API_KEY non configure sur le proxy.")
    resp = await client.post(
        f"{ODOO_URL}/json/2/{model}/{method}",
        headers={"Authorization": f"Bearer {ODOO_API_KEY}", "Content-Type": "application/json"},
        json=kwargs,
    )
    if not resp.is_success:
        raise HTTPException(502, detail=f"Odoo {model}.{method} error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        msg = data["error"].get("data", {}).get("message") or data["error"].get("message", str(data["error"]))
        raise HTTPException(502, detail=f"Odoo error: {msg[:200]}")
    return data


def resolve_domain(domain: list, ctx: dict) -> list:
    out = []
    for cond in domain:
        if isinstance(cond, list) and len(cond) == 3:
            f, op, v = cond
            if isinstance(v, str) and v.startswith("$"):
                v = ctx.get(v[1:], v)
            out.append([f, op, v])
        else:
            out.append(cond)
    return out


def eval_formula(formula: str, ctx: dict) -> float:
    expr = formula
    for k, v in ctx.items():
        if isinstance(v, (int, float)):
            expr = re.sub(r"\$" + re.escape(k) + r"\b", str(v), expr)
    if not re.match(r"^[\d+\-*/().\s]+$", expr):
        return 0.0
    try:
        result = eval(expr)  # noqa: S307 — formule arithmetique uniquement
        return float(result) if result else 0.0
    except Exception:
        return 0.0


def fmt_records(records: list) -> list:
    out = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                row[k] = v[1]
            elif v is False:
                row[k] = None
            else:
                row[k] = v
        out.append(row)
    return out


async def execute_chart(client: httpx.AsyncClient, intent: dict) -> dict:
    groupby = intent.get("groupby", [])
    field   = intent.get("field", "amount_total")
    if not groupby:
        return {"type": "chart", "chart_type": "bar", "labels": [], "values": [], "title": intent.get("title", "")}

    groups = await odoo_call(client, intent["model"], "read_group",
                             domain=intent.get("domain", []),
                             fields=[field],
                             groupby=groupby,
                             lazy=False)

    label_fld = groupby[0]
    pairs = []
    for g in groups:
        lbl = g.get(label_fld, "N/A")
        if isinstance(lbl, list): lbl = lbl[1] if len(lbl) > 1 else lbl[0]
        if lbl is False or lbl is None: lbl = "Non défini"
        pairs.append((str(lbl), round(g.get(field, 0) * 100) / 100))
    pairs.sort(key=lambda x: x[1], reverse=True)
    pairs = pairs[:15]

    return {
        "type": "chart",
        "chart_type": intent.get("chart_type", "bar"),
        "labels": [p[0] for p in pairs],
        "values": [p[1] for p in pairs],
        "title":   intent.get("title", ""),
        "x_label": intent.get("x_label", ""),
        "y_label": intent.get("y_label", ""),
    }


async def execute_odoo(client: httpx.AsyncClient, intent: dict) -> dict:
    t = intent.get("type", "query")

    if t == "message":
        return intent

    if t == "count":
        n = await odoo_call(client, intent["model"], "search_count",
                            domain=intent.get("domain", []))
        return {"type": "count", "value": n, "title": intent.get("title", "")}

    if t == "sum":
        field  = intent.get("field", "amount_total")
        groups = await odoo_call(client, intent["model"], "read_group",
                                 domain=intent.get("domain", []),
                                 fields=[field], groupby=[], lazy=False)
        total = groups[0].get(field, 0) if groups else 0
        return {"type": "sum", "value": total, "title": intent.get("title", ""), "unit": intent.get("unit", "")}

    if t == "query":
        fields  = intent.get("fields", ["name"])
        records = await odoo_call(client, intent["model"], "search_read",
                                  domain=intent.get("domain", []),
                                  fields=fields,
                                  order=intent.get("orderby", "id desc"),
                                  limit=min(int(intent.get("limit", 10)), 100))
        return {"type": "query", "records": fmt_records(records), "fields": fields,
                "field_labels": intent.get("field_labels", {}),
                "title": intent.get("title", ""), "total": len(records)}

    if t == "chart":
        return await execute_chart(client, intent)

    if t == "synthesis":
        kpis = []
        for kpi in intent.get("kpis", []):
            method = kpi.get("method", "count")
            if method == "count":
                v = await odoo_call(client, kpi["model"], "search_count",
                                    domain=kpi.get("domain", []))
                kpis.append({"label": kpi["label"], "value": v, "unit": "", "valueType": "count"})
            else:
                field  = kpi.get("field", "amount_total")
                groups = await odoo_call(client, kpi["model"], "read_group",
                                         domain=kpi.get("domain", []),
                                         fields=[field], groupby=[], lazy=False)
                total = groups[0].get(field, 0) if groups else 0
                kpis.append({"label": kpi["label"], "value": total, "unit": kpi.get("unit", "EUR"), "valueType": "sum"})

        chart_data = None
        if intent.get("chart"):
            chart_data = await execute_chart(client, intent["chart"])

        return {"type": "synthesis", "title": intent.get("title", ""), "kpis": kpis, "chartData": chart_data}

    if t == "multi":
        ctx   = {}
        items = []
        for step in intent.get("steps", []):
            st     = step.get("type")
            domain = resolve_domain(step.get("domain", []), ctx)
            show   = step.get("display", True)

            if st == "collect":
                field = step.get("field", "partner_id")
                recs  = await odoo_call(client, step["model"], "search_read",
                                        domain=domain, fields=[field], limit=2000)
                ids = list({
                    r[field][0] if isinstance(r[field], list) else r[field]
                    for r in recs if r[field] not in (False, None)
                })[:500]
                ctx[step["id"]] = ids

            elif st == "sum":
                field  = step.get("field", "amount_total")
                groups = await odoo_call(client, step["model"], "read_group",
                                         domain=domain, fields=[field], groupby=[], lazy=False)
                val = groups[0].get(field, 0) if groups else 0
                ctx[step["id"]] = val
                if show: items.append({"type": "kpi", "label": step.get("label", ""), "value": val, "unit": step.get("unit", "EUR")})

            elif st == "count":
                val = await odoo_call(client, step["model"], "search_count", domain=domain)
                ctx[step["id"]] = val
                if show: items.append({"type": "kpi", "label": step.get("label", ""), "value": val, "unit": ""})

            elif st == "calc":
                val = eval_formula(step.get("formula", "0"), ctx)
                ctx[step["id"]] = val
                if show: items.append({"type": "calc", "label": step.get("label", ""), "value": val, "unit": step.get("unit", "")})

            elif st == "query":
                fields  = step.get("fields", ["name"])
                recs    = await odoo_call(client, step["model"], "search_read",
                                          domain=domain, fields=fields,
                                          order=step.get("orderby", "id desc"),
                                          limit=min(int(step.get("limit", 15)), 100))
                fmt = fmt_records(recs)
                ctx[step["id"]] = fmt
                if show: items.append({"type": "query", "title": step.get("label", ""),
                                        "fields": fields, "field_labels": step.get("field_labels", {}),
                                        "records": fmt, "total": len(fmt)})

            elif st == "chart":
                cd = await execute_chart(client, {**step, "domain": domain})
                ctx[step["id"]] = cd
                if show: items.append(cd)

        return {"type": "multi", "title": intent.get("title", ""), "items": items}

    return {"type": "error", "content": f"Type non reconnu : {t}"}
