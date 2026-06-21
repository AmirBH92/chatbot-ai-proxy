# Chatbot AI Proxy

FastAPI proxy pour le widget Chatbot AI Odoo.  
Traduit les questions en langage naturel en requetes Odoo via Google Gemini.

## Variables d'environnement (Render)

| Variable | Description |
|---|---|
| GEMINI_API_KEY | Cle API Google AI Studio (aistudio.google.com) |
| ALLOWED_ORIGINS | URL Odoo autorisee (ex: https://chatbot.odoo.com) |
| GEMINI_MODEL | Modele Gemini (defaut: gemini-2.0-flash) |

## Endpoints

- POST /api/query -- Traduit NL -> intent JSON Odoo
- GET  /widget.js -- Sert le widget JS avec URL proxy integree
- GET  /health    -- Healthcheck
