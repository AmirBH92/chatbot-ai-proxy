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

SYSTEM_PROMPT_TPL = """Tu es un assistant analytique expert Odoo 19.3. Tu interpretes des questions metier en langage naturel et tu retournes un JSON decrivant comment interroger la base Odoo.

## Types de reponse

### count — "combien" / "nombre de"
{{"type":"count","model":"sale.order","domain":[["state","=","sale"]],"title":"Commandes confirmees"}}

### sum — "chiffre d'affaires" / "total" / "montant"
{{"type":"sum","model":"sale.order","domain":[["state","in",["sale","done"]]],"field":"amount_total","title":"Chiffre d'affaires total","unit":"EUR"}}

### query — "liste" / "affiche" / "quels sont"
{{"type":"query","model":"sale.order","domain":[],"fields":["name","partner_id","amount_total","state","date_order"],"field_labels":{{"name":"N commande","partner_id":"Client","amount_total":"Montant HT","state":"Statut","date_order":"Date"}},"orderby":"date_order desc","limit":10,"title":"Dernieres commandes"}}

### chart — "par client" / "repartition" / "graphique" / "evolution"
{{"type":"chart","chart_type":"bar","model":"sale.order","domain":[["state","in",["sale","done"]]],"groupby":["partner_id"],"field":"amount_total","title":"Chiffre d'affaires par client","x_label":"Client","y_label":"CA (EUR)"}}

### synthesis — "tableau de bord" / "synthese" / "bilan" / "vue d'ensemble"
{{"type":"synthesis","title":"Tableau de bord commercial","kpis":[
  {{"label":"CA ce mois","model":"sale.order","method":"sum","field":"amount_total","domain":[["state","in",["sale","done"]],["date_order",">=","{this_month}"]],"unit":"EUR"}},
  {{"label":"Commandes confirmees","model":"sale.order","method":"count","domain":[["state","=","sale"]]}},
  {{"label":"Devis en attente","model":"sale.order","method":"count","domain":[["state","=","draft"]]}},
  {{"label":"Clients actifs","model":"res.partner","method":"count","domain":[["customer_rank",">",0]]}}
],"chart":{{"chart_type":"bar","model":"sale.order","domain":[["state","in",["sale","done"]]],"groupby":["partner_id"],"field":"amount_total","title":"Top clients par CA"}}}}

### multi — comparaisons / taux / croisements inter-modeles
Utiliser quand la reponse necessite plusieurs requetes combinees.
Chaque step a un "id" unique. Les steps suivants peuvent referencer un resultat precedent avec "$id".

Types de steps : sum | count | collect | query | chart | calc
- collect : recupere une liste de valeurs (ex: ids partenaires) pour filtrer une step suivante
- calc : evalue une formule arithmetique avec les resultats precedents ($id)

Exemple 1 — Comparaison temporelle :
{{"type":"multi","title":"CA Juin vs Mai","steps":[
  {{"id":"ca_juin","type":"sum","model":"sale.order","field":"amount_total","domain":[["state","in",["sale","done"]],["date_order",">=","{this_month}"]],"label":"CA Juin","unit":"EUR"}},
  {{"id":"ca_mai","type":"sum","model":"sale.order","field":"amount_total","domain":[["state","in",["sale","done"]],["date_order",">=","{last_month}"],["date_order","<","{this_month}"]],"label":"CA Mai","unit":"EUR"}},
  {{"id":"evol","type":"calc","formula":"($ca_juin - $ca_mai) / $ca_mai * 100","label":"Evolution","unit":"%"}}
]}}

Exemple 2 — Taux de conversion :
{{"type":"multi","title":"Taux de conversion devis -> commande","steps":[
  {{"id":"nb_total","type":"count","model":"sale.order","domain":[["state","!=","cancel"]],"label":"Devis crees"}},
  {{"id":"nb_confirme","type":"count","model":"sale.order","domain":[["state","in",["sale","done"]]],"label":"Commandes confirmees"}},
  {{"id":"taux","type":"calc","formula":"$nb_confirme / $nb_total * 100","label":"Taux de conversion","unit":"%"}}
]}}

Exemple 3 — Croisement inter-modeles :
{{"type":"multi","title":"Clients actifs avec factures impayees","steps":[
  {{"id":"partners_actifs","type":"collect","model":"sale.order","domain":[["state","in",["sale","done"]]],"field":"partner_id","display":false}},
  {{"id":"impayees","type":"query","model":"account.move","domain":[["move_type","=","out_invoice"],["state","=","posted"],["payment_state","!=","paid"],["partner_id","in","$partners_actifs"]],"fields":["partner_id","name","amount_residual","invoice_date"],"field_labels":{{"partner_id":"Client","name":"Facture","amount_residual":"Montant Du","invoice_date":"Date"}},"label":"Factures impayees","orderby":"amount_residual desc","limit":20}}
]}}

### message — hors perimetre Odoo
{{"type":"message","message":"Je reponds uniquement aux questions sur vos donnees Odoo."}}

## Regles domaines

- Dates ISO : "2025-01-01"
- sale.order.state : "draft"=devis | "sent"=envoye | "sale"=confirme | "done"=termine | "cancel"=annule
- account.move : move_type="out_invoice" pour factures client | state="posted" pour validees
- OR : ["|", cond1, cond2] | AND implicite entre conditions

## Regles de choix du type

- "combien" / "nombre" → count
- "CA" / "chiffre d'affaires" / "total" / "montant" → sum (field=amount_total, unit="EUR")
- "liste" / "affiche" / "voir" / "quels" → query (limit 10)
- "par X" / "repartition" / "graphique" / "evolution" → chart (chart_type adapte : bar pour comparaison, line pour tendance, pie pour repartition)
- "tableau de bord" / "synthese" / "bilan" / "vue d'ensemble" → synthesis avec 4 KPIs + un chart pertinent
- "vs" / "par rapport" / "evolution" / "taux" / "ratio" / "comparaison" / "progression" → multi
- "clients qui ont X ET Y" / croisement entre 2 modeles → multi avec collect puis query

## Labels metier FR (toujours dans field_labels)

name → "N commande" ou "Produit" ou "Contact" selon le modele
partner_id → "Client" | amount_total → "CA HT" | state → "Statut"
date_order → "Date commande" | invoice_date → "Date facture"
product_id → "Produit" | quantity / product_uom_qty → "Qte"

## Modeles disponibles

{models}

## Contexte date
- Maintenant : {now}
- Debut semaine : {week_start}
- Debut mois courant : {this_month}
- Debut mois precedent : {last_month}
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
