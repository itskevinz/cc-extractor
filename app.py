import os
import re
import sys
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
    CARD_TYPES = [
        (re.compile(r'^3[47]\d{13}$'), 4),
        (re.compile(r'^4\d{12}(?:\d{3})?$'), 3),
        (re.compile(r'^(?:5[1-5]\d{4}|222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720)\d{10}$'), 3),
        (re.compile(r'^3(?:0[0-5]|[68]\d)\d{11}$'), 3),
        (re.compile(r'^6(?:011|5\d{2}|4[4-9]\d)\d{12}$'), 3),
        (re.compile(r'^(?:2131|1800|35\d{3})\d{11}$'), 3),
    ]

    _PAN_SPACED_RE = re.compile(r'\b(\d{4}\s+\d{4}\s+\d{4}\s+\d{4}(?:\s+\d{4})?)\b')
    _PAN_PLAIN_RE = re.compile(r'\b(\d{13,19})\b')
    _TOKEN_RE = re.compile(r'\d+')
    _CVV_RE = re.compile(r'\b(\d{3,4})\b')

    _FW_MAP = str.maketrans(
        '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ',
        '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    )

    def __init__(self):
        self.current_date = datetime.now()
        self.current_year = self.current_date.year
        self.current_month = self.current_date.month
        self.max_year = 2045
        self._patterns = self._compile_patterns()
        self._exp_ctx_patterns = [
            re.compile(r'(?:exp|expiry|expiration|valid|thru|date)[\s:]*[=:]+\s*(\d{1,2})\s*[\/\\\-~.]\s*(\d{2,4})', re.I),
            re.compile(r'(?:exp|expiry)?\s*(\d{1,2})\s*[~\-]\s*(\d{4})', re.I),
            re.compile(r'\b(\d{1,2})\s*[\/\\\-~.]\s*(\d{2,4})\b'),
            re.compile(r'\b(\d{4})\s*[\/\\\-~.]\s*(\d{1,2})\b'),
            re.compile(r'\b(20\d{2})(\d{2})\b'),
        ]
        self._cvv_ctx_patterns = [
            re.compile(r'(?:cvv|cvc|cvv2|cvc2|code|security)[\s:]*[=:]+\s*(\d{3,4})', re.I),
            re.compile(r'(?:cvv|cvc|code)\s*[⌁:>\-=~.\|/\\]+\s*(\d{3,4})', re.I),
        ]

    def _get_cvv_length(self, pan):
        for prefix_re, cvv_len in self.CARD_TYPES:
            if prefix_re.match(pan):
                return cvv_len
        return 3

    def _resolve_expiry(self, a, b):
        try:
            int_a, int_b = int(a), int(b)
            len_a, len_b = len(a), len(b)
            if len_a == 4 and 2020 <= int_a <= self.max_year and 1 <= int_b <= 12:
                return str(int_b).zfill(2), str(int_a)
            if len_b == 4 and 2020 <= int_b <= self.max_year and 1 <= int_a <= 12:
                return str(int_a).zfill(2), str(int_b)
            if len_a == 2 and len_b == 2:
                if int_a > 12 and int_b <= 12:
                    return str(int_b).zfill(2), '20' + str(int_a)
                if int_b > 12 and int_a <= 12:
                    return str(int_a).zfill(2), '20' + str(int_b)
                if int_a <= 12 and int_b <= 12:
                    if int_b >= 20:
                        return str(int_a).zfill(2), '20' + str(int_b)
                    if int_a >= 20:
                        return str(int_b).zfill(2), '20' + str(int_a)
            if len_b == 2:
                if int_a > 12 and int_b <= 12:
                    return str(int_b).zfill(2), '20' + str(int_a)
                if int_b > 12 and int_a <= 12:
                    return str(int_a).zfill(2), '20' + str(int_b)
                if int_a <= 12 and int_b <= 12 and int_b >= 20:
                    return str(int_a).zfill(2), '20' + str(int_b)
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

    def _compile_patterns(self):
        patterns = []
        old = [
            (r'(\d{13,19})\|(\d{1,2})\|(\d{4})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})\|(\d{6})\|(\d{3,4})', lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))),
            (r'(\d{13,19})\s+\S+\s+(\d{6})\s+(\d{3,4})', lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(3))),
            (r'(\d{13,19})\|(\d{2})/(\d{2,4})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})::(\d{1,2})::(\d{4})::(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})----(\d{1,2})----(\d{4})----(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'CCNUM\s*(\d{13,19})\s*EXP\s*(\d{1,2})/(\d{2,4})\s*CVV\s*(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r"'card_num':\s*'(\d{13,19})',.*?'expiry_date':\s*'(\d{2})(\d{2,4})',.*?'cvv':\s*'(\d{3,4})'", lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})\|(\d{2})/(\d{4})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})\|(\d{1,2})\|(\d{2})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), '20' + m.group(3), m.group(4))),
            (r'(\d{13,19})\n(\d{2})/(\d{2,4})\n(\d{3})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'(\d{13,19})\s+(\d{6})\s+(\d{3,4})\s*$', lambda m: (m.group(1), m.group(2)[4:].zfill(2), m.group(2)[:4], m.group(4))),
            (r'(\d{13,19})\s+(\d{1,2})\s+(\d{4})\s+(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'.*?\|\d\|.*?\|(\d{13,19})\|(\d{2})(\d{2,4})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'CC:\s*(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'[⇻↬↫✅❌]\s*CC\s*[:：]\s*(\d{13,19})\s*[\|｜]\s*(\d{1,2})\s*[\|｜]\s*(\d{4})\s*[\|｜]\s*(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), m.group(3), m.group(4))),
            (r'[⇻↬↫✅❌]\s*CC\s*[:：]\s*(\d{13,19})\s*[\|｜]\s*(\d{1,2})\s*[\|｜]\s*(\d{2})\s*[\|｜]\s*(\d{3,4})', lambda m: (m.group(1), m.group(2).zfill(2), '20' + m.group(3), m.group(4))),
        ]
        for pat, extr in old:
            patterns.append((re.compile(pat, re.I), extr))

        new = [
            (r'(?:CCNUM|CARD|CC|NUMBER|#)[\s:]*\n?\s*(\d[\d\s]{12,22}\d)[\s\S]{0,50}?'
             r'(?:EXP|EXPIRY|EXPIRATION|EXP DATE|VALID THRU)[\s:]*\n?\s*(\d{1,2})[\s/\\\-~.]+(\d{2,4})'
             r'[\s\S]{0,50}?(?:CVV|CVC|CVV2|CVC2|CODE)[\s:]*\n?\s*(\d{3,4})',
             lambda m: (re.sub(r'\s', '', m.group(1)), *self._resolve_expiry(m.group(2), m.group(3)), m.group(4))),
            (r'[•\-\*\+>]\s*(?:CC|CARD|PAN|#)\s*[⌁:>\-=~.\|/\\]+\s*(\d[\d\s]{12,22}\d)\s*[•\-\*\+]>'
             r'\s*(?:EXP|EXPIRY|VALID)\s*[⌁:>\-=~.\|/\\]+\s*(\d{1,2})\s*[~\-/.\\]+\s*(\d{2,4})\s*[•\-\*\+]>'
             r'\s*(?:CVV|CVC|CODE)\s*[⌁:>\-=~.\|/\\]+\s*(\d{3,4})',
             lambda m: (re.sub(r'\s', '', m.group(1)), *self._resolve_expiry(m.group(2), m.group(3)), m.group(4))),
            (r'(?:card|cc|pan|number)[\s:]*[=:]+\s*(\d[\d\s]{12,22}\d)[,;\s]*'
             r'(?:exp|expiry|valid)[\s:]*[=:]+\s*(\d{1,2})[\s/\\\-~.]+(\d{2,4})[,;\s]*'
             r'(?:cvv|cvc|code)[\s:]*[=:]+\s*(\d{3,4})',
             lambda m: (re.sub(r'\s', '', m.group(1)), *self._resolve_expiry(m.group(2), m.group(3)), m.group(4))),
            (r'(\d{4}\s+\d{4}\s+\d{4}\s+\d{4}(?:\s+\d{4})?)\s+(\d{1,2})[\s/\\\-~.]+(\d{2,4})\s+(\d{3,4})',
             lambda m: (re.sub(r'\s', '', m.group(1)), *self._resolve_expiry(m.group(2), m.group(3)), m.group(4))),
        ]
        for pat, extr in new:
            patterns.append((re.compile(pat, re.I), extr))

        return patterns

    def _normalize_text(self, text):
        if not text:
            return ""
        text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff\u2060]', '', text)
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'[*_~`]', '', text)
        text = re.sub(r'[⌁∶：﹕｡．‥…．。･・•・∙⋅]', ':', text)
        text = re.sub(r'[～∼〜﹋﹌]', '~', text)
        text = re.sub(r'[／]', '/', text)
        text = re.sub(r'[＼]', r'\\', text)
        text = re.sub(r'[｜]', '|', text)
        text = re.sub(r'\r\n|\r', '\n', text)
        return text

    def _find_all_pans(self, text):
        candidates = []
        seen = set()
        for m in self._PAN_SPACED_RE.finditer(text):
            pan = re.sub(r'\s', '', m.group(1))
            if pan not in seen and 13 <= len(pan) <= 19:
                seen.add(pan)
                candidates.append((pan, m.start(), m.end()))
        for m in self._PAN_PLAIN_RE.finditer(text):
            pan = m.group(1)
            if pan not in seen:
                seen.add(pan)
                candidates.append((pan, m.start(), m.end()))
        return candidates

    def _find_expiry_near(self, text, pan_start, pan_end, window=150):
        start = max(0, pan_start - window)
        end = min(len(text), pan_end + window)
        context = text[start:pan_start] + ' ' + text[pan_end:end]
        for pat in self._exp_ctx_patterns:
            for m in pat.finditer(context):
                groups = m.groups()
                if len(groups) == 2:
                    month, year = self._resolve_expiry(groups[0], groups[1])
                    if month and year:
                        return month, year
        return None, None

    def _find_cvv_near(self, text, pan_start, pan_end, expected_len, window=150):
        start = max(0, pan_start - window)
        end = min(len(text), pan_end + window)
        context = text[start:pan_start] + ' ' + text[pan_end:end]
        for pat in self._cvv_ctx_patterns:
            m = pat.search(context)
            if m:
                cvv = m.group(1)
                if len(cvv) == expected_len:
                    return cvv
        for m in self._CVV_RE.finditer(context):
            cvv = m.group(1)
            if len(cvv) == expected_len and cvv not in ('000', '0000'):
                return cvv
        return None

    def _is_luhn_valid(self, card_number):
        try:
            digits = [int(d) for d in str(card_number)]
            odd = digits[-1::-2]
            even = digits[-2::-2]
            checksum = sum(odd)
            for d in even:
                checksum += sum(divmod(d * 2, 10))
            return checksum % 10 == 0
        except Exception:
            return False

    def _is_not_expired(self, month_str, year_str):
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
        y = int(year_str)
        if len(str(y)) == 2:
            y = 2000 + y
        return str(y)

    def _validate_card(self, pan, month, year, cvv):
        if not self._is_luhn_valid(pan):
            return False
        year = self._normalize_year(year)
        if not self._is_not_expired(month, year):
            return False
        if len(cvv) != self._get_cvv_length(pan):
            return False
        return True

    def _smart_extract(self, text):
        results = set()
        for pan, start, end in self._find_all_pans(text):
            expected_cvv = self._get_cvv_length(pan)
            month, year = self._find_expiry_near(text, start, end)
            cvv = self._find_cvv_near(text, start, end, expected_cvv)
            if month and year and cvv and self._validate_card(pan, month, year, cvv):
                key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                results.add(key)
        return results

    def _pattern_extract(self, text):
        results = set()
        for pattern, extractor in self._patterns:
            for m in pattern.finditer(text):
                try:
                    result = extractor(m)
                    if result is None:
                        continue
                    pan, month, year, cvv = result
                    if not month or not year:
                        continue
                    if self._validate_card(pan, month, year, cvv):
                        year = self._normalize_year(year)
                        key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                        results.add(key)
                except Exception:
                    continue
        return results

    def _fallback_per_line(self, text, already_found_pans):
        results = set()
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if any(pan in line for pan in already_found_pans):
                continue
            tokens = self._TOKEN_RE.findall(line)
            if not tokens:
                continue
            pan = None
            for t in tokens:
                if 13 <= len(t) <= 19 and self._is_luhn_valid(t):
                    pan = t
                    break
            if not pan:
                continue
            expected_cvv = self._get_cvv_length(pan)
            tokens.remove(pan)
            cvv = None
            for t in reversed(tokens):
                if len(t) == expected_cvv:
                    cvv = t
                    tokens.remove(cvv)
                    break
            if not cvv:
                continue
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
                        break
            if not month:
                for t in tokens:
                    if len(t) in (1, 2) and 1 <= int(t) <= 12:
                        month = t.zfill(2)
                        break
            if pan and month and year and self._validate_card(pan, month, year, cvv):
                year = self._normalize_year(year)
                key = f"{pan}|{month.zfill(2)}|{year[-2:]}|{cvv}"
                results.add(key)
        return results

    def extract(self, raw_text):
        cleaned = set()
        text = self._normalize_text(raw_text)
        cleaned.update(self._pattern_extract(text))
        cleaned.update(self._smart_extract(text))
        found_pans = {c.split('|')[0] for c in cleaned}
        cleaned.update(self._fallback_per_line(text, found_pans))
        return sorted(cleaned, key=lambda x: x[:6])


extractor = SuperCardExtractor()


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


@app.post('/shutdown')
async def shutdown():
    os._exit(0)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
