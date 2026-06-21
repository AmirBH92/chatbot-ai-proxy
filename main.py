"""
Chatbot AI Proxy — CO LABS CONSEILS
Intermediaire entre le widget Odoo frontend et l'API Google Gemini.

Endpoints :
  POST /api/query  -- traduit une question NL en intent JSON Odoo
  GET  /widget.js  -- sert le widget avec l'URL du proxy integree
  GET  /health     -- healthcheck
"""
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

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1/models"

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

SYSTEM_PROMPT_TPL = """Tu es un assistant expert Odoo 19.3. Tu interpretes des questions en langage naturel et tu retournes un objet JSON decrivant comment interroger la base Odoo.

## Format de reponse (JSON strict)

{{
  "type": "query|count|sum|chart|message",
  "model": "nom.du.modele",
  "domain": [["champ", "operateur", "valeur"]],
  "fields": ["champ1", "champ2"],
  "groupby": ["champ"],
  "orderby": "champ desc",
  "limit": 40,
  "aggregation": {{"field": "amount_total", "function": "sum"}},
  "chart_type": "bar|line|pie",
  "chart_label_field": "champ",
  "message": "texte si pas de requete"
}}

## Regles domaines Odoo

- Dates en ISO 8601 ("2025-01-01")
- state "sale" = commande confirmee | "draft" = devis | "done" = valide
- move_type "out_invoice" = facture client | "in_invoice" = facture fournisseur
- OR : ["|", cond1, cond2] -- AND : [cond1, cond2]

## Types de reponse

- query = liste d'enregistrements (tableau)
- count = nombre d'enregistrements (chiffre)
- sum = somme d'un champ numerique (chiffre)
- chart = donnees agregees (graphique)
- message = reponse textuelle directe

## Modeles disponibles sur cette instance

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
                models = [m["name"] for m in r.json().get("models", [])]
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
            "maxOutputTokens": 1024,
            "temperature": 0.1,
            "responseMimeType": "application/json",  # Gemini force JSON valide
        },
    }

    endpoint = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            endpoint,
            params={"key": GEMINI_API_KEY},
            json=payload,
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"Gemini error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        intent = json.loads(raw)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return JSONResponse({"error": f"Reponse Gemini invalide : {e}", "raw": str(data)[:300]})

    return JSONResponse(intent)
