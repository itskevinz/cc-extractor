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

class SuperCardExtractor:
    def __init__(self):
        self.current_date = datetime.now()
        self.current_year = self.current_date.year
        self.current_month = self.current_date.month
        self.max_year = 2045
        self._init_patterns()

    def _init_patterns(self):
        """Khởi tạo tất cả patterns trích xuất - siêu toàn diện"""
        self.patterns = []
        
        # Pattern 1: PAN|MM|YYYY|CVV (pipe với MM và YYYY riêng, 4-digit year)
        # 5326560002649746|8|2024|147
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{1,2})\|(\d{4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 2: PAN|YYYYMM|CVV (pipe với YYYYMM liền)
        # 5241150365343509|202907|437
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{6})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))
        ))
        
        # Pattern 3: PAN Name YYYYMM CVV (space format)
        # 4596930012599858 HolderName 202804 848 AUSTRALIA
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+\S+\s+(\d{6})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))
        ))
        
        # Pattern 4: PAN|MM/YY|CVV (AMEX style với slash)
        # 377660901155941|03/25|6028
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{2})/(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 5: PAN MM/YY CVV (space with slash)
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 6: PAN::MM::YYYY::CVV
        self.patterns.append((
            re.compile(r'(\d{13,19})::(\d{1,2})::(\d{4})::(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 7: PAN----MM----YYYY----CVV
        self.patterns.append((
            re.compile(r'(\d{13,19})----(\d{1,2})----(\d{4})----(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 8: CCNUM PAN EXP MM/YY CVV CVV
        self.patterns.append((
            re.compile(r'CCNUM\s*(\d{13,19})\s*EXP\s*(\d{1,2})/(\d{2,4})\s*CVV\s*(\d{3,4})', re.I),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 9: JSON format
        self.patterns.append((
            re.compile(r"'card_num':\s*'(\d{13,19})',.*?'expiry_date':\s*'(\d{2})(\d{2,4})',.*?'cvv':\s*'(\d{3,4})'"),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 10: Number: PAN Expiry: MM/YY CVV: CVV
        self.patterns.append((
            re.compile(r'Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})', re.I),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 11: PAN|MM/YYYY|CVV
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{2})/(\d{4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 12: PAN|MM|YY|CVV (2-digit year)
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{1,2})\|(\d{2})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), '20' + m.group(3), m.group(4))
        ))
        
        # Pattern 13: PAN (newline) MM/YY (newline) CVV
        self.patterns.append((
            re.compile(r'(\d{13,19})\n(\d{2})/(\d{2,4})\n(\d{3})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 14: PAN YYYYMM CVV (no name, just space)
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{6})\s+(\d{3,4})\s*$'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))
        ))
        
        # Pattern 15: PAN MM YYYY CVV (space separated)
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{1,2})\s+(\d{4})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 16: PAN|MM|YYYY|CVV với nhiều pipe sau
        # .*?\|\d\|.*?\|PAN\|MMYY\|CVV
        self.patterns.append((
            re.compile(r'.*?\|\d\|.*?\|(\d{13,19})\|(\d{2})(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 17: PAN|MM|YY|CVV với delimiter |
        self.patterns.append((
            re.compile(r'CC:\s*(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        
        # Pattern 18: PAN MM YYYY CVV (space separated full)
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?\s+.*?'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))

    def _is_luhn_valid(self, card_number):
        """Kiểm tra thuật toán Luhn"""
        try:
            digits = [int(d) for d in str(card_number)]
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(divmod(d * 2, 10))
            return checksum % 10 == 0
        except Exception:
            return False

    def _is_not_expired(self, month_str, year_str):
        """Kiểm tra thẻ chưa hết hạn, năm tối đa 2045"""
        try:
            m = int(month_str)
            y = int(year_str)
            
            # Chuẩn hóa năm
            if len(str(y)) == 2:
                y = 2000 + y
            
            # Kiểm tra năm hợp lệ
            if y > self.max_year:
                return False
            if not (1 <= m <= 12):
                return False
            
            # So sánh với ngày hiện tại
            if y < self.current_year:
                return False
            if y == self.current_year and m < self.current_month:
                return False
            return True
        except Exception:
            return False

    def _normalize_year(self, year_str):
        """Chuẩn hóa năm về 4 chữ số"""
        y = int(year_str)
        if len(str(y)) == 2:
            y = 2000 + y
        return str(y)

    def _pure_fallback(self, text):
        """Fallback cuối cùng: tìm PAN, tháng, năm, CVV từ các token số"""
        tokens = re.findall(r'\d+', text)
        if not tokens:
            return None
        
        # Tìm PAN (13-19 chữ số)
        pan = None
        for t in tokens:
            if 13 <= len(t) <= 19 and self._is_luhn_valid(t):
                pan = t
                break
        
        if not pan:
            return None
        
        tokens.remove(pan)
        
        # Tìm CVV (3-4 chữ số, thường ở cuối)
        cvv = None
        for t in reversed(tokens):
            if len(t) in [3, 4]:
                cvv = t
                tokens.remove(cvv)
                break
        
        # Tìm tháng và năm
        month, year = None, None
        
        # Thử tìm YYYYMM (6 chữ số)
        for t in tokens:
            if len(t) == 6:
                y, m = t[:4], t[4:]
                if 1 <= int(m) <= 12 and int(y) >= 2020:
                    year, month = y, m
                    break
                # Hoặc MMYYYY
                m, y = t[:2], t[2:]
                if 1 <= int(m) <= 12 and int(y) >= 2020:
                    year, month = y, m
                    break
        
        # Thử tìm MMYY (4 chữ số)
        if not month or not year:
            for t in tokens:
                if len(t) == 4:
                    m, y = t[:2], t[2:]
                    if 1 <= int(m) <= 12 and int(y) >= 20:
                        year, month = '20' + y, m
                        break
        
        # Tìm năm 4 chữ số
        if not year:
            for t in tokens:
                if len(t) == 4 and 2020 <= int(t) <= self.max_year:
                    year = t
                    tokens.remove(t)
                    break
        
        # Tìm tháng 1-2 chữ số
        if not month:
            for t in tokens:
                if len(t) in [1, 2] and 1 <= int(t) <= 12:
                    month = t.zfill(2)
                    tokens.remove(t)
                    break
        
        if pan and month and year and cvv:
            return pan, month.zfill(2), year, cvv
        return None

    def extract(self, raw_text):
        """Trích xuất tất cả CC hợp lệ, chưa hết hạn từ text"""
        cleaned = set()
        
        # Xử lý từng dòng
        for line in raw_text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            
            matched = False
            
            # Thử tất cả patterns
            for pattern, extractor in self.patterns:
                m = pattern.search(line)
                if m:
                    try:
                        pan, month, year, cvv = extractor(m)
                        
                        # Chuẩn hóa năm
                        year = self._normalize_year(year)
                        
                        # Kiểm tra Luhn và hết hạn
                        if self._is_luhn_valid(pan) and self._is_not_expired(month, year):
                            key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                            cleaned.add(key)
                            matched = True
                            break
                    except Exception:
                        continue
            
            # Fallback nếu không match pattern nào
            if not matched:
                fallback = self._pure_fallback(line)
                if fallback:
                    pan, month, year, cvv = fallback
                    year = self._normalize_year(year)
                    if self._is_luhn_valid(pan) and self._is_not_expired(month, year):
                        key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                        cleaned.add(key)
        
        # Sắp xếp theo BIN (6 số đầu) từ thấp đến cao
        return sorted(list(cleaned), key=lambda x: x[:6])

extractor = SuperCardExtractor()

def process_text_cleaning(text: str) -> dict:
    """Xử lý text và trả về kết quả - giữ format API cũ"""
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
