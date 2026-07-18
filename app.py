from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, UploadFile, File, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from concurrent.futures import ProcessPoolExecutor
import asyncio
import os
import time
import re
from typing import Optional

# ─── CONFIG ─────────────────────────────────────────────────────────────
MAX_WORKERS = max(1, os.cpu_count() or 2)
RATE_LIMIT = "200/minute"
SKIP_PREFIXES = frozenset([
    'country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:',
    'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:',
    'order:', 'cc|month|year|cvv'
])
YEAR_EXCLUSIONS = frozenset(str(y) for y in range(2020, 2040))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "209715200"))  # 200MB default

# ─── LUHN CHECK ─────────────────────────────────────────────────────────
def verify_luhn(card_number: str) -> bool:
    digits = [int(c) for c in card_number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse = digits[::-1]
    for i, d in enumerate(reverse):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

# ─── LINE PATTERNS ──────────────────────────────────────────────────────
LINE_PATTERNS = [
    {
        "regex": re.compile(r'^\d{13,19}\|\d{6}\|\d{3,4}$'),
        "type": "format_yyyymm",
        "extract": lambda m: (
            m.group(0).split('|')[0],
            m.group(0).split('|')[1][4:6],
            m.group(0).split('|')[1][2:4],
            m.group(0).split('|')[2]
        )
    },
    {
        "regex": re.compile(r'^\d{13,19}\|\d{2}/\d{2}\|\d{3,4}$'),
        "type": "format_mm_slash_yy",
        "extract": lambda m: (
            m.group(0).split('|')[0],
            m.group(0).split('|')[1].split('/')[0],
            m.group(0).split('|')[1].split('/')[1],
            m.group(0).split('|')[2]
        )
    },
    {
        "regex": re.compile(r'^\d{13,19}~\d{2}/\d{2}~\d{3,4}$'),
        "type": "format_tilde",
        "extract": lambda m: (
            m.group(0).split('~')[0],
            m.group(0).split('~')[1].split('/')[0],
            m.group(0).split('~')[1].split('/')[1],
            m.group(0).split('~')[2]
        )
    },
    {
        "regex": re.compile(r'^\d{13,19}\|\d{2}\|\d{2}\|\d{3,4}$'),
        "type": "format_pipe_mm_yy",
        "extract": lambda m: m.group(0).split('|')
    },
    {
        "regex": re.compile(r'^\d{13,19}\|\d{2}/\d{2}\|\d{3,4}\|'),
        "type": "format_slovakia",
        "extract": lambda m: (
            m.group(0).split('|')[0],
            m.group(0).split('|')[1].split('/')[0],
            m.group(0).split('|')[1].split('/')[1],
            m.group(0).split('|')[2]
        )
    },
]

# ─── WORKER FUNCTION ────────────────────────────────────────────────────
def process_text(text: str):
    start_time = time.time()
    all_results = []
    found_cards = set()

    lines = text.split('\n')
    for line in lines:
        trimmed = line.strip()
        if len(trimmed) < 20:
            continue
        if any(trimmed.lower().startswith(p) for p in SKIP_PREFIXES):
            continue

        matched = False
        for pat in LINE_PATTERNS:
            m = pat["regex"].match(trimmed)
            if m:
                card, mm, yy, cvv = pat["extract"](m)
                if card not in found_cards and verify_luhn(card):
                    found_cards.add(card)
                    all_results.append({"card": card, "mm": mm, "yy": yy, "cvv": cvv, "source": pat["type"]})
                matched = True
                break

        if not matched and 'card_number:' in trimmed:
            card_match = re.search(r'card_number:\s*(\d{13,19})', trimmed)
            if card_match:
                card = card_match.group(1)
                idx = text.find(trimmed)
                block_end = text.find('\n\n', idx)
                if block_end == -1:
                    block_end = len(text)
                block = text[idx:block_end]
                cvv_m = re.search(r'secure_code:\s*(\d{3,4})', block)
                exp_m = re.search(r'expiration:\s*(\d{2})/(\d{2})', block)
                if cvv_m and exp_m and card not in found_cards and verify_luhn(card):
                    found_cards.add(card)
                    all_results.append({"card": card, "mm": exp_m.group(1), "yy": exp_m.group(2),
                                        "cvv": cvv_m.group(1), "source": "structured"})

    PAN_REGEX = re.compile(r'\b\d{13,19}\b')
    DATE_STRICT = re.compile(r'\b(0[1-9]|1[0-2])[\s\-/|]?(20\d{2}|\d{2})\b')
    DATE_YYYYMM = re.compile(r'\b(20\d{2})(0[1-9]|1[0-2])\b')
    CVV_REGEX = re.compile(r'\b\d{3,4}\b')

    for m in PAN_REGEX.finditer(text):
        card = m.group(0)
        if card in found_cards or not verify_luhn(card):
            continue
        p_start = m.start()
        space_start = max(0, p_start - 300)
        space_end = min(len(text), p_start + len(card) + 400)
        context = text[space_start:space_end]
        ctx_offset = space_start

        best_date = None
        min_date_dist = float('inf')
        for dm in DATE_STRICT.finditer(context):
            dist = abs((ctx_offset + dm.start()) - p_start)
            if dist < min_date_dist:
                min_date_dist = dist
                best_date = (dm.group(1), dm.group(2)[-2:])
        for dm in DATE_YYYYMM.finditer(context):
            dist = abs((ctx_offset + dm.start()) - p_start)
            if dist < min_date_dist:
                min_date_dist = dist
                best_date = (dm.group(2), dm.group(1)[2:])

        if not best_date:
            continue

        best_cvv = None
        min_cvv_dist = float('inf')
        for cm in CVV_REGEX.finditer(context):
            cvv_cand = cm.group(0)
            actual_start = ctx_offset + cm.start()
            if cvv_cand in card or cvv_cand in ''.join(best_date):
                continue
            if cvv_cand in YEAR_EXCLUSIONS:
                continue
            left_ok = actual_start == 0 or not text[actual_start - 1].isdigit()
            right_ok = actual_start + len(cvv_cand) >= len(text) or not text[actual_start + len(cvv_cand)].isdigit()
            if not (left_ok and right_ok):
                continue
            dist = abs(actual_start - p_start)
            if dist < min_cvv_dist:
                min_cvv_dist = dist
                best_cvv = cvv_cand

        if best_cvv:
            found_cards.add(card)
            all_results.append({"card": card, "mm": best_date[0], "yy": best_date[1],
                                "cvv": best_cvv, "source": "heuristic"})

    now = time.localtime()
    current_year_short = now.tm_year % 100
    current_month = now.tm_mon
    valid = [r for r in all_results
             if int(r["yy"]) > current_year_short or
                (int(r["yy"]) == current_year_short and int(r["mm"]) >= current_month)]
    formatted = sorted([f'{r["card"]}|{r["mm"]}|{r["yy"]}|{r["cvv"]}' for r in valid])

    return {
        "success": True,
        "count": len(formatted),
        "processing_time_ms": int((time.time() - start_time) * 1000),
        "data": formatted
    }

# ─── LIFESPAN (thay thế on_event) ──────────────────────────────────────
executor = None
START_TIME = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)
    yield
    executor.shutdown(wait=True)

# ─── FASTAPI APP ───────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="CCClean API", version="1.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok", "uptime": time.time() - START_TIME}

@app.post("/v1/ccclean")
@limiter.limit(RATE_LIMIT)
async def ccclean(
    request: Request,
    file: Optional[UploadFile] = File(None)
):
    text = ""
    source = "body"

    if file is not None:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max {MAX_FILE_SIZE // (1024*1024)}MB"
            )
        text = content.decode("utf-8", errors="ignore")
        source = f"file:{file.filename}"
    else:
        body = await request.body()
        text = body.decode("utf-8", errors="ignore")

    if not text or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Missing or invalid text/file input")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, process_text, text)
    result["source"] = source
    return JSONResponse(content=result)
