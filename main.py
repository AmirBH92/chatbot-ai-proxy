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

    return JSONResponse(intent)
