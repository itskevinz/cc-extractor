import os
import re
import time
import json
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Set, Any
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

START_TIME = time.time()


@dataclass
class ExtractedCard:
    """Đại diện cho thẻ đã trích xuất."""
    pan: str
    month: str
    year: str
    cvv: str
    format_type: str
    confidence: float
    raw_line: str
    extra_info: Dict[str, str] = field(default_factory=dict)
    luhn_valid: bool = True
    expiry_status: str = "unknown"
    
    def to_standard(self) -> str:
        """Format chuẩn: PAN|MM|YY|CVV"""
        yy = self.year[-2:] if len(self.year) >= 2 else self.year
        return f"{self.pan}|{self.month.zfill(2)}|{yy}|{self.cvv}"
    
    def to_full(self) -> Dict:
        """Export đầy đủ thông tin."""
        return {
            "pan": self.pan,
            "month": self.month,
            "year": self.year,
            "cvv": self.cvv,
            "standard": self.to_standard(),
            "format_type": self.format_type,
            "confidence": self.confidence,
            "luhn_valid": self.luhn_valid,
            "expiry_status": self.expiry_status
        }


class PatternLearner:
    """Học và tự động tạo patterns mới từ dữ liệu."""
    
    def __init__(self):
        self.known_formats: Dict[str, Dict] = {}  # fingerprint -> pattern
        self.token_patterns: List[Dict] = []
        self.success_history: List[Dict] = []
        
    def create_fingerprint(self, line: str, pan: str) -> str:
        """Tạo fingerprint từ cấu trúc dòng."""
        # Thay PAN bằng placeholder
        masked = line.replace(pan, "{PAN}")
        # Thay các số khác bằng {N}
        masked = re.sub(r'\d{3,4}(?!\d)', '{CVV}', masked)
        masked = re.sub(r'\d{1,2}/\d{2,4}', '{DATE}', masked)
        masked = re.sub(r'\d{6}(?!\d)', '{YYYYMM}', masked)
        # Lấy cấu trúc delimiter
        structure = re.sub(r'[A-Za-z0-9\s]', '', masked[:50])
        return hashlib.md5(structure.encode()).hexdigest()[:8]
    
    def learn_from_success(self, line: str, card: ExtractedCard, 
                          month_pos: int, year_pos: int, cvv_pos: int):
        """Học từ một lần trích xuất thành công."""
        self.success_history.append({
            "line": line[:100],
            "format": card.format_type,
            "positions": {"month": month_pos, "year": year_pos, "cvv": cvv_pos}
        })
        
        # Nếu đủ data, tự động tạo pattern mới
        if len(self.success_history) >= 10:
            self._auto_generate_patterns()
    
    def _auto_generate_patterns(self):
        """Tự động tạo patterns từ history."""
        # Phân tích các format thành công
        format_counter = Counter(h["format"] for h in self.success_history)
        
        # Tìm format mới chưa có trong known_formats
        for fmt, count in format_counter.most_common():
            if fmt not in self.known_formats and count >= 3:
                # Tạo pattern template
                self.known_formats[fmt] = {
                    "count": count,
                    "auto_generated": True,
                    "confidence_boost": min(0.05 * count, 0.2)
                }
        
        # Giữ history manageable
        if len(self.success_history) > 1000:
            self.success_history = self.success_history[-500:]


class SelfUpgradingExtractor:
    """
    Trích xuất CC với khả năng tự học và tự nâng cấp (Super Algorithm).
    """
    
    def __init__(self, strict_expiry: bool = False, min_confidence: float = 0.0):
        """
        Args:
            strict_expiry: True = chỉ lấy thẻ chưa hết hạn, False = lấy tất cả
            min_confidence: Ngưỡng confidence tối thiểu
        """
        self.current_date = datetime.now()
        self.current_year = self.current_date.year
        self.current_month = self.current_date.month
        self.strict_expiry = strict_expiry
        self.min_confidence = min_confidence
        
        # Pattern learner
        self.learner = PatternLearner()
        
        # Base patterns với metadata đầy đủ
        self.patterns = self._init_patterns()
        
        # Performance tracking
        self.pattern_stats = defaultdict(lambda: {
            'hits': 0, 'fails': 0, 'confidence_sum': 0.0
        })
        
        # Cache kết quả
        self._cache: Dict[str, List[ExtractedCard]] = {}
        
    def _init_patterns(self) -> List[Dict]:
        """Khởi tạo patterns cơ bản - SIÊU BỘ PATTERNS."""
        patterns = []
        
        # === PIPE FORMATS ===
        
        # 1. PAN|MM|YYYY|CVV (classic)
        patterns.append({
            'id': 'pipe_mm_yyyy_cvv',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{1,2})\|(\d{4})\|(\d{3,4})(?!\d)'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yyyy',
            'confidence': 0.95
        })
        
        # 2. PAN|YYYYMM|CVV (combined year-month)
        patterns.append({
            'id': 'pipe_yyyymm_cvv',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{6})\|(\d{3,4})(?!\d)'),
            'groups': {'pan': 1, 'month': 2, 'year': 2, 'cvv': 3},
            'date_parser': 'yyyymm',
            'confidence': 0.92
        })
        
        # 3. PAN|MM|YY|CVV (2-digit year)
        patterns.append({
            'id': 'pipe_mm_yy_cvv',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{1,2})\|(\d{2})\|(\d{3,4})(?!\d)'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yy',
            'confidence': 0.90
        })
        
        # 4. PAN|YYYY|MM|CVV (year first)
        patterns.append({
            'id': 'pipe_yyyy_mm_cvv',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{4})\|(\d{1,2})\|(\d{3,4})(?!\d)'),
            'groups': {'pan': 1, 'year': 2, 'month': 3, 'cvv': 4},
            'date_parser': 'yyyy_mm',
            'confidence': 0.90
        })
        
        # === SLASH DATE FORMATS ===
        
        # 5. PAN|MM/YY|CVV
        patterns.append({
            'id': 'pipe_slash_mm_yy',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{2})/(\d{2})\|(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yy',
            'confidence': 0.88
        })
        
        # 6. PAN|MM/YYYY|CVV
        patterns.append({
            'id': 'pipe_slash_mm_yyyy',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{2})/(\d{4})\|(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yyyy',
            'confidence': 0.90
        })
        
        # === SPACE SEPARATED ===
        
        # 7. PAN Name YYYYMM CVV (space format)
        patterns.append({
            'id': 'space_yyyymm',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\s+\S+\s+(\d{6})\s+(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 2, 'cvv': 3},
            'date_parser': 'yyyymm',
            'confidence': 0.85
        })
        
        # 8. PAN MM/YY CVV (space with slash)
        patterns.append({
            'id': 'space_slash',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\s+(\d{2})/(\d{2,4})\s+(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.87
        })
        
        # === DOUBLE COLON ===
        
        # 9. PAN::MM::YYYY::CVV
        patterns.append({
            'id': 'double_colon',
            'regex': re.compile(r'(?<!\d)(\d{13,19})::(\d{1,2})::(\d{2,4})::(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.88
        })
        
        # === DASH/UNDERSCORE ===
        
        # 10. PAN----MM----YYYY----CVV
        patterns.append({
            'id': 'dash_sep',
            'regex': re.compile(r'(?<!\d)(\d{13,19})----(\d{1,2})----(\d{2,4})----(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.85
        })
        
        # === JSON/DICT LIKE ===
        
        # 11. 'card_num': '...', 'expiry_date': '...', 'cvv': '...'
        patterns.append({
            'id': 'json_like',
            'regex': re.compile(r"'card_num':\s*'(\d{13,19})',.*?'expiry_date':\s*'(\d{2})(\d{2,4})',.*?'cvv':\s*'(\d{3,4})'"),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.88
        })
        
        # === MULTILINE FORMATS ===
        
        # 12. PAN\nMM/YY\nCVV
        patterns.append({
            'id': 'multiline',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\n(\d{2})/(\d{2,4})\n(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.85
        })
        
        # === CC: PREFIX ===
        
        # 13. CC: PAN|MM|YYYY|CVV
        patterns.append({
            'id': 'cc_prefix',
            'regex': re.compile(r'CC:\s*(\d{13,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'auto',
            'confidence': 0.88
        })
        
        # === LABELLED FORMATS ===
        
        # 14. Number: PAN Expiry: MM/YYYY CVV: CVV
        patterns.append({
            'id': 'labelled',
            'regex': re.compile(r'Number:\s*(\d{13,19})\s*Expiry:\s*(\d{2})/(\d{2,4})\s*CVV:\s*(\d{3})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yyyy',
            'confidence': 0.90
        })
        
        # === SPECIAL: Full dump with many fields ===
        
        # 15. PAN|MM/YY|CVV|Name|Address|... (full info dumps)
        patterns.append({
            'id': 'full_dump_pipe',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{2})/(\d{2})\|(\d{3,4})\|[^|]{3,}'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yy',
            'confidence': 0.85
        })
        
        # 16. PAN|MM/YYYY|CVV|Name|Address|... (full dump with 4-digit year)
        patterns.append({
            'id': 'full_dump_pipe_yyyy',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{2})/(\d{4})\|(\d{3,4})\|[^|]{3,}'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yyyy',
            'confidence': 0.87
        })
        
        # === PIPE WITH METADATA (PAN|MM|YYYY|CVV|BIN|TYPE|LEVEL|BANK) ===
        
        # 17. Pipe with metadata (8 fields)
        patterns.append({
            'id': 'pipe_with_metadata',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{1,2})\|(\d{4})\|(\d{3,4})\|BIN[：:](\d{6})'),
            'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
            'date_parser': 'mm_yyyy',
            'confidence': 0.92,
            'extra': {'bin': 5}
        })
        
        # 18. Pipe with metadata v2 (PAN|YYYYMM|CVV|TYPE|BANK|...)
        patterns.append({
            'id': 'pipe_yyyymm_metadata',
            'regex': re.compile(r'(?<!\d)(\d{13,19})\|(\d{6})\|(\d{3,4})\|[A-Z]'),
            'groups': {'pan': 1, 'month': 2, 'year': 2, 'cvv': 3},
            'date_parser': 'yyyymm',
            'confidence': 0.90
        })
        
        return patterns
    
    def _parse_date(self, raw_month: str, raw_year: str, 
                    parser_type: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse date tùy theo loại parser."""
        try:
            if parser_type == 'yyyymm':
                # raw_month chứa YYYYMM
                if len(raw_month) == 6:
                    year = raw_month[:4]
                    month = raw_month[4:]
                    return month, year
                return None, None
            
            elif parser_type == 'mm_yyyy':
                # Standard
                month = raw_month.zfill(2)
                year = raw_year
                return month, year
            
            elif parser_type == 'mm_yy':
                # 2-digit year
                month = raw_month.zfill(2)
                y = int(raw_year)
                if y < 50:
                    year = f"20{raw_year}"
                else:
                    year = f"19{raw_year}"
                return month, year
            
            elif parser_type == 'yyyy_mm':
                # Year first
                year = raw_year
                month = raw_month.zfill(2)
                return month, year
            
            elif parser_type == 'auto':
                # Auto detect
                if len(raw_year) == 4 and 2000 <= int(raw_year) <= 2099:
                    return raw_month.zfill(2), raw_year
                elif len(raw_year) == 2:
                    y = int(raw_year)
                    if y < 50:
                        return raw_month.zfill(2), f"20{raw_year}"
                    else:
                        return raw_month.zfill(2), f"19{raw_year}"
                elif len(raw_month) == 6 and 2000 <= int(raw_month[:4]) <= 2099:
                    # YYYYMM in month field
                    return raw_month[4:], raw_month[:4]
            
            return None, None
            
        except Exception:
            return None, None
    
    def _luhn_check(self, card_number: str) -> bool:
        """Kiểm tra Luhn algorithm."""
        try:
            digits = [int(d) for d in card_number if d.isdigit()]
            if len(digits) < 13:
                return False
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(divmod(d * 2, 10))
            return checksum % 10 == 0
        except Exception:
            return False
    
    def _check_expiry(self, month: str, year: str) -> Tuple[bool, str]:
        """
        Kiểm tra hạn thẻ.
        Returns: (is_valid, status)
        """
        try:
            m = int(month)
            y = int(year)
            
            if not (1 <= m <= 12):
                return False, "invalid_month"
            
            # So sánh với current date
            if y < self.current_year:
                return False, "expired"
            if y == self.current_year and m < self.current_month:
                return False, "expired"
            
            # Check quá xa trong tương lai (> 20 năm)
            if y > self.current_year + 20:
                return False, "future_invalid"
            
            return True, "valid"
            
        except Exception:
            return False, "parse_error"
    
    def _adaptive_fallback(self, line: str) -> Optional[ExtractedCard]:
        """
        Fallback thông minh khi không match pattern nào.
        Sử dụng heuristic và token analysis.
        """
        # Tìm tất cả số trong dòng
        tokens = re.findall(r'\d+', line)
        if not tokens:
            return None
        
        # Tìm PAN (13-19 digits, bắt đầu bằng 3,4,5,6)
        pan = None
        pan_idx = -1
        
        for i, t in enumerate(tokens):
            if 13 <= len(t) <= 19 and t[0] in '3456':
                # Quick Luhn pre-check
                if self._luhn_check(t):
                    pan = t
                    pan_idx = i
                    break
        
        if not pan:
            return None
        
        # Tìm CVV (3-4 digits, không phải PAN, thường sau PAN)
        cvv = None
        for t in tokens[pan_idx+1:]:
            if len(t) in [3, 4] and t != pan:
                # CVV thường không bắt đầu bằng 0 trong nhiều trường hợp
                cvv = t
                break
        
        # Nếu không tìm thấy sau PAN, thử tìm trước
        if not cvv:
            for t in tokens[:pan_idx]:
                if len(t) in [3, 4] and t != pan:
                    cvv = t
                    break
        
        # Tìm date
        month, year = None, None
        
        # 1. Thử tìm YYYYMM (6 digits)
        for t in tokens:
            if len(t) == 6:
                y, m = t[:4], t[4:]
                if 1 <= int(m) <= 12 and 2000 <= int(y) <= 2099:
                    month, year = m, y
                    break
        
        # 2. Thử tìm MM/YY hoặc MM/YYYY trong text
        if not month:
            date_match = re.search(r'(\d{2})/(\d{2,4})', line)
            if date_match:
                month = date_match.group(1)
                year_raw = date_match.group(2)
                if len(year_raw) == 2:
                    year = f"20{year_raw}"
                else:
                    year = year_raw
        
        # 3. Thử tìm từng token
        if not month:
            year_candidates = []
            month_candidates = []
            
            for t in tokens:
                if t == pan or t == cvv:
                    continue
                if len(t) == 4 and 2020 <= int(t) <= 2099:
                    year_candidates.append(t)
                elif len(t) in [1, 2] and 1 <= int(t) <= 12:
                    month_candidates.append(t)
            
            if year_candidates:
                year = year_candidates[0]
            if month_candidates:
                month = month_candidates[0]
        
        # 4. Thử tìm 4-digit year trong các số còn lại
        if not year:
            for t in tokens:
                if len(t) == 4 and 2020 <= int(t) <= 2099:
                    if t != pan and t != cvv:
                        year = t
                        break
        
        # Validate
        if pan and month and year and cvv:
            luhn = self._luhn_check(pan)
            expiry_valid, expiry_status = self._check_expiry(month, year)
            
            # Nếu strict_expiry, bỏ qua thẻ hết hạn
            if self.strict_expiry and not expiry_valid:
                return None
            
            return ExtractedCard(
                pan=pan, month=month.zfill(2), year=year,
                cvv=cvv, format_type='adaptive_fallback',
                confidence=0.55, raw_line=line[:100],
                luhn_valid=luhn, expiry_status=expiry_status
            )
        
        return None
    
    def _extract_from_line(self, line: str) -> Optional[ExtractedCard]:
        """Trích xuất từ 1 dòng với tất cả patterns."""
        
        for pattern in self.patterns:
            for match in pattern['regex'].finditer(line):
                g = pattern['groups']
                
                try:
                    pan = match.group(g['pan'])
                    raw_month = match.group(g['month'])
                    raw_year = match.group(g['year'])
                    cvv = match.group(g['cvv'])
                except (IndexError, AttributeError):
                    continue
                
                # Parse date
                month, year = self._parse_date(raw_month, raw_year, 
                                               pattern.get('date_parser', 'auto'))
                
                if not month or not year:
                    continue
                
                # Validate Luhn
                luhn = self._luhn_check(pan)
                if not luhn:
                    self.pattern_stats[pattern['id']]['fails'] += 1
                    continue
                
                # Check expiry
                expiry_valid, expiry_status = self._check_expiry(month, year)
                
                # Nếu strict_expiry và hết hạn, skip
                if self.strict_expiry and not expiry_valid:
                    self.pattern_stats[pattern['id']]['fails'] += 1
                    continue
                
                # Success!
                self.pattern_stats[pattern['id']]['hits'] += 1
                self.pattern_stats[pattern['id']]['confidence_sum'] += pattern['confidence']
                
                # Học từ thành công
                self.learner.learn_from_success(
                    line, None,
                    match.start(g['month']), match.start(g['year']),
                    match.start(g['cvv'])
                )
                
                # Extra info
                extra = {}
                if 'extra' in pattern:
                    for key, group_idx in pattern['extra'].items():
                        try:
                            extra[key] = match.group(group_idx)
                        except:
                            pass
                
                return ExtractedCard(
                    pan=pan, month=month, year=year,
                    cvv=cvv, format_type=pattern['id'],
                    confidence=pattern['confidence'],
                    raw_line=line[:100],
                    extra_info=extra,
                    luhn_valid=luhn,
                    expiry_status=expiry_status
                )
        
        # Fallback
        return self._adaptive_fallback(line)
    
    def extract(self, raw_text: str, use_cache: bool = True) -> List[ExtractedCard]:
        """
        Trích xuất tất cả valid CCs từ text.
        
        Args:
            raw_text: Text cần parse
            use_cache: Có sử dụng cache không
        """
        # Check cache
        if use_cache:
            cache_key = hashlib.md5(raw_text[:1000].encode()).hexdigest()
            if cache_key in self._cache:
                return self._cache[cache_key]
        
        results = []
        seen = set()
        
        # Xử lý cả multiline formats
        # Thay \r\n bằng \n, giữ lại \n\n cho multiline
        normalized = raw_text.replace('\r\n', '\n').replace('\r', '\n')
        
        # Thử parse theo blocks (cho multiline formats)
        blocks = normalized.split('\n\n')
        
        for block in blocks:
            lines = block.split('\n')
            
            # Thử multiline trước
            if len(lines) >= 3:
                multiline_text = '\n'.join(lines[:3])
                card = self._extract_from_line(multiline_text)
                if card:
                    key = card.to_standard()
                    if key not in seen:
                        seen.add(key)
                        results.append(card)
            
            # Sau đó parse từng dòng
            for line in lines:
                line = line.strip()
                if not line or len(line) < 20:
                    continue
                
                card = self._extract_from_line(line)
                if card:
                    key = card.to_standard()
                    if key not in seen:
                        seen.add(key)
                        results.append(card)
        
        # Sort
        results.sort(key=lambda x: x.to_standard())
        
        # Cache
        if use_cache:
            self._cache[cache_key] = results
        
        return results
    
    def get_stats(self) -> Dict:
        """Thống kê performance đầy đủ."""
        stats = {}
        for pid, data in self.pattern_stats.items():
            total = data['hits'] + data['fails']
            stats[pid] = {
                'hits': data['hits'],
                'fails': data['fails'],
                'total': total,
                'success_rate': round(data['hits'] / total, 3) if total > 0 else 0,
                'avg_confidence': round(data['confidence_sum'] / data['hits'], 3) 
                                  if data['hits'] > 0 else 0
            }
        return stats
    
    def get_learned_formats(self) -> Dict:
        """Lấy các formats đã học được."""
        return {
            'known_fingerprints': len(self.learner.known_formats),
            'success_history_size': len(self.learner.success_history),
            'auto_patterns': self.learner.known_formats
        }
    
    def upgrade_from_feedback(self, raw_line: str, correct_pan: str, 
                             correct_month: str, correct_year: str, 
                             correct_cvv: str):
        """
        Tự nâng cấp từ feedback người dùng.
        Đây là SIÊU THUẬT TOÁN - học từ corrections.
        """
        # Tìm vị trí của các trường đúng trong dòng
        pan_pos = raw_line.find(correct_pan)
        
        # Phân tích cấu trúc xung quanh
        before_pan = raw_line[:pan_pos] if pan_pos > 0 else ""
        after_pan = raw_line[pan_pos + len(correct_pan):] if pan_pos >= 0 else ""
        
        # Tìm delimiter pattern
        delim_pattern = re.findall(r'[^A-Za-z0-9\s]{1,3}', after_pan[:10])
        
        # Tạo regex mới nếu cấu trúc mới
        if delim_pattern:
            delim = re.escape(delim_pattern[0])
            # Tạo pattern template
            new_pattern = {
                'id': f'learned_{hashlib.md5(delim.encode()).hexdigest()[:6]}',
                'regex': re.compile(
                    rf'(\d{{13,19}}){delim}(\d{{1,2}}){delim}(\d{{2,4}}){delim}(\d{{3,4}})'
                ),
                'groups': {'pan': 1, 'month': 2, 'year': 3, 'cvv': 4},
                'date_parser': 'auto',
                'confidence': 0.75,  # Start lower, will increase with success
                'learned': True
            }
            
            # Kiểm tra xem đã có chưa
            existing = [p for p in self.patterns if p['id'] == new_pattern['id']]
            if not existing:
                self.patterns.append(new_pattern)
                return True
        
        return False
    
    def export_patterns(self) -> str:
        """Export patterns để lưu trữ."""
        exportable = []
        for p in self.patterns:
            ep = {
                'id': p['id'],
                'pattern': p['regex'].pattern,
                'groups': p['groups'],
                'date_parser': p.get('date_parser', 'auto'),
                'confidence': p['confidence'],
                'learned': p.get('learned', False)
            }
            exportable.append(ep)
        return json.dumps(exportable, indent=2)
    
    def import_patterns(self, json_data: str):
        """Import patterns từ JSON."""
        data = json.loads(json_data)
        for p in data:
            new_pattern = {
                'id': p['id'],
                'regex': re.compile(p['pattern']),
                'groups': p['groups'],
                'date_parser': p.get('date_parser', 'auto'),
                'confidence': p['confidence'],
                'learned': p.get('learned', True)
            }
            # Tránh duplicate
            existing = [x for x in self.patterns if x['id'] == p['id']]
            if not existing:
                self.patterns.append(new_pattern)


# === INTEGRATION VỚI FASTAPI ===

extractor = SelfUpgradingExtractor(strict_expiry=False, min_confidence=0.0)

def process_text_cleaning(text: str, strict_expiry: bool = False) -> dict:
    """Xử lý text và trả về kết quả."""
    if strict_expiry:
        temp_extractor = SelfUpgradingExtractor(strict_expiry=True)
        results = temp_extractor.extract(text)
    else:
        results = extractor.extract(text)
    
    valid_cards = [r.to_standard() for r in results]
    full_details = [r.to_full() for r in results]
    
    return {
        "success": True,
        "count": len(results),
        "processing_time_ms": int((time.time() - time.time()) * 1000) + 1,
        "data": valid_cards,
        "details": full_details,
        "stats": extractor.get_stats(),
        "learned": extractor.get_learned_formats()
    }

@app.get('/health')
def health_check():
    return {
        "status": "ok", 
        "uptime": int(time.time() - START_TIME),
        "patterns_loaded": len(extractor.patterns),
        "learned_formats": len(extractor.learner.known_formats)
    }

@app.post('/v1/ccclean')
@limiter.limit("200/minute")
async def ccclean(
    request: Request,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    strict_expiry: bool = Form(False),
    include_details: bool = Form(False)
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
            try:
                data = json.loads(raw_text)
                if isinstance(data, dict):
                    raw_text = data.get("text", raw_text)
            except Exception:
                pass

    if not raw_text:
        raise HTTPException(status_code=400, detail="Missing or invalid text body")

    result = process_text_cleaning(raw_text, strict_expiry)
    
    if not include_details:
        result.pop("details", None)
    
    return result

@app.post('/v1/feedback')
@limiter.limit("100/minute")
async def feedback(
    request: Request,
    raw_line: str = Form(...),
    correct_pan: str = Form(...),
    correct_month: str = Form(...),
    correct_year: str = Form(...),
    correct_cvv: str = Form(...)
):
    """
    Nhận feedback để tự nâng cấp thuật toán.
    Đây là SIÊU THUẬT TOÁN - học từ corrections.
    """
    upgraded = extractor.upgrade_from_feedback(
        raw_line, correct_pan, correct_month, correct_year, correct_cvv
    )
    
    return {
        "success": True,
        "upgraded": upgraded,
        "total_patterns": len(extractor.patterns),
        "message": "Pattern learned and added to extractor" if upgraded else "Pattern already known"
    }

@app.get('/v1/patterns')
def get_patterns():
    """Xem tất cả patterns hiện tại."""
    return {
        "patterns": [
            {
                "id": p["id"],
                "confidence": p["confidence"],
                "learned": p.get("learned", False)
            }
            for p in extractor.patterns
        ],
        "stats": extractor.get_stats(),
        "learned": extractor.get_learned_formats()
    }

@app.post('/v1/export')
def export_patterns():
    """Export patterns để backup/transfer."""
    return {
        "patterns_json": extractor.export_patterns()
    }

@app.post('/v1/import')
async def import_patterns(request: Request):
    """Import patterns từ backup."""
    body = await request.body()
    try:
        extractor.import_patterns(body.decode())
        return {"success": True, "total_patterns": len(extractor.patterns)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid pattern data: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)
