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

PAN_REGEX = re.compile(r'\b\d{13,19}\b')
CVV_REGEX = re.compile(r'\b\d{3,4}\b')
DATE_STRICT_REGEX = re.compile(r'\b(0[1-9]|1[0-2])[\s\-\/|]?(20\d{2}|\d{2})\b')
DATE_YYYYMM_REGEX = re.compile(r'\b(20\d{2})(0[1-9]|1[0-2])\b')
YEAR_EXCLUSIONS = {str(2020 + i) for i in range(20)}

LINE_PATTERNS = [
    (re.compile(r'^\d{13,19}\|\d{6}\|\d{3,4}'), 'format_yyyymm', 
     lambda m: (m.split('|')[0], m.split('|')[1][4:6], m.split('|')[1][2:4], m.split('|')[2])),
    (re.compile(r'^\d{13,19}\|\d{2}\/\d{2,4}\|\d{3,4}'), 'format_mm_slash_yy', 
     lambda m: (m.split('|')[0], m.split('|')[1].split('/')[0], m.split('|')[1].split('/')[1], m.split('|')[2])),
    (re.compile(r'^\d{13,19}~\d{2}\/\d{2,4}~\d{3,4}'), 'format_tilde', 
     lambda m: (m.split('~')[0], m.split('~')[1].split('/')[0], m.split('~')[1].split('/')[1], m.split('~')[2])),
    (re.compile(r'^\d{13,19}\|\d{2}\|\d{2,4}\|\d{3,4}'), 'format_pipe_mm_yy', 
     lambda m: (m.split('|')[0], m.split('|')[1], m.split('|')[2], m.split('|')[3])),
    (re.compile(r'^\d{13,19}\|\d{2}\/\d{2,4}\|\d{3,4}\|'), 'format_slovakia', 
     lambda m: (m.split('|')[0], m.split('|')[1].split('/')[0], m.split('|')[1].split('/')[1], m.split('|')[2])),
]

SKIP_PREFIXES = (
    'country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:', 'type:', 
    'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:', 'order:', 'cc|month|year|cvv'
)

def verify_luhn(card_number: str) -> bool:
    digits = [int(c) for c in card_number if '0' <= c <= '9']
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
        alternate = !alternate
    return total % 10 == 0

def process_text_cleaning(text: str) -> dict:
    start_time = time.time()
    all_results = []
    found_cards = set()
    
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        trimmed = line.strip()
        if len(trimmed) < 20 or trimmed.lower().startswith(SKIP_PREFIXES):
            continue
            
        matched_line = False
        for pattern, p_type, extract_fn in LINE_PATTERNS:
            m = pattern.match(trimmed)
            if m:
                try:
                    card, mm, yy, cvv = extract_fn(m.group(0))
                    if len(yy) == 4:
                        yy = yy[-2:]
                    if card not in found_cards and verify_luhn(card):
                        found_cards.add(card)
                        all_results.append({'card': card, 'mm': mm, 'yy': yy, 'cvv': cvv})
                    matched_line = True
                    break
                except Exception:
                    pass
                    
        if matched_line:
            continue
            
        if 'card_number:' in trimmed:
            card_match = re.search(r'card_number:\s*(\d{13,19})', trimmed)
            if card_match:
                card = card_match.group(1)
                block_context = "\n".join(lines[max(0, idx-3):min(len(lines), idx+6)])
                cvv_match = re.search(r'secure_code:\s*(\d{3,4})', block_context)
                exp_match = re.search(r'expiration:\s*(\d{2})\/(\d{2,4})', block_context)
                
                if cvv_match and exp_match and card not in found_cards and verify_luhn(card):
                    yy = exp_match.group(2)
                    if len(yy) == 4:
                        yy = yy[-2:]
                    found_cards.add(card)
                    all_results.append({'card': card, 'mm': exp_match.group(1), 'yy': yy, 'cvv': cvv_match.group(1)})
                    continue

        card_matches = PAN_REGEX.findall(trimmed)
        if card_matches:
            for card in card_matches:
                if card in found_cards or not verify_luhn(card):
                    continue
                
                best_date = ''
                min_date_score = float('inf')
                p_start = trimmed.find(card)
                
                min_bound = max(0, p_start - 150)
                max_bound = min(len(trimmed), p_start + len(card) + 150)
                
                for dm in DATE_STRICT_REGEX.finditer(trimmed):
                    if not (min_bound <= dm.start() <= max_bound):
                        continue
                    dist = abs(dm.start() - p_start)
                    if dist < min_date_score:
                        min_date_score = dist
                        best_date = f"{dm.group(1)}|{dm.group(2)[-2:]}"
                        
                for dm in DATE_YYYYMM_REGEX.finditer(trimmed):
                    if not (min_bound <= dm.start() <= max_bound):
                        continue
                    dist = abs(dm.start() - p_start)
                    if dist < min_date_score:
                        min_date_score = dist
                        best_date = f"{dm.group(2)}|{dm.group(1)[2:]}"
                        
                if not best_date:
                    continue
                    
                best_cvv = ''
                min_cvv_score = float('inf')
                
                for cm in CVV_REGEX.finditer(trimmed):
                    if not (min_bound <= cm.start() <= max_bound):
                        continue
                    cvv_cand = cm.group(0)
                    actual_start = cm.start()
                    
                    if cvv_cand in card or cvv_cand in best_date.replace('|', ''):
                        continue
                    if cvv_cand in YEAR_EXCLUSIONS:
                        continue
                        
                    left_idx = actual_start - 1
                    right_idx = actual_start + len(cvv_cand)
                    if (left_idx >= 0 and trimmed[left_idx].isdigit()) or (right_idx < len(trimmed) and trimmed[right_idx].isdigit()):
                        continue
                        
                    dist = abs(actual_start - p_start)
                    if dist < min_cvv_score:
                        min_cvv_score = dist
                        best_cvv = cvv_cand
                        
                if best_cvv:
                    mm, yy = best_date.split('|')
                    found_cards.add(card)
                    all_results.append({'card': card, 'mm': mm, 'yy': yy, 'cvv': best_cvv})

    now = datetime.now()
    current_year_short = now.year % 100
    current_month = now.month
    
    valid = []
    for r in all_results:
        try:
            exp_month = int(r['mm'])
            exp_year = int(r['yy'])
            if exp_year > current_year_short or (exp_year == current_year_short and exp_month >= current_month):
                valid.append(f"{r['card']}|{r['mm']}|{r['yy']}|{r['cvv']}")
        except ValueError:
            continue

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
                raise HTTPException(status_code=413, detail="Payload Too Large (Max 50MB)")
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
        raise HTTPException(status_code=400, detail="Missing or invalid text body")

    result = process_text_cleaning(raw_text)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
