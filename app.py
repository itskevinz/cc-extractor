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

    def _resolve_expiry(self, a, b):
        """Tự động xác định MM và YY/YYYY từ 2 giá trị.
        Xử lý cả trường hợp đảo ngược như 27/08 -> 08/2027.
        Trả về (month, year) hoặc (None, None)."""
        try:
            int_a = int(a)
            int_b = int(b)
            len_a = len(a)
            len_b = len(b)

            # Case: a là 4-digit year
            if len_a == 4 and 2020 <= int_a <= self.max_year:
                if 1 <= int_b <= 12:
                    return str(int_b).zfill(2), str(int_a)

            # Case: b là 4-digit year
            if len_b == 4 and 2020 <= int_b <= self.max_year:
                if 1 <= int_a <= 12:
                    return str(int_a).zfill(2), str(int_b)

            # Case: a là 2-digit, b là 2-digit (MM/YY hoặc YY/MM)
            if len_a == 2 and len_b == 2:
                if int_a > 12 and int_b <= 12:
                    return str(int_b).zfill(2), '20' + str(int_a)
                if int_b > 12 and int_a <= 12:
                    return str(int_a).zfill(2), '20' + str(int_b)
                if int_a <= 12 and int_b <= 12:
                    if int_b >= 20:
                        return str(int_a).zfill(2), '20' + str(int_b)
                    elif int_a >= 20:
                        return str(int_b).zfill(2), '20' + str(int_a)

            # Case: a là 1-2 digit, b là 2-digit
            if len_b == 2:
                if int_a > 12 and int_b <= 12:
                    return str(int_b).zfill(2), '20' + str(int_a)
                if int_b > 12 and int_a <= 12:
                    return str(int_a).zfill(2), '20' + str(int_b)
                if int_a <= 12 and int_b <= 12 and int_b >= 20:
                    return str(int_a).zfill(2), '20' + str(int_b)

            # Case: a là 2-digit, b là 1-2 digit
            if len_a == 2:
                if int_a > 12 and int_b <= 12:
                    return str(int_b).zfill(2), '20' + str(int_a)
                if int_b > 12 and int_a <= 12:
                    return str(int_a).zfill(2), '20' + str(int_b)
                if int_a <= 12 and int_b <= 12 and int_a >= 20:
                    return str(int_b).zfill(2), '20' + str(int_a)

            return None, None
        except Exception:
            return None, None

    def _init_patterns(self):
        """Khởi tạo tất cả patterns trích xuất - siêu toàn diện"""
        self.patterns = []

        # ===== PATTERNS CŨ (giữ nguyên backward compatibility) =====
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{1,2})\|(\d{4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{6})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+\S+\s+(\d{6})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{2})/(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})::(\d{1,2})::(\d{4})::(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})----(\d{1,2})----(\d{4})----(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'CCNUM\s*(\d{13,19})\s*EXP\s*(\d{1,2})/(\d{2,4})\s*CVV\s*(\d{3,4})', re.I),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r"'card_num':\s*'(\d{13,19})',.*?'expiry_date':\s*'(\d{2})(\d{2,4})',.*?'cvv':\s*'(\d{3,4})'"),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})', re.I),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{2})/(\d{4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\|(\d{1,2})\|(\d{2})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), '20' + m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\n(\d{2})/(\d{2,4})\n(\d{3})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{6})\s+(\d{3,4})\s*$'),
            lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(4))
        ))
        self.patterns.append((
            re.compile(r'(\d{13,19})\s+(\d{1,2})\s+(\d{4})\s+(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'.*?\|\d\|.*?\|(\d{13,19})\|(\d{2})(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))
        self.patterns.append((
            re.compile(r'CC:\s*(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})'),
            lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))
        ))

        # ===== PATTERNS MỚI - Multi-line & Human-like =====
        # Multi-line có nhãn: CCNUM\nPAN\nEXP\nMM/YY\nCVV\nXXX
        self.patterns.append((
            re.compile(r'(?:CCNUM|CARD|CC|NUMBER|#)[\s:]*\n?\s*(\d[\d\s]{12,22}\d)[\s\S]{0,50}?(?:EXP|EXPIRY|EXPIRATION|EXP DATE|VALID THRU)[\s:]*\n?\s*(\d{1,2})[\s/\\\-\~.]+(\d{2,4})[\s\S]{0,50}?(?:CVV|CVC|CVV2|CVC2|CODE)[\s:]*\n?\s*(\d{3,4})', re.I),
            lambda m, self=self: (
                re.sub(r'\s', '', m.group(1)),
                *self._resolve_expiry(m.group(2), m.group(3)),
                m.group(4)
            ) if self._resolve_expiry(m.group(2), m.group(3))[0] else None
        ))

        # Bullet points: • CC ⌁ PAN • Exp ⌁ MM ~ YYYY • Cvv ⌁ CVV
        self.patterns.append((
            re.compile(r'[•\-\*\+>]\s*(?:CC|CARD|PAN|#)\s*[⌁:>\-=~.\|/\\]+\s*(\d[\d\s]{12,22}\d)\s*[•\-\*\+>]\s*(?:EXP|EXPIRY|VALID)\s*[⌁:>\-=~.\|/\\]+\s*(\d{1,2})\s*[~\-/.\\\]+\s*(\d{2,4})\s*[•\-\*\+>]\s*(?:CVV|CVC|CODE)\s*[⌁:>\-=~.\|/\\]+\s*(\d{3,4})', re.I),
            lambda m, self=self: (
                re.sub(r'\s', '', m.group(1)),
                *self._resolve_expiry(m.group(2), m.group(3)),
                m.group(4)
            ) if self._resolve_expiry(m.group(2), m.group(3))[0] else None
        ))

        # Key-value: Card: PAN, Exp: MM/YY, CVV: XXX
        self.patterns.append((
            re.compile(r'(?:card|cc|pan|number)[\s:]*[=:]+\s*(\d[\d\s]{12,22}\d)[,;\s]*(?:exp|expiry|valid)[\s:]*[=:]+\s*(\d{1,2})[\s/\\\-\~.]+(\d{2,4})[,;\s]*(?:cvv|cvc|code)[\s:]*[=:]+\s*(\d{3,4})', re.I),
            lambda m, self=self: (
                re.sub(r'\s', '', m.group(1)),
                *self._resolve_expiry(m.group(2), m.group(3)),
                m.group(4)
            ) if self._resolve_expiry(m.group(2), m.group(3))[0] else None
        ))

        # PAN có space + MM/YY + CVV cùng dòng
        self.patterns.append((
            re.compile(r'(\d{4}\s+\d{4}\s+\d{4}\s+\d{4}(?:\s+\d{4})?)\s+(\d{1,2})[\s/\\\-\~.]+(\d{2,4})\s+(\d{3,4})'),
            lambda m, self=self: (
                re.sub(r'\s', '', m.group(1)),
                *self._resolve_expiry(m.group(2), m.group(3)),
                m.group(4)
            ) if self._resolve_expiry(m.group(2), m.group(3))[0] else None
        ))

    def _normalize_text(self, text):
        """Chuẩn hóa text: full-width digits + letters, ký tự đặc biệt, newline"""
        # Full-width digits + letters -> half-width
        text = text.translate(str.maketrans(
            '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ',
            '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
        ))
        # Ký tự separator đặc biệt -> chuẩn
        text = re.sub(r'[⌁∶：﹕｡．‥…．。･・•・∙⋅]', ':', text)
        text = re.sub(r'[～∼〜﹋﹌]', '~', text)
        text = re.sub(r'[／]', '/', text)
        text = re.sub(r'[＼]', r'\\', text)
        text = re.sub(r'[｜]', '|', text)
        # Chuẩn hóa newline
        text = re.sub(r'\r\n|\r', '\n', text)
        return text

    def _find_pan_candidates(self, text):
        """Tìm tất cả PAN candidates: liền hoặc có space"""
        candidates = []
        for m in re.finditer(r'\b(\d{13,19})\b', text):
            candidates.append((m.group(1), m.start(), m.end()))
        for m in re.finditer(r'\b(\d{4}\s+\d{4}\s+\d{4}\s+\d{4}(?:\s+\d{4})?)\b', text):
            pan_clean = re.sub(r'\s', '', m.group(1))
            if 13 <= len(pan_clean) <= 19:
                candidates.append((pan_clean, m.start(), m.end()))
        return candidates

    def _find_expiry_in_context(self, text, pan_start, pan_end, window=200):
        """Tìm expiry trong vùng lân cận PAN, loại trừ chính PAN"""
        start = max(0, pan_start - window)
        end = min(len(text), pan_end + window)
        context = text[start:pan_start] + ' ' + text[pan_end:end]

        exp_patterns = [
            r'(?:exp|expiry|expiration|valid|thru|date)[\s:]*[=:]+\s*(\d{1,2})\s*[\/\\\-\~.]\s*(\d{2,4})',
            r'(?:exp|expiry)?\s*(\d{1,2})\s*[~\-\.]\s*(\d{4})',
            r'\b(\d{1,2})\s*[\/\\\-\~.]\s*(\d{2,4})\b',
            r'\b(\d{4})\s*[\/\\\-\~.]\s*(\d{1,2})\b',
            r'\b(20\d{2})(\d{2})\b',
        ]

        for pattern in exp_patterns:
            for m in re.finditer(pattern, context, re.I):
                groups = m.groups()
                if len(groups) == 2:
                    month, year = self._resolve_expiry(groups[0], groups[1])
                    if month and year:
                        return month, year
        return None, None

    def _find_cvv_in_context(self, text, pan_start, pan_end, window=200):
        """Tìm CVV trong vùng lân cận PAN, loại trừ chính PAN"""
        start = max(0, pan_start - window)
        end = min(len(text), pan_end + window)
        context = text[start:pan_start] + ' ' + text[pan_end:end]

        cvv_patterns = [
            r'(?:cvv|cvc|cvv2|cvc2|code|security)[\s:]*[=:]+\s*(\d{3,4})',
            r'(?:cvv|cvc|code)\s*[⌁:>\-=~.\|/\\]+\s*(\d{3,4})',
        ]
        for pattern in cvv_patterns:
            m = re.search(pattern, context, re.I)
            if m:
                return m.group(1)

        tokens = re.findall(r'\b(\d{3,4})\b', context)
        for t in tokens:
            if t not in ['000', '0000']:
                return t
        return None

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
            if len(str(y)) == 2:
                y = 2000 + y
            if y > self.max_year or not (1 <= m <= 12):
                return False
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

    def _smart_extract(self, text):
        """Trích xuất thông minh: PAN -> tìm EXP và CVV lân cận"""
        results = set()
        pan_candidates = self._find_pan_candidates(text)

        for pan, start, end in pan_candidates:
            if not self._is_luhn_valid(pan):
                continue

            month, year = self._find_expiry_in_context(text, start, end)
            cvv = self._find_cvv_in_context(text, start, end)

            if month and year and cvv:
                year = self._normalize_year(year)
                if self._is_not_expired(month, year):
                    key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                    results.add(key)

        return results

    def _pattern_extract(self, text):
        """Trích xuất bằng patterns"""
        results = set()
        for pattern, extractor in self.patterns:
            for m in pattern.finditer(text):
                try:
                    result = extractor(m)
                    if result is None:
                        continue
                    pan, month, year, cvv = result
                    if not month or not year:
                        continue
                    year = self._normalize_year(year)
                    if self._is_luhn_valid(pan) and self._is_not_expired(month, year):
                        key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                        results.add(key)
                except Exception:
                    continue
        return results

    def _pure_fallback(self, text):
        """Fallback cuối cùng: tìm PAN, tháng, năm, CVV từ các token số"""
        tokens = re.findall(r'\d+', text)
        if not tokens:
            return None

        pan = None
        for t in tokens:
            if 13 <= len(t) <= 19 and self._is_luhn_valid(t):
                pan = t
                break

        if not pan:
            return None

        tokens.remove(pan)

        cvv = None
        for t in reversed(tokens):
            if len(t) in [3, 4]:
                cvv = t
                tokens.remove(cvv)
                break

        month, year = None, None

        for t in tokens:
            if len(t) == 6:
                y, m = t[:4], t[4:]
                if 1 <= int(m) <= 12 and int(y) >= 2020:
                    year, month = y, m
                    break
                m, y = t[:2], t[2:]
                if 1 <= int(m) <= 12 and int(y) >= 2020:
                    year, month = y, m
                    break

        if not month or not year:
            for t in tokens:
                if len(t) == 4:
                    m, y = t[:2], t[2:]
                    if 1 <= int(m) <= 12 and int(y) >= 20:
                        year, month = '20' + y, m
                        break

        if not year:
            for t in tokens:
                if len(t) == 4 and 2020 <= int(t) <= self.max_year:
                    year = t
                    tokens.remove(t)
                    break

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

        # Bước 1: Chuẩn hóa text
        text = self._normalize_text(raw_text)

        # Bước 2: Trích xuất bằng patterns (single-line + multi-line)
        cleaned.update(self._pattern_extract(text))

        # Bước 3: Trích xuất thông minh (human-like) cho những cái chưa match
        cleaned.update(self._smart_extract(text))

        # Bước 4: Xử lý từng dòng với fallback cũ (backward compatibility)
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            has_pan = any(pan in line for pan in [c.split('|')[0] for c in cleaned])
            if not has_pan:
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
