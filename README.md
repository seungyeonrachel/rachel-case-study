# PartSelect AI Assistant – Case Study

This project implements a focused, production-style chat agent for the PartSelect e-commerce website.  
The assistant is scoped strictly to **Refrigerator** and **Dishwasher** parts and supports:

- Part lookup by PS number
- Installation guidance
- Model compatibility checks
- Model-specific parts lists
- Model Q&A
- Symptom-based troubleshooting with guided flows

The emphasis is on **user experience**, **agentic control**, and **robust scraping under real-world constraints**.

---

## Architecture Overview

### Frontend
- **Next.js** chat interface
- Conversational UI with step-by-step flows (e.g., symptom selection)
- Product links surfaced directly in chat

### Backend
- **FastAPI** service with a stateful chat endpoint
- Deterministic intent detection + session memory
- Optional LLM usage (only for summarizing installation steps)
- Robust scraper with:
  - Requests + Selenium fallback
  - TTL caching
  - Bot / block detection
  - Breadcrumb-based appliance scoping

---

## Supported User Flows

| Feature | Example |
|------|-------|
| Part info | “What is PS11752778?” |
| Installation | “How do I install PS11752778?” |
| Compatibility | “Is PS11752778 compatible with WDT780SAEM1?” |
| Troubleshooting | “My Whirlpool fridge ice maker isn’t working” |

---

## Scope Control

The assistant **only** answers questions related to:
- Refrigerator parts
- Dishwasher parts

If a page or request falls outside this scope, the agent declines gracefully and explains the limitation.

---

## Running the Project

### Backend
```bash
pip install -r requirements.txt
uvicorn main:app --reload
