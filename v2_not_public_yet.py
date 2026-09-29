# --- app.py ---

from __future__ import annotations

import html
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


MAX_BODY_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 10_000_000
FILE_CHUNK_BYTES = 1024 * 1024
MAX_CANDIDATE_DIGITS = 19
MIN_PAN_DIGITS = 13
MAX_SEPARATOR_GAP = 4
CONTEXT_WINDOW = 180
MIN_ACCEPT_SCORE = 70

START_TIME = time.monotonic()

limiter = Limiter(key_func=get_remote_address)

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)


@dataclass(slots=True, frozen=True)
class Span:
    start: int
    end: int


@dataclass(slots=True)
class PanCandidate:
    digits: str
    span: Span
    score: int = 0
    network: str = "unknown"


@dataclass(slots=True)
class RelatedEntity:
    kind: str
    span: Span
    score: int


class AdaptivePaymentDetector:
    _ZERO_WIDTH = {
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u061c",
        "\ufeff",
        "\u2060",
    }

    _PANISH_LABELS = (
        "card",
        "credit",
        "debit",
        "account",
        "acct",
        "pan",
        "cc",
        "number",
        "numero",
        "num",
        "payment",
        "visa",
        "mastercard",
        "amex",
        "discover",
        "jcb",
        "unionpay",
    )

    _EXPIRY_LABELS = (
        "exp",
        "expiry",
        "expiration",
        "valid",
        "validthru",
        "validuntil",
        "expires",
        "expirationdate",
    )

    _SECURITY_LABELS = (
        "cvv",
        "cvc",
        "cvv2",
        "cvc2",
        "securitycode",
        "security",
        "verificationcode",
        "verification",
        "cid",
    )

    _EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+", re.IGNORECASE)
    _URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

    def __init__(self) -> None:
        self._digit_translate = str.maketrans(
            "０１２３４５６７８９",
            "0123456789",
        )

    @staticmethod
    def _is_digit(ch: str) -> bool:
        return ch.isdecimal()

    @staticmethod
    def _is_soft_separator(ch: str) -> bool:
        if ch.isspace():
            return True

        category = unicodedata.category(ch)

        return category.startswith("P") or category in {
            "Sm",
            "Sk",
            "Sc",
        }

    @staticmethod
    def _compact(value: str) -> str:
        return "".join(
            ch for ch in value.casefold()
            if ch.isalnum()
        )

    def normalize(self, text: str) -> str:
        if not text:
            return ""

        text = unicodedata.normalize("NFKC", text)

        text = "".join(
            ch for ch in text
            if ch not in self._ZERO_WIDTH
        )

        text = html.unescape(text)
        text = text.translate(self._digit_translate)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        return text

    @staticmethod
    def luhn_valid(digits: str) -> bool:
        if not MIN_PAN_DIGITS <= len(digits) <= MAX_CANDIDATE_DIGITS:
            return False

        total = 0
        parity = len(digits) & 1

        for index, char in enumerate(digits):
            digit = ord(char) - 48

            if (index & 1) == parity:
                digit *= 2
                if digit > 9:
                    digit -= 9

            total += digit

        return total % 10 == 0

    @staticmethod
    def repeated_digits(digits: str) -> bool:
        return len(set(digits)) == 1

    @staticmethod
    def classify_network(digits: str) -> str:
        if digits.startswith("4") and len(digits) in (13, 16, 19):
            return "visa"

        if len(digits) == 16:
            prefix2 = int(digits[:2])
            prefix4 = int(digits[:4])

            if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
                return "mastercard"

        if len(digits) in (15,):
            if digits.startswith(("34", "37")):
                return "amex"

        if len(digits) in (16, 19):
            if digits.startswith("6011"):
                return "discover"

        if 3528 <= int(digits[:4]) <= 3589 if len(digits) >= 4 else False:
            return "jcb"

        if len(digits) >= 6:
            prefix6 = int(digits[:6])

            if (
                622126 <= prefix6 <= 622925
                or 624000 <= prefix6 <= 626999
                or 628200 <= prefix6 <= 628899
            ):
                return "unionpay"

        return "unknown"

    def _collect_numeric_candidates(
        self,
        text: str,
    ) -> list[PanCandidate]:
        candidates: list[PanCandidate] = []
        seen: set[tuple[str, int, int]] = set()

        n = len(text)
        i = 0

        while i < n:
            if not self._is_digit(text[i]):
                i += 1
                continue

            start = i
            digits: list[str] = []
            end = i
            separators = 0
            last_digit = i

            while i < n and len(digits) <= MAX_CANDIDATE_DIGITS:
                if self._is_digit(text[i]):
                    digits.append(text[i])
                    last_digit = i
                    end = i + 1
                    i += 1
                    continue

                if (
                    self._is_soft_separator(text[i])
                    and i + 1 < n
                    and self._is_digit(text[i + 1])
                    and i - last_digit <= MAX_SEPARATOR_GAP
                ):
                    separators += 1
                    i += 1
                    continue

                break

            digit_string = "".join(digits)

            if (
                MIN_PAN_DIGITS <= len(digit_string) <= MAX_CANDIDATE_DIGITS
                and self.luhn_valid(digit_string)
                and not self.repeated_digits(digit_string)
            ):
                key = (digit_string, start, end)

                if key not in seen:
                    seen.add(key)

                    candidates.append(
                        PanCandidate(
                            digits=digit_string,
                            span=Span(start, end),
                            network=self.classify_network(digit_string),
                        )
                    )

            i = max(i, last_digit + 1)

        return candidates

    def _context(
        self,
        text: str,
        span: Span,
    ) -> tuple[str, str]:
        left = text[max(0, span.start - CONTEXT_WINDOW):span.start]
        right = text[span.end:min(len(text), span.end + CONTEXT_WINDOW)]
        return left.casefold(), right.casefold()

    def _label_score(
        self,
        context: str,
        labels: Iterable[str],
    ) -> int:
        compact = self._compact(context)
        score = 0

        for label in labels:
            normalized = self._compact(label)

            if normalized in compact:
                score += 18

        return min(score, 54)

    def _numeric_shape_score(self, candidate: PanCandidate) -> int:
        digits = candidate.digits
        score = 0

        if len(digits) in (13, 15, 16, 18, 19):
            score += 12

        if candidate.network != "unknown":
            score += 18

        if not self.repeated_digits(digits):
            score += 8

        return score

    def _separator_score(
        self,
        text: str,
        span: Span,
    ) -> int:
        fragment = text[span.start:span.end]

        if " " in fragment or "\n" in fragment:
            return 6

        if any(ch in fragment for ch in "-_/\\.:|"):
            return 8

        return 2

    def _position_score(
        self,
        text: str,
        span: Span,
    ) -> int:
        line_start = text.rfind("\n", 0, span.start) + 1
        line_end = text.find("\n", span.end)

        if line_end == -1:
            line_end = len(text)

        left = text[line_start:span.start].strip().casefold()
        right = text[span.end:line_end].strip().casefold()

        score = 0

        for label in self._PANISH_LABELS:
            compact_label = self._compact(label)

            if compact_label in self._compact(left[-80:]):
                score += 10

            if compact_label in self._compact(right[:80]):
                score += 10

        return min(score, 40)

    def _score_pan(
        self,
        text: str,
        candidate: PanCandidate,
    ) -> int:
        left, right = self._context(text, candidate.span)

        context = f"{left} {right}"

        score = 10
        score += self._numeric_shape_score(candidate)
        score += self._separator_score(text, candidate.span)
        score += self._label_score(context, self._PANISH_LABELS)
        score += self._position_score(text, candidate.span)

        if self._EMAIL_RE.search(text[
            max(0, candidate.span.start - 40):
            min(len(text), candidate.span.end + 40)
        ]):
            score -= 25

        if self._URL_RE.search(text[
            max(0, candidate.span.start - 40):
            min(len(text), candidate.span.end + 40)
        ]):
            score -= 20

        return max(0, min(score, 100))

    def _resolve_candidates(
        self,
        text: str,
        candidates: list[PanCandidate],
    ) -> list[PanCandidate]:
        for candidate in candidates:
            candidate.score = self._score_pan(text, candidate)

        candidates.sort(
            key=lambda item: (
                item.score,
                len(item.digits),
                -item.span.start,
            ),
            reverse=True,
        )

        selected: list[PanCandidate] = []
        occupied: list[Span] = []

        for candidate in candidates:
            if candidate.score < MIN_ACCEPT_SCORE:
                continue

            overlaps = False

            for span in occupied:
                if (
                    candidate.span.start < span.end
                    and span.start < candidate.span.end
                ):
                    overlaps = True
                    break

            if overlaps:
                continue

            selected.append(candidate)
            occupied.append(candidate.span)

        selected.sort(key=lambda item: item.span.start)

        return selected

    @staticmethod
    def _parse_date_pair(
        left: str,
        right: str,
    ) -> bool:
        if not left.isdigit() or not right.isdigit():
            return False

        a = int(left)
        b = int(right)

        if len(left) == 4 and 1 <= b <= 12:
            return 2020 <= a <= 2100

        if len(right) == 4 and 1 <= a <= 12:
            return 2020 <= b <= 2100

        if len(left) <= 2 and len(right) <= 2:
            return (
                1 <= a <= 12 and b >= 20
            ) or (
                1 <= b <= 12 and a >= 20
            )

        return False

    def _find_expiry(
        self,
        text: str,
        pan: PanCandidate,
    ) -> Optional[RelatedEntity]:
        start = max(0, pan.span.start - CONTEXT_WINDOW)
        end = min(len(text), pan.span.end + CONTEXT_WINDOW)

        context = text[start:end]

        for match in re.finditer(
            r"(?<!\d)(\d{1,4})\s*[/\\_.:\-~]\s*(\d{1,4})(?!\d)",
            context,
        ):
            a, b = match.groups()

            if not self._parse_date_pair(a, b):
                continue

            absolute_start = start + match.start()
            absolute_end = start + match.end()

            around = context[
                max(0, match.start() - 60):
                min(len(context), match.end() + 60)
            ]

            score = 35 + self._label_score(
                around,
                self._EXPIRY_LABELS,
            )

            return RelatedEntity(
                kind="expiry",
                span=Span(
                    absolute_start,
                    absolute_end,
                ),
                score=min(score, 100),
            )

        for match in re.finditer(
            r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)",
            context,
        ):
            absolute_start = start + match.start()
            absolute_end = start + match.end()

            return RelatedEntity(
                kind="expiry",
                span=Span(
                    absolute_start,
                    absolute_end,
                ),
                score=55,
            )

        return None

    def _find_cvv(
        self,
        text: str,
        pan: PanCandidate,
    ) -> Optional[RelatedEntity]:
        start = max(0, pan.span.start - CONTEXT_WINDOW)
        end = min(len(text), pan.span.end + CONTEXT_WINDOW)

        context = text[start:end]

        labeled = re.finditer(
            r"(?<!\w)"
            r"(?:cvv2|cvc2|cvv|cvc|cid|"
            r"security[\s_-]*code|"
            r"verification[\s_-]*code)"
            r"\s*[:=|>\-~./\\]*\s*"
            r"(\d{3,4})"
            r"(?!\d)",
            context,
            re.IGNORECASE,
        )

        best: Optional[RelatedEntity] = None

        for match in labeled:
            value_start, value_end = match.span(1)

            absolute_start = start + value_start
            absolute_end = start + value_end

            label_score = self._label_score(
                match.group(0),
                self._SECURITY_LABELS,
            )

            score = 70 + label_score

            candidate = RelatedEntity(
                kind="cvv",
                span=Span(
                    absolute_start,
                    absolute_end,
                ),
                score=min(score, 100),
            )

            if best is None or candidate.score > best.score:
                best = candidate

        return best

    @staticmethod
    def _overlap(a: Span, b: Span) -> bool:
        return (
            a.start < b.end
            and b.start < a.end
        )

    def _apply_redactions(
        self,
        text: str,
        spans: list[tuple[Span, str]],
    ) -> str:
        selected: list[tuple[Span, str]] = []

        for span, replacement in sorted(
            spans,
            key=lambda item: (
                item[0].start,
                -(item[0].end - item[0].start),
            ),
        ):
            if any(
                self._overlap(span, existing)
                for existing, _ in selected
            ):
                continue

            selected.append((span, replacement))

        for span, replacement in reversed(selected):
            text = (
                text[:span.start]
                + replacement
                + text[span.end:]
            )

        return text

    def redact(self, raw_text: str) -> dict:
        text = self.normalize(raw_text)

        if not text:
            return {
                "redacted_text": "",
                "detections": 0,
                "pan_count": 0,
                "cvv_count": 0,
                "expiry_count": 0,
                "matches": [],
            }

        candidates = self._collect_numeric_candidates(text)
        accepted = self._resolve_candidates(text, candidates)

        redactions: list[tuple[Span, str]] = []
        matches: list[dict] = []

        cvv_count = 0
        expiry_count = 0

        for pan in accepted:
            redactions.append(
                (
                    pan.span,
                    "[REDACTED_PAN]",
                )
            )

            expiry = self._find_expiry(
                text,
                pan,
            )

            cvv = self._find_cvv(
                text,
                pan,
            )

            if expiry is not None:
                expiry_count += 1

                redactions.append(
                    (
                        expiry.span,
                        "[REDACTED_EXPIRY]",
                    )
                )

            if cvv is not None:
                cvv_count += 1

                redactions.append(
                    (
                        cvv.span,
                        "[REDACTED_CVV]",
                    )
                )

            matches.append(
                {
                    "type": "payment_card",
                    "network": pan.network,
                    "confidence": pan.score,
                    "pan": "[REDACTED]",
                    "expiry_detected": expiry is not None,
                    "cvv_detected": cvv is not None,
                }
            )

        redacted = self._apply_redactions(
            text,
            redactions,
        )

        return {
            "redacted_text": redacted,
            "detections": len(accepted) + cvv_count + expiry_count,
            "pan_count": len(accepted),
            "cvv_count": cvv_count,
            "expiry_count": expiry_count,
            "matches": matches,
        }


class RequestReader:
    @staticmethod
    async def read_stream(
        request: Request,
    ) -> bytes:
        content_length = request.headers.get(
            "content-length"
        )

        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0

            if declared > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Payload Too Large",
                )

        chunks: list[bytes] = []
        total = 0

        async for chunk in request.stream():
            total += len(chunk)

            if total > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Payload Too Large",
                )

            chunks.append(chunk)

        return b"".join(chunks)


detector = AdaptivePaymentDetector()


def process_text_cleaning(text: str) -> dict:
    started = time.perf_counter()

    result = detector.redact(text)

    elapsed_ms = (
        time.perf_counter() - started
    ) * 1000

    return {
        "success": True,
        "count": result["detections"],
        "processing_time_ms": round(
            elapsed_ms,
            3,
        ),
        "data": result["redacted_text"],
        "analysis": {
            "pan_count": result["pan_count"],
            "cvv_count": result["cvv_count"],
            "expiry_count": result["expiry_count"],
            "matches": result["matches"],
        },
    }


async def read_upload(
    upload: UploadFile,
) -> str:
    chunks: list[bytes] = []
    total = 0

    try:
        while True:
            chunk = await upload.read(
                FILE_CHUNK_BYTES
            )

            if not chunk:
                break

            total += len(chunk)

            if total > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Payload Too Large",
                )

            chunks.append(chunk)

    finally:
        await upload.close()

    return b"".join(chunks).decode(
        "utf-8",
        errors="replace",
    )


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "uptime": int(
            time.monotonic() - START_TIME
        ),
    }


@app.post("/v1/ccclean")
@limiter.limit("200/minute")
async def ccclean(
    request: Request,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
):
    raw_text = ""

    if file is not None:
        raw_text = await read_upload(file)

    elif text is not None:
        if len(text.encode("utf-8")) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Payload Too Large",
            )

        raw_text = text

    else:
        content_type = request.headers.get(
            "content-type",
            "",
        ).lower()

        raw_bytes = await RequestReader.read_stream(
            request
        )

        raw_text = raw_bytes.decode(
            "utf-8",
            errors="replace",
        )

        if "application/json" in content_type:
            try:
                payload = json.loads(raw_text)

                if isinstance(payload, dict):
                    candidate = payload.get("text")

                    if isinstance(candidate, str):
                        raw_text = candidate

            except json.JSONDecodeError:
                pass

    if not raw_text:
        raise HTTPException(
            status_code=400,
            detail="Missing or invalid text body",
        )

    if len(raw_text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail="Text Too Large",
        )

    return process_text_cleaning(raw_text)


@app.post("/shutdown")
async def shutdown():
    os._exit(0)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=3000,
        reload=False,
        workers=1,
    )
