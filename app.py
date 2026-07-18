import os
import re
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

START_TIME = time.time()

PAN_PATTERN = re.compile(r'(?<!\d)\d{13,19}(?!\d)')
CVV_PATTERN = re.compile(r'(?<!\d)\d{3,4}(?!\d)')

DATE_PATTERN_1 = re.compile(r'(?<!\d)(0[1-9]|1[0-2])[|\s~/:,-]+(20\d{2}|\d{2})(?!\d)')
DATE_PATTERN_2 = re.compile(r'(?<!\d)(20\d{2})[|\s~/:,-]+(0[1-9]|1[0-2])(?!\d)')
DATE_STICKY = re.compile(r'(?<!\d)(0[1-9]|1[0-2])(20\d{2}|\d{2})(?!\d)')

STANDARD_EXTRACTOR = re.compile(
    r'(?<!\d)(\d{13,19})[^\d]{1,5}(0[1-9]|1[0-2])[^\d]{1,5}(20\d{2}|\d{2})[^\d]{1,5}(\d{3,4})(?!\d)'
)
STANDARD_EXTRACTOR_ALT = re.compile(
    r'(?<!\d)(\d{13,19})[^\d]{1,5}(\d{3,4})[^\d]{1,5}(0[1-9]|1[0-2])[^\d]{1,5}(20\d{2}|\d{2})(?!\d)'
)

def verify_luhn(card_number: str) -> bool:
    digits = [int(c) for c in card_number]
    length = len(digits)
    if length < 13 or length > 19:
        return False
    total = 0
    alternate = False
    for i in range(length - 1, -1, -1):
        n = digits[i]
        if alternate:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alternate = not alternate
    return total % 10 == 0

def process_text_cleaning(text: str) -> dict:
    start_time = time.time()
    all_results = []
    found_cards = set()

    for match in STANDARD_EXTRACTOR.finditer(text):
        card, mm, yy, cvv = match.groups()
        if card not in found_cards and verify_luhn(card):
            found_cards.add(card)
            all_results.append({'card': card, 'mm': mm, 'yy': yy[-2:], 'cvv': cvv})

    for match in STANDARD_EXTRACTOR_ALT.finditer(text):
        card, cvv, mm, yy = match.groups()
        if card not in found_cards and verify_luhn(card):
            found_cards.add(card)
            all_results.append({'card': card, 'mm': mm, 'yy': yy[-2:], 'cvv': cvv})

    for match in PAN_PATTERN.finditer(text):
        card = match.group(0)
        if card in found_cards or not verify_luhn(card):
            continue

        idx = match.start()
        ctx_start = max(0, idx - 250)
        ctx_end = min(len(text), idx + len(card) + 250)
        context = text[ctx_start:ctx_end]

        best_mm, best_yy = '', ''
        min_date_dist = float('inf')
        
        for m_date in DATE_PATTERN_1.finditer(context):
            m_start = ctx_start + m_date.start()
            dist = abs(m_start - idx)
            if dist < min_date_dist:
                min_date_dist = dist
                best_mm, best_yy = m_date.group(1), m_date.group(2)[-2:]

        for m_date in DATE_PATTERN_2.finditer(context):
            m_start = ctx_start + m_date.start()
            dist = abs(m_start - idx)
            if dist < min_date_dist:
                min_date_dist = dist
                best_mm, best_yy = m_date.group(2), m_date.group(1)[-2:]

        for m_date in DATE_STICKY.finditer(context):
            m_start = ctx_start + m_date.start()
            dist = abs(m_start - idx)
            if dist < min_date_dist:
                min_date_dist = dist
                best_mm, best_yy = m_date.group(1), m_date.group(2)[-2:]

        if not best_mm:
            continue

        best_cvv = ''
        min_cvv_dist = float('inf')
        for m_cvv in CVV_PATTERN.finditer(context):
            cvv_cand = m_cvv.group(0)
            if cvv_cand in card or cvv_cand == best_mm or cvv_cand == best_yy:
                continue
            m_start = ctx_start + m_cvv.start()
            dist = abs(m_start - idx)
            if dist < min_cvv_dist:
                min_cvv_dist = dist
                best_cvv = cvv_cand

        if best_cvv:
            found_cards.add(card)
            all_results.append({'card': card, 'mm': best_mm, 'yy': best_yy, 'cvv': best_cvv})

    now = datetime.now()
    curr_y = now.year % 100
    curr_m = now.month

    valid = []
    for r in all_results:
        try:
            mm, yy = int(r['mm']), int(r['yy'])
            if yy > curr_y or (yy == curr_y and mm >= curr_m):
                valid.append(f"{r['card']}|{r['mm']}|{r['yy']}|{r['cvv']}")
        except ValueError:
            continue

    valid = list(set(valid))
    valid.sort()
    
    return {
        "success": True,
        "count": len(valid),
        "processing_time_ms": int((time.time() - start_time) * 1000),
        "data": valid
    }

@app.get('/health')
def health_check():
    return {"status": "ok", "uptime": int(time.time() - START_TIME)}

@app.post('/v1/ccclean')
@limiter.limit("200/minute")
async def ccclean(
    request: Request,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None)
):
    raw_text = ""
    if file:
        chunks = []
        while chunk := await file.read(1024 * 1024):
            chunks.append(chunk.decode("utf-8", errors="ignore"))
        raw_text = "".join(chunks)
    elif text:
        raw_text = text
    else:
        content_type = request.headers.get("content-type", "")
        body_bytes = b""
        async for chunk in request.stream():
            body_bytes += chunk
            if len(body_bytes) > 52428800:
                raise HTTPException(status_code=413, detail="Payload Too Large")
        raw_text = body_bytes.decode("utf-8", errors="ignore")
        if "application/json" in content_type:
            import json
            try:
                data = json.loads(raw_text)
                if isinstance(data, dict):
                    raw_text = data.get("text", raw_text)
            except Exception:
                pass

    if not raw_text:
        raise HTTPException(status_code=400, detail="Missing text")

    return process_text_cleaning(raw_text)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
