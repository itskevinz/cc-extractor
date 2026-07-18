import re
from datetime import datetime
from typing import List, Set, Dict, Tuple, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import uvicorn

app = FastAPI()

class NonLinearCardExtractor:
    def __init__(self):
        self.pan_regex = re.compile(r'\b\d{13,19}\b')
        self.cvv_regex = re.compile(r'\b\d{3,4}\b')
        self.date_strict_regex = re.compile(r'\b(0[1-9]|1[0-2])[\s\-/|]?(20\d{2}|\d{2})\b')
        self.date_yyyymm_regex = re.compile(r'\b(20\d{2})(0[1-9]|1[0-2])\b')
        self.keywords = {
            "cvv": ["secure_code", "cvv", "cvc", "code", "secure", "cid", "cvv2"],
            "date": ["expiration", "exp", "expires", "date", "valid", "thru", "month", "year"],
            "exclude": ["bin", "order", "id", "phone", "tel", "amount", "zip", "ua", "ip"]
        }
        self.year_exclusions = set(str(y) for y in range(2020, 2040))

    @staticmethod
    def verify_luhn(card_number: str) -> bool:
        digits = [int(d) for d in card_number if d.isdigit()]
        if not (13 <= len(digits) <= 19):
            return False
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            val = d * 2
            checksum += val if val < 10 else val - 9
        return checksum % 10 == 0

    def _parse_line_formats(self, text: str) -> List[Tuple[str, str, str, str, str]]:
        results = []
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith(('country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:',
                               'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:',
                               'order:', 'cc|month|year|cvv')):
                continue
            m = re.match(r'^(\d{13,19})\|(\d{6})\|(\d{3,4})\b', line)
            if m:
                card, yyyymm, cvv = m.groups()
                if not self.verify_luhn(card):
                    continue
                results.append((card, yyyymm[4:6], yyyymm[2:4], cvv, "format_yyyymm"))
                continue
            m = re.match(r'^(\d{13,19})\|(\d{2})/(\d{2})\|(\d{3,4})\b', line)
            if m:
                card, mm, yy, cvv = m.groups()
                if not self.verify_luhn(card):
                    continue
                results.append((card, mm, yy, cvv, "format_mm_slash_yy"))
                continue
            m = re.match(r'^(\d{13,19})~(\d{2})/(\d{2})~(\d{3,4})\b', line)
            if m:
                card, mm, yy, cvv = m.groups()
                if not self.verify_luhn(card):
                    continue
                results.append((card, mm, yy, cvv, "format_tilde"))
                continue
            m = re.match(r'^(\d{13,19})\|(\d{2})\|(\d{2})\|(\d{3,4})\b', line)
            if m:
                card, mm, yy, cvv = m.groups()
                if not self.verify_luhn(card):
                    continue
                results.append((card, mm, yy, cvv, "format_pipe_mm_yy"))
                continue
            m = re.match(r'^(\d{13,19})\|(\d{2})/(\d{2})\|(\d{3,4})\|', line)
            if m:
                card, mm, yy, cvv = m.groups()
                if not self.verify_luhn(card):
                    continue
                results.append((card, mm, yy, cvv, "format_slovakia"))
                continue
        return results

    def _parse_structured(self, text: str) -> List[Tuple[str, str, str, str, str]]:
        results = []
        pattern = re.compile(
            r'card_number:\s*(\d{13,19})\s*\n'
            r'(?:[^\n]*\n)*?'
            r'secure_code:\s*(\d{3,4})\s*\n'
            r'(?:[^\n]*\n)*?'
            r'expiration:\s*(\d{2})/(\d{2})',
            re.MULTILINE
        )
        for m in pattern.finditer(text):
            card, cvv, mm, yy = m.groups()
            if not self.verify_luhn(card):
                continue
            results.append((card, mm, yy, cvv, "structured"))
        return results

    def _heuristic_extract(self, text: str, found_cards: Set[str]) -> List[Tuple[str, str, str, str, str]]:
        results = []
        for match in self.pan_regex.finditer(text):
            card = match.group()
            if card in found_cards or not self.verify_luhn(card):
                continue
            p_start = match.start()
            space_start = max(0, p_start - 300)
            space_end = min(len(text), match.end() + 400)
            context = text[space_start:space_end]
            offset = space_start
            best_date = ""
            min_date_score = float('inf')
            for d_match in self.date_strict_regex.finditer(context):
                actual_start = offset + d_match.start()
                dist = abs(actual_start - p_start)
                if dist < min_date_score:
                    min_date_score = dist
                    mm, yy = d_match.group(1), d_match.group(2)
                    best_date = f"{mm}|{yy[-2:]}"
            for d_match in self.date_yyyymm_regex.finditer(context):
                actual_start = offset + d_match.start()
                dist = abs(actual_start - p_start)
                if dist < min_date_score:
                    min_date_score = dist
                    best_date = f"{d_match.group(2)}|{d_match.group(1)[2:]}"
            if not best_date:
                continue
            best_cvv = ""
            min_cvv_score = float('inf')
            for c_match in self.cvv_regex.finditer(context):
                cvv_cand = c_match.group()
                actual_start = offset + c_match.start()
                if cvv_cand in card or cvv_cand in best_date.replace("|", ""):
                    continue
                if cvv_cand in self.year_exclusions:
                    continue
                left_idx = actual_start - 1
                right_idx = actual_start + len(cvv_cand)
                if (left_idx >= 0 and text[left_idx].isdigit()) or \
                   (right_idx < len(text) and text[right_idx].isdigit()):
                    continue
                dist = abs(actual_start - p_start)
                if dist < min_cvv_score:
                    min_cvv_score = dist
                    best_cvv = cvv_cand
            if best_cvv:
                m, y = best_date.split("|")
                results.append((card, m, y, best_cvv, "heuristic"))
        return results

    def extract_highest_precision(self, text: str) -> List[str]:
        all_results = []
        found_cards = set()
        for card, mm, yy, cvv, src in self._parse_line_formats(text):
            if card not in found_cards:
                found_cards.add(card)
                all_results.append((card, mm, yy, cvv, src))
        for card, mm, yy, cvv, src in self._parse_structured(text):
            if card not in found_cards:
                found_cards.add(card)
                all_results.append((card, mm, yy, cvv, src))
        for card, mm, yy, cvv, src in self._heuristic_extract(text, found_cards):
            if card not in found_cards:
                found_cards.add(card)
                all_results.append((card, mm, yy, cvv, src))
        now = datetime.now()
        current_year_short = now.year % 100
        current_month = now.month
        valid_results = []
        for card, mm, yy, cvv, src in all_results:
            exp_month, exp_year = int(mm), int(yy)
            if exp_year < current_year_short or (exp_year == current_year_short and exp_month < current_month):
                continue
            valid_results.append(f"{card}|{mm}|{yy}|{cvv}")
        return sorted(valid_results)

extractor = NonLinearCardExtractor()

@app.post("/v1/ccclean")
async def ccclean(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
    if not text and not file:
        raise HTTPException(status_code=400, detail="Missing text or file input")
    if file:
        file_bytes = await file.read()
        content = file_bytes.decode("utf-8", errors="ignore")
    else:
        content = text
    
    cleaned_cards = extractor.extract_highest_precision(content)
    return {
        "status": "success",
        "count": len(cleaned_cards),
        "data": cleaned_cards
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
