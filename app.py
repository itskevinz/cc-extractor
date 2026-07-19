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

class HybridCardExtractor:
    def __init__(self):
        self.patterns = []
        self._load_default_patterns()
        current_date = datetime.now()
        self.current_year = current_date.year
        self.current_month = current_date.month

    def _load_default_patterns(self):
        self.patterns = [
            # === PATTERN MỚI: Dạng Australia ===
            # PAN Name YYYYMM CVV [email] [Country]
            # Ví dụ: 4596930012599858 HolderName 202804 848  AUSTRALIA
            # YYYYMM = 4 số năm + 2 số tháng
            re.compile(r"(\d{13,19})\s+\S+\s+(\d{4})(\d{2})\s+(\d{3,4})"),
            
            # === PATTERN MỚI: Dạng Taiwan ===
            # PAN|YYYYMM|CVV|TYPE|BANK|CREDIT|LEVEL|COUNTRY
            # Ví dụ: 5241150365343509|202907|437|MASTERCARD|BANK SINOPAC|CREDIT|TITANIUM|TW
            re.compile(r"(\d{13,19})\|(\d{4})(\d{2})\|(\d{3,4})\|"),
            
            # === PATTERN MỚI: Dạng Switzerland ===
            # PAN|MM/YY|CVV|Name|Address|...
            # Ví dụ: 5487190090873639|02/25|773|Vitorovic Goran|Altrheinweg 94|...
            re.compile(r"(\d{13,19})\|(\d{2})/(\d{2,4})\|(\d{3,4})\|"),
            
            # === CÁC PATTERN CŨ ===
            re.compile(r"(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})"),
            re.compile(r"CCNUM\s*(\d{13,19})\s*EXP\s*(\d{1,2})/(\d{2,4})\s*CVV\s*(\d{3,4})"),
            re.compile(r"(\d{13,19})::(\d{1,2})::(\d{2,4})::(\d{3,4})"),
            re.compile(r"(\d{13,19})\s+.*?\s+(\d{2})(\d{2,4})\s+(\d{3})"),
            re.compile(r"(\d{13,19})\n(\d{2})/(\d{2,4})\n(\d{3})"),
            re.compile(r"Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})"),
            re.compile(r"(\d{13,19})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3})"),
            re.compile(r"(\d{13,19})----(\d{1,2})----(\d{2,4})----(\d{3,4})"),
            re.compile(r".*?\|\d\|.*?\|(\d{13,19})\|(\d{2})(\d{2,4})\|(\d{3,4})"),
            re.compile(r".*?\|.*?\|.*?\|(\d{13,19})\|(\d{2})(\d{2,4})\|(\d{3,4})"),
            re.compile(r"CC:\s*(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})"),
            re.compile(r"'card_num':\s*'(\d{13,19})',.*?'expiry_date':\s*'(\d{2})(\d{2,4})',.*?'cvv':\s*'(\d{3,4})'"),
            re.compile(r"(\d{13,19})\s(\d{2})\s(\d{2,4})\s(\d{3})\s"),
            re.compile(r"(\d{13,19})\|(\d{2})/(\d{2,4})\|(\d{3,4})"),
            re.compile(r"(\d{13,19})\|(\d{2})\|(\d{2,4})\|(\d{3,4})"),
            re.compile(r"Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})\s*Name:\s*.*?\s*Address:\s*.*?\s*City:\s*.*?\s*State:\s*.*?\s*ZIP:\s*.*?\s*Country:\s*.*?\s*Phone:\s*.*?\s*Email:\s*.*?\s*IP:\s*.*?\s*Browser:\s*.*?"),
            re.compile(r"(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?")
        ]

    def _is_luhn_valid(self, card_number):
        try:
            digits = [int(d) for d in card_number]
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(divmod(d * 2, 10))
            return checksum % 10 == 0
        except Exception:
            return False

    def _is_not_expired(self, month_str, year_str):
        try:
            m = int(month_str)
            y = int(year_str)
            if len(year_str) == 2:
                y += 2000
            if not (1 <= m <= 12):
                return False
            if y < self.current_year:
                return False
            if y == self.current_year and m < self.current_month:
                return False
            return True
        except Exception:
            return False

    def _pure_fallback(self, text):
        tokens = re.findall(r'\d+', text)
        if not tokens:
            return None
        pan = next((t for t in tokens if 13 <= len(t) <= 19), None)
        if not pan:
            return None
        tokens.remove(pan)
        cvv = next((t for t in reversed(tokens) if len(t) in [3, 4]), None)
        if cvv:
            tokens.remove(cvv)
        month, year = None, None
        for t in tokens:
            if len(t) == 6:
                if 1 <= int(t[4:]) <= 12:
                    year, month = t[:4], t[4:]
                    break
                elif 1 <= int(t[:2]) <= 12:
                    month, year = t[:2], t[2:]
                    break
            elif len(t) == 4 and 1 <= int(t[:2]) <= 12:
                month, year = t[:2], "20" + t[2:]
                break
        if not month or not year:
            year_tok = next((t for t in tokens if len(t) == 4 and int(t) >= 2020), None)
            if year_tok:
                year = year_tok
                tokens.remove(year_tok)
            month_tok = next((t for t in tokens if len(t) in [1, 2] and 1 <= int(t) <= 12), None)
            if month_tok:
                month = month_tok
                tokens.remove(month_tok)
            if not year:
                year_tok = next((t for t in tokens if len(t) == 2), None)
                if year_tok:
                    year = "20" + year_tok
        if pan and month and year and cvv:
            return pan, month.zfill(2), year, cvv
        return None

    def extract(self, raw_text):
        cleaned = set()
        for line in raw_text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            matched = False
            for pattern in self.patterns:
                m = pattern.search(line)
                if m:
                    groups = m.groups()
                    if len(groups) == 4:
                        pan, month, year, cvv = groups
                        if len(year) == 2:
                            year = "20" + year
                        if self._is_luhn_valid(pan) and self._is_not_expired(month, year):
                            cleaned.add(f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}")
                        matched = True
                        break
            if not matched:
                fallback = self._pure_fallback(line)
                if fallback:
                    pan, month, year, cvv = fallback
                    if len(year) == 2:
                        year = "20" + year
                    if self._is_luhn_valid(pan) and self._is_not_expired(month, year):
                        cleaned.add(f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}")
        return sorted(list(cleaned))

extractor = HybridCardExtractor()

def process_text_cleaning(text: str) -> dict:
    start_time = time.time()
    valid_cards = extractor.extract(text)
    
    return {
        "success": True,
        "count": len(valid_cards),
        "processing_time_ms": int((time.time() - start_time) * 1000),
        "data": valid_cards
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
