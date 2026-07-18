import os
import re
import time
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

# 1. Regex bóc tách thẻ siêu chính xác cho mọi cấu trúc phân tách (|, ~, /,空格)
# Định dạng: CARD [phân tách] MONTH [phân tách] YEAR [phân tách] CVV
CC_EXTRACT_REGEX = re.compile(
    r'\b(\d{13,19})[\s\-\/|~]+(0[1-9]|1[0-2])[\s\-\/|~]+(20\d{2}|\d{2})[\s\-\/|~]+(\d{3,4})\b'
)

# 2. Regex dự phòng cho trường hợp format ngược (YEAR rồi đến MONTH: YYYYMM)
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
    valid = set() # Dùng set để tự động loại bỏ trùng lặp (Tối ưu RAM)
    
    # Xử lý cắt dòng thông minh, loại bỏ rác trống trước khi chạy để tránh nuốt thẻ
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 20]
    
    for line in lines:
        # Cách 1: Quét bằng Regex chuẩn (Thả xích hoàn toàn cho cấu trúc dính liền User-Agent)
        match = CC_EXTRACT_REGEX.search(line)
        if match:
            card, mm, yy, cvv = match.groups()
            # Chuẩn hóa năm về 2 chữ số (Ví dụ: 2025 -> 25)
            yy_short = yy[-2:]
            if verify_luhn(card):
                valid.add(f"{card}|{mm}|{yy_short}|{cvv}")
                continue # Đã ăn được thẻ dòng này thì bỏ qua quét cách khác
                
        # Cách 2: Quét bằng format YYYYMM ngược
        match_ym = CC_YYYYMM_REGEX.search(line)
        if match_ym:
            card, yyyy, mm, cvv = match_ym.groups()
            yy_short = yyyy[-2:]
            if verify_luhn(card):
                valid.add(f"{card}|{mm}|{yy_short}|{cvv}")
                continue

        # Cách 3: Dự phòng cho cấu trúc nhãn cồng kềnh (card_number: ... secure_code: ...)
        if 'card_number:' in line.lower():
            card_match = re.search(r'card_number:\s*(\d{13,19})', line, re.IGNORECASE)
            if card_match:
                card = card_match.group(1)
                cvv_match = re.search(r'(?:secure_code|cvv|cvc):\s*(\d{3,4})', line, re.IGNORECASE)
                exp_match = re.search(r'(?:expiration|exp):\s*(\d{2})\/(\d{2,4})', line, re.IGNORECASE)
                if cvv_match and exp_match and verify_luhn(card):
                    mm = exp_match.group(1)
                    yy_short = exp_match.group(2)[-2:]
                    valid.add(f"{card}|{mm}|{yy_short}|{cvv_match.group(1)}")

    # Sắp xếp lại kết quả trả về
    formatted_results = sorted(list(valid))
    
    return {
        "success": True,
        "count": len(formatted_results),
        "processing_time_ms": int((time.time() - start_time) * 1000),
        "data": formatted_results
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
        while chunk := await file.read(1024 * 1024): # Stream từng block 1MB chống sập RAM
            chunks.append(chunk.decode("utf-8", errors="ignore"))
        raw_text = "".join(chunks)
    elif text:
        raw_text = text
    else:
        content_type = request.headers.get("content-type", "")
        body_bytes = b""
        async for chunk in request.stream():
            body_bytes += chunk
            if len(body_bytes) > 52428800: # Giới hạn max 50MB
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

    # Xử lý đa luồng ngầm hoàn toàn an toàn
    result = process_text_cleaning(raw_text)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
