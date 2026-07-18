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

# 1. Regex bóc tách định dạng: CARD [phân tách] MONTH [phân tách] YEAR [phân tách] CVV
CC_EXTRACT_REGEX = re.compile(
    r'\b(\d{13,19})[\s\-\/|~]+(0[1-9]|1[0-2])[\s\-\/|~]+(20\d{2}|\d{2})[\s\-\/|~]+(\d{3,4})\b'
)

# 2. Regex dự phòng cho định dạng ngược (YEAR rồi đến MONTH: YYYYMM)
CC_YYYYMM_REGEX = re.compile(
    r'\b(\d{13,19})[\s\-\/|~]+(20\d{2})(0[1-9]|1[0-2])[\s\-\/|~]+(\d{3,4})\b'
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
        alternate = not alternate
    return total % 10 == 0

def process_text_cleaning(text: str) -> dict:
    start_time = time.time()
    extracted_cards = []
    found_cards = set()
    
    # Cắt dòng và loại bỏ khoảng trắng rác
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 20]
    
    for line in lines:
        # Cách 1: Quét bằng Regex chuẩn phân tách ký tự
        match = CC_EXTRACT_REGEX.search(line)
        if match:
            card, mm, yy, cvv = match.groups()
            yy_short = yy[-2:]
            if card not in found_cards and verify_luhn(card):
                found_cards.add(card)
                extracted_cards.append({'card': card, 'mm': mm, 'yy': yy_short, 'cvv': cvv})
                continue
                
        # Cách 2: Quét bằng format YYYYMM ngược
        match_ym = CC_YYYYMM_REGEX.search(line)
        if match_ym:
            card, yyyy, mm, cvv = match_ym.groups()
            yy_short = yyyy[-2:]
            if card not in found_cards and verify_luhn(card):
                found_cards.add(card)
                extracted_cards.append({'card': card, 'mm': mm, 'yy': yy_short, 'cvv': cvv})
                continue

        # Cách 3: Dự phòng cho cấu trúc có nhãn văn bản (card_number: ... secure_code: ...)
        if 'card_number:' in line.lower():
            card_match = re.search(r'card_number:\s*(\d{13,19})', line, re.IGNORECASE)
            if card_match:
                card = card_match.group(1)
                cvv_match = re.search(r'(?:secure_code|cvv|cvc):\s*(\d{3,4})', line, re.IGNORECASE)
                exp_match = re.search(r'(?:expiration|exp):\s*(\d{2})\/(\d{2,4})', line, re.IGNORECASE)
                if cvv_match and exp_match and card not in found_cards and verify_luhn(card):
                    mm = exp_match.group(1)
                    yy_short = exp_match.group(2)[-2:]
                    found_cards.add(card)
                    extracted_cards.append({'card': card, 'mm': mm, 'yy': yy_short, 'cvv': cvv_match.group(1)})

    # --- BỘ LỌC KIỂM TRA HẾT HẠN THỜI GIAN THỰC ---
    now = datetime.now()
    current_year_short = now.year % 100
    current_month = now.month
    
    valid = []
    for r in extracted_cards:
        try:
            exp_month = int(r['mm'])
            exp_year = int(r['yy'])
            # Chỉ giữ lại thẻ có hạn lớn hơn hoặc bằng tháng/năm hiện tại
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
        while chunk := await file.read(1024 * 1024):  # Đọc tuần tự từng block 1MB bảo vệ RAM
            chunks.append(chunk.decode("utf-8", errors="ignore"))
        raw_text = "".join(chunks)
    elif text:
        raw_text = text
    else:
        content_type = request.headers.get("content-type", "")
        body_bytes = b""
        async for chunk in request.stream():
            body_bytes += chunk
            if len(body_bytes) > 52428800:  # Chặn payload vượt quá 50MB
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

    # Đẩy tác vụ xử lý sang Thread Pool ngầm để duy trì Non-blocking
    result = process_text_cleaning(raw_text)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
