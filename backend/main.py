from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from scraper import (
    get_part_bundle,
    get_model_symptoms,
    get_symptom_fix_parts,
    check_compatibility,
    bundle_to_dict,
    list_parts_for_model,
    list_qna_for_model,
)

# -----------------------------
# Optional OpenAI (LLM summarization)
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        _client = None

# -----------------------------
# App
# -----------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Simple session store
# -----------------------------
_SESSIONS: Dict[str, Dict[str, Any]] = {}

def _session_get(session_id: str) -> Dict[str, Any]:
    if session_id not in _SESSIONS:
        _SESSIONS[session_id] = {}
    return _SESSIONS[session_id]

# -----------------------------
# Request / response models
# -----------------------------
class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    role: str = "assistant"
    content: str

# -----------------------------
# Parsing / intent
# -----------------------------
PS_RE = re.compile(r"\bPS\d{5,}\b", re.I)

SYMPTOM_RE = re.compile(
    r"\b("
    r"symptom|issue|problem|troubleshoot|diagnos|fix|repair|help|"
    r"not working|isn\'t working|won\'t|will not|doesn\'t|does not|"
    r"leak|leaking|drip|water|noisy|loud|vibrat|shak|"
    r"burn|smell|odor|sparks?|smoke|"
    r"not draining|drain|clog|not cleaning|clean|"
    r"not cooling|warm|hot|freez|frost|"
    r"dispens|ice|maker|"
    r"error|code|e\d+|f\d+"
    r")\b",
    re.I,
)

COMPAT_RE = re.compile(r"\b(compat|compatible|fit|fits|work with|works with)\b", re.I)
INSTALL_RE = re.compile(r"\b(install|installation|replace|replacement|remove|swap)\b", re.I)
WHATIS_RE = re.compile(r"\b(what is|tell me about|part info|details)\b", re.I)

MODEL_PARTS_RE = re.compile(r"\b(model\s+parts|parts\s+for\s+model|parts\s+list)\b", re.I)
MODEL_QNA_RE = re.compile(r"\b(questions\s+and\s+answers|q\s*&\s*a|qna)\b", re.I)

MODEL_STOPWORDS = {
    "NUMBER","NUMBERS","MODEL","SERIAL","REFRIGERATOR","FRIDGE","DISHWASHER","WHIRLPOOL",
    "MAYTAG","KITCHENAID","GE","SAMSUNG","LG",
}

def extract_ps(text: str) -> Optional[str]:
    m = PS_RE.search(text or "")
    return m.group(0).upper() if m else None

def extract_model(text: str) -> Optional[str]:
    t = (text or "").upper().replace(":", " ")
    tokens = [re.sub(r"[^A-Z0-9]", "", x) for x in t.split()]
    tokens = [x for x in tokens if x]

    for i, tok in enumerate(tokens[:-1]):
        if tok == "MODEL":
            cand = tokens[i + 1]
            if cand not in MODEL_STOPWORDS and not cand.startswith("PS"):
                if re.search(r"[A-Z]", cand) and re.search(r"\d", cand) and len(cand) >= 8:
                    return cand

    for cand in tokens:
        if cand.startswith("PS") or cand in MODEL_STOPWORDS:
            continue
        if re.search(r"[A-Z]", cand) and re.search(r"\d", cand) and len(cand) >= 8:
            return cand
    return None

def refers_to_previous_model(text: str) -> bool:
    t = (text or "").lower()
    return any(
        p in t
        for p in [
            "this model",
            "that model",
            "same model",
            "previous model",
            "above model",
            "my model",
            "the model",
        ]
    )


def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if MODEL_PARTS_RE.search(t):
        return "model_parts"
    if MODEL_QNA_RE.search(t):
        return "model_qna"
    if INSTALL_RE.search(t):
        return "install"
    if COMPAT_RE.search(t):
        return "compat"
    if WHATIS_RE.search(t):
        return "what_is"
    if SYMPTOM_RE.search(t):
        return "symptom"
    if extract_ps(t):
        return "what_is"
    return "unknown"

def in_scope(text: str) -> bool:
    t = (text or "").lower()
    return any(
        k in t
        for k in [
            "ps","part","install","installation","replace",
            "refrigerator","fridge","dishwasher",
            "fit","compatible","compatibility",
            "symptom","not working","leak","noisy",
            "questions and answers","q&a","parts list","model parts",
        ]
    )

def _ensure_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []

ALLOWED_APPLIANCE_TYPES = {"refrigerator", "dishwasher"}

def _scope_error(appliance_type: Optional[str], url: str = "") -> ChatResponse:
    at = (appliance_type or "").strip() or "unknown"
    msg = (
        "I can only help with **refrigerator** and **dishwasher** parts right now.\n"
        f"This page looks like: **{at}**."
    )
    if url:
        msg += f"\n\nPage: `{url}`"
    return ChatResponse(content=msg)

def _enforce_appliance_type(appliance_type: Optional[str], url: str = "") -> Optional[ChatResponse]:
    if appliance_type and appliance_type.lower() in ALLOWED_APPLIANCE_TYPES:
        return None
    # If we couldn't infer it (None), treat it as out-of-scope to be strict.
    return _scope_error(appliance_type, url=url)


# -----------------------------
# Installation summarization (same as your current)
# -----------------------------
WORD_RE = re.compile(r"[a-z0-9]+")

def _join_top_reviews(b: Dict[str, Any], max_reviews: int = 6, max_chars: int = 1800) -> str:
    reviews = _ensure_list(b.get("customer_reviews"))
    chunks: List[str] = []
    for r in reviews[:max_reviews]:
        if isinstance(r, dict):
            body = (r.get("body") or "").strip()
            if body:
                chunks.append(body)
    text = "\n\n".join(chunks).strip()
    return text[:max_chars]

def _join_description(b: Dict[str, Any], max_chars: int = 1800) -> str:
    desc = (b.get("product_description") or "").strip()
    if not desc:
        desc = (b.get("description") or "").strip()
    return desc[:max_chars]

def _heuristic_install_steps(b: Dict[str, Any], max_steps: int = 6) -> List[str]:
    action_pat = re.compile(r"\b(align|line up|snap|click|slide|push|press|pull|lift|remove|insert|install|replace)\b", re.I)
    texts = []
    d = _join_description(b, max_chars=2400)
    if d:
        texts.append(d)
    rv = _join_top_reviews(b, max_reviews=8, max_chars=3000)
    if rv:
        texts.append(rv)

    sent: List[str] = []
    for t in texts:
        parts = re.split(r"(?<=[.!?])\s+|\n+", t)
        for s in parts:
            s = re.sub(r"\s+", " ", s).strip()
            if 12 <= len(s) <= 220 and action_pat.search(s):
                sent.append(s)

    out: List[str] = []
    seen = set()
    for s in sent:
        k = re.sub(r"\W+", "", s.lower())[:120]
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= max_steps:
            break
    return out

def build_installation_answer(ps: str, b: Dict[str, Any]) -> str:
    part_name = (b.get("name") or ps).strip()
    desc = _join_description(b)
    reviews = _join_top_reviews(b)

    if not desc and not reviews:
        return "I couldn’t find installation details in the product description or customer reviews for this part on PartSelect."

    if _client is not None:
        system = (
            "You are a careful assistant generating installation instructions for appliance parts.\n"
            "Rules:\n"
            "- Use ONLY the provided Product Description and Customer Reviews.\n"
            "- Do NOT add steps that are not supported by the text.\n"
            "- If the text does not clearly describe steps, say that explicitly.\n"
            "- Output concise step-by-step instructions (numbered), plus a short Notes section if relevant.\n"
        )
        user = (
            f"Part: {ps} ({part_name})\n\n"
            f"Product Description:\n{desc or '[none found]'}\n\n"
            f"Customer Reviews:\n{reviews or '[none found]'}\n\n"
            "Task: Create installation steps grounded ONLY in the text above."
        )
        try:
            resp = _client.responses.create(
                model=OPENAI_MODEL,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = (resp.output_text or "").strip()
            if text:
                return text
        except Exception:
            pass

    steps = _heuristic_install_steps(b)
    if not steps:
        return "I couldn’t find clear step-by-step installation directions in the product description or customer reviews for this part on PartSelect."
    lines = ["Installation (from product description + customer reviews):"]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. {s}")
    return "\n".join(lines)

def extract_keyword_after_colon(text: str) -> Optional[str]:
    # e.g. "model parts: shelf" -> "shelf"
    if not text:
        return None
    m = re.search(r":\s*([A-Za-z0-9][A-Za-z0-9 \-_/]{1,40})\s*$", text)
    return m.group(1).strip() if m else None

# -----------------------------
# Symptom formatting
# -----------------------------
def _format_symptom_menu(model: str, symptoms: List[Dict[str, str]], page: int, page_size: int) -> str:
    start = page * page_size
    end = min(len(symptoms), start + page_size)
    chunk = symptoms[start:end]

    lines = [f"Common symptom guides for model {model}:"]
    for i, s in enumerate(chunk, 1):
        lines.append(f"{i}. {s.get('name')}")

    if end < len(symptoms):
        lines.append(f"\nReply with a number (1-{len(chunk)}), or type `more` to see more symptoms.")
    else:
        lines.append(f"\nReply with a number (1-{len(chunk)}).")

    return "\n".join(lines)

def _format_parts(model: str, symptom_name: str, symptom_url: str, parts: List[Dict[str, Any]]) -> str:
    lines = [f"Parts that fix “{symptom_name}” for model {model}:"]
    for p in parts:
        name = (p.get("name") or "").strip()
        psn = (p.get("ps_number") or "").strip()
        fix = (p.get("fix_rate") or "").strip()
        desc = (p.get("description") or "").strip()

        head = "- "
        if name and psn:
            head += f"{name} ({psn})"
        elif name:
            head += name
        elif psn:
            head += psn
        else:
            continue

        if fix:
            head += f" — {fix}"
        lines.append(head)

        if desc:
            lines.append(f"  {desc}")

    if symptom_url:
        lines.append(f"\nSymptom guide: `{symptom_url}`")
    return "\n".join(lines)

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    user_text = (req.message or "").strip()

    session_id = request.headers.get("x-session-id") or (request.client.host if request.client else "anon")
    session = _session_get(session_id)

    if not user_text:
        return ChatResponse(
            content="Ask me about a PartSelect refrigerator/dishwasher part (PS…), installation steps, compatibility (model + PS), model parts, model Q&A, or troubleshooting (model + symptom)."
        )

    intent = detect_intent(user_text)
    ps = extract_ps(user_text)
    model = extract_model(user_text)

    # allow follow-up "this part"
    if not ps and intent in ("install", "compat", "what_is"):
        ps = session.get("last_ps")
    if ps:
        session["last_ps"] = ps
    if model:
        session["last_model"] = model

    # handle pending flows
    pending = session.get("pending") or {}

    # pending: need model for symptom flow
    if pending.get("type") == "need_model_for_symptom" and model:
        intent = "symptom"
        user_text = pending.get("symptom_text") or user_text
        session["pending"] = {}

    # pending: symptom menu paging
    if pending.get("type") == "symptom_menu":
        if user_text.lower() == "more":
            pending["page"] = int(pending.get("page", 0)) + 1
            session["pending"] = pending
            return ChatResponse(content=_format_symptom_menu(pending["model"], pending["symptoms"], pending["page"], pending["page_size"]))

        if user_text.isdigit():
            idx = int(user_text) - 1
            page = int(pending.get("page", 0))
            page_size = int(pending.get("page_size", 8))
            start = page * page_size
            chosen_idx = start + idx

            symptoms = pending.get("symptoms") or []
            if 0 <= chosen_idx < len(symptoms):
                chosen = symptoms[chosen_idx]
                session["pending"] = {}

                symptom_name = chosen.get("name") or "that symptom"
                symptom_url = chosen.get("url") or ""

                fixes_payload, symptom_source = get_symptom_fix_parts(pending["model"], symptom_url)
                symptom_page_url = getattr(symptom_source, "url", "") or symptom_url or ""

                if fixes_payload.get("blocked"):
                    return ChatResponse(content=f"⚠️ I couldn’t load that symptom guide right now. Symptom guide: `{symptom_page_url}`")

                parts = fixes_payload.get("parts") or []
                if not parts:
                    return ChatResponse(content=f"I found the symptom guide for “{symptom_name}”, but couldn’t extract the parts list. Symptom guide: `{symptom_page_url}`")

                return ChatResponse(content=_format_parts(pending["model"], symptom_name, symptom_page_url, parts))

        # invalid response → re-show
        return ChatResponse(content=_format_symptom_menu(pending["model"], pending["symptoms"], pending["page"], pending["page_size"]))

    # scope guard
    if not in_scope(user_text) and not ps and not model:
        return ChatResponse(content="I can help with PartSelect refrigerator/dishwasher parts: installation, compatibility (model + PS), part lookup (PS…), model parts, model Q&A, and troubleshooting (model + symptom).")

    # -----------------------------
    # MODEL PARTS
    # -----------------------------
    if intent == "model_parts":
        if not model:
            model = session.get("last_model")
        if not model:
            return ChatResponse(content="What’s your appliance model number? (Example: WRS325FDAM02)")

        kw = extract_keyword_after_colon(user_text)
        raw = list_parts_for_model(model, keyword=kw)
        payload = raw[0] if isinstance(raw, tuple) else raw

        appliance_type = payload.get("appliance_type") if isinstance(payload, dict) else None
        scope_resp = _enforce_appliance_type(appliance_type, url=(payload.get("model_url") or ""))
        if scope_resp:
            return scope_resp

        if isinstance(payload, dict) and payload.get("blocked"):
            return ChatResponse(content=f"⚠️ I couldn’t load the parts list for {model} right now (blocked).")

        parts = _ensure_list(payload.get("parts") if isinstance(payload, dict) else [])
        if not parts:
            return ChatResponse(content=f"I couldn’t extract the parts list for model {model} on PartSelect.")

        shown = parts[:12]
        lines = [f"Parts for model {model}" + (f' (filtered by "{kw}"):' if kw else ":")]
        for p in shown:
            psn = (p.get("ps") or p.get("ps_number") or "").strip()
            title = (p.get("title") or p.get("name") or "").strip()
            if psn and title:
                lines.append(f"- {psn} — {title}")
            elif psn:
                lines.append(f"- {psn}")
        if len(parts) > len(shown):
            lines.append(f"...and {len(parts) - len(shown)} more on the model page.")
        lines.append(f"\nModel page: `https://www.partselect.com/Models/{model}/#Parts`")
        return ChatResponse(content="\n".join(lines))

    # -----------------------------
    # MODEL Q&A
    # -----------------------------
    if intent == "model_qna":
        if not model:
            model = session.get("last_model")
        if not model:
            return ChatResponse(content="What’s your appliance model number? (Example: WRS325FDAM02)")

        kw = extract_keyword_after_colon(user_text)
        raw = list_qna_for_model(model, keyword=kw, limit=8)
        payload = raw[0] if isinstance(raw, tuple) else raw

        if isinstance(payload, dict) and payload.get("blocked"):
            return ChatResponse(content=f"⚠️ I couldn’t load the model Q&A for {model} right now (blocked).")

        qna = _ensure_list(payload.get("qna") if isinstance(payload, dict) else [])
        if not qna:
            return ChatResponse(content=f"I couldn’t extract the model Q&A section for {model} on PartSelect.")

        lines = [f"Questions & Answers for model {model}:"]
        for i, qa in enumerate(qna, 1):
            q = (qa.get("question") or "").strip()
            a = (qa.get("answer") or "").strip()
            if not q:
                continue
            if a:
                lines.append(f"{i}. Q: {q}\n   A: {a}")
            else:
                lines.append(f"{i}. Q: {q}")
        lines.append(f"\nModel Q&A: `https://www.partselect.com/Models/{model}/#QuestionsAndAnswers`")
        return ChatResponse(content="\n".join(lines))

    # -----------------------------
    # INSTALL
    # -----------------------------
    if intent == "install":
        if not ps:
            return ChatResponse(content="What’s the PS part number?")

        bundle, _ = get_part_bundle(ps)
        b = bundle_to_dict(bundle)

        scope_resp = _enforce_appliance_type(b.get("appliance_type"), url=(b.get("url") or ""))
        if scope_resp:
            return scope_resp

        if b.get("blocked"):
            return ChatResponse(content="⚠️ I couldn’t load that part page right now (blocked). Try again in a minute.")
        if b.get("not_found"):
            return ChatResponse(content=f"I couldn’t find that part number ({ps}) on PartSelect.")

        answer = build_installation_answer(ps, b)
        part_url = (b.get("url") or "").strip()
        if part_url:
            answer += f"\n\nIf you want to view this part on PartSelect: `{part_url}`"
        return ChatResponse(content=answer)

    # -----------------------------
    # COMPAT
    # -----------------------------
    if intent == "compat":
        if not ps:
            return ChatResponse(content="Which PS part number are you asking about?")
        if not model:
            model = session.get("last_model")
        if not model:
            return ChatResponse(content=f"What’s your appliance model number for compatibility with {ps}?")

        comp, model_source = check_compatibility(model, ps)
        model_url = getattr(model_source, "url", "") or ""

        scope_resp = _enforce_appliance_type(comp.get("appliance_type"), url=model_url)
        if scope_resp:
            return scope_resp

        verdict = (
            f"Yes — {ps} appears under **Parts** for model {model} on PartSelect."
            if comp.get("compatible")
            else f"No — {ps} is **not** listed under **Parts** for model {model} on PartSelect."
        )

        part_url = ""
        try:
            bundle, _ = get_part_bundle(ps)
            b = bundle_to_dict(bundle)
            part_url = (b.get("url") or "").strip()
        except Exception:
            pass

        links = []
        if model_url:
            links.append(f"Model page: `{model_url}`")
        if part_url:
            links.append(f"Part page: `{part_url}`")
        return ChatResponse(content=verdict + (("\n\n" + "\n".join(links)) if links else ""))

    # -----------------------------
    # SYMPTOM (GENERAL)
    # -----------------------------
    if intent == "symptom":
        if not model and refers_to_previous_model(user_text):
            model = session.get("last_model")

        if not model:
            session["pending"] = {"type": "need_model_for_symptom", "symptom_text": user_text}
            return ChatResponse(content="To troubleshoot, I’ll need your appliance model number. What model number do you have?")



        symptoms_payload, model_source = get_model_symptoms(model)
        scope_resp = _enforce_appliance_type(symptoms_payload.get("appliance_type"), url=getattr(model_source, "url", ""))
        if scope_resp:
            return scope_resp

        if symptoms_payload.get("blocked"):
            return ChatResponse(content=f"⚠️ I couldn’t load the symptoms list for {model} right now. Model page: `{getattr(model_source, 'url', '')}`")

        symptoms = symptoms_payload.get("symptoms") or []
        if not symptoms:
            return ChatResponse(content=f"I couldn’t find symptom guides for {model} on PartSelect. Model page: `{getattr(model_source, 'url', '')}`")

        # store menu state and show first page
        page_size = 8
        session["pending"] = {
            "type": "symptom_menu",
            "model": model,
            "symptoms": [{"name": s.get("name"), "url": s.get("url")} for s in symptoms if s.get("name") and s.get("url")],
            "page": 0,
            "page_size": page_size,
        }
        return ChatResponse(content=_format_symptom_menu(model, session["pending"]["symptoms"], 0, page_size))

    # -----------------------------
    # WHAT IS
    # -----------------------------
    if intent == "what_is":
        if not ps:
            return ChatResponse(content="What’s the PS part number?")

        bundle, _ = get_part_bundle(ps)
        b = bundle_to_dict(bundle)

        scope_resp = _enforce_appliance_type(b.get("appliance_type"), url=(b.get("url") or ""))
        if scope_resp:
            return scope_resp

        if b.get("blocked"):
            return ChatResponse(content="⚠️ I couldn’t load that part page right now (blocked). Try again in a minute.")
        if b.get("not_found"):
            return ChatResponse(content=f"I couldn’t find that part number ({ps}) on PartSelect.")

        name = b.get("name") or ps
        mpn = b.get("manufacturer_part_number") or "Not listed"
        part_url = b.get("url") or ""
        content = f"{ps} is {name}.\nManufacturer Part #: {mpn}"
        if part_url:
            content += f"\n\nIf you want to view this part on PartSelect: `{part_url}`"
        return ChatResponse(content=content)

    if intent == "unknown":
        if re.search(r"\b(not|won\'t|doesn\'t|noisy|loud|vibrat|leak|smell|error|code|help|fix)\b", user_text.lower()):
            session["pending"] = {"type": "need_model_for_symptom", "symptom_text": user_text}
            return ChatResponse(content="I can help troubleshoot, but I’ll need your appliance model number. What model number do you have?")

    return ChatResponse(content="Ask about a PartSelect part number (PS…), installation, compatibility (model + PS), model parts, model Q&A, or troubleshooting (model + symptom).")
