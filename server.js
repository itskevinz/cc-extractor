const express = require('express');
const rateLimit = require('express-rate-limit');
const os = require('os');

const app = express();

// ─── Trust Proxy (BẮT BUỘC cho Render) ──────────────────────
app.set('trust proxy', 1);

app.use(express.json({ limit: '50mb' }));
app.use(express.text({ limit: '50mb' }));

// ─── Rate Limit ───────────────────────────────────────────────
const limiter = rateLimit({
    windowMs: 60 * 1000,
    max: 200,
    standardHeaders: true,
    legacyHeaders: false,
    skip: (req) => req.path === '/health',
    validate: { xForwardedForHeader: false }
});

// ─── Luhn Check ───────────────────────────────────────────────
function verifyLuhn(cardNumber) {
    const digits = cardNumber.split('').filter(c => /\d/.test(c)).map(Number);
    if (digits.length < 13 || digits.length > 19) return false;
    let sum = 0;
    let alternate = false;
    for (let i = digits.length - 1; i >= 0; i--) {
        let n = digits[i];
        if (alternate) {
            n *= 2;
            if (n > 9) n -= 9;
        }
        sum += n;
        alternate = !alternate;
    }
    return sum % 10 === 0;
}

// ─── Regex Patterns ───────────────────────────────────────────
const PAN_REGEX = /\b\d{13,19}\b/g;
const CVV_REGEX = /\b\d{3,4}\b/g;
const DATE_STRICT_REGEX = /\b(0[1-9]|1[0-2])[\s\-\/|]?(20\d{2}|\d{2})\b/g;
const DATE_YYYYMM_REGEX = /\b(20\d{2})(0[1-9]|1[0-2])\b/g;
const YEAR_EXCLUSIONS = new Set(Array.from({ length: 20 }, (_, i) => String(2020 + i)));

const LINE_PATTERNS = [
    { regex: /^\d{13,19}\|\d{6}\|\d{3,4}\b/, type: 'format_yyyymm', extract: (m) => {
        const parts = m[0].split('|');
        return [parts[0], parts[1].slice(4, 6), parts[1].slice(2, 4), parts[2]];
    }},
    { regex: /^\d{13,19}\|\d{2}\/\d{2}\|\d{3,4}\b/, type: 'format_mm_slash_yy', extract: (m) => {
        const parts = m[0].split('|');
        const dateParts = parts[1].split('/');
        return [parts[0], dateParts[0], dateParts[1], parts[2]];
    }},
    { regex: /^\d{13,19}~\d{2}\/\d{2}~\d{3,4}\b/, type: 'format_tilde', extract: (m) => {
        const parts = m[0].split('~');
        const dateParts = parts[1].split('/');
        return [parts[0], dateParts[0], dateParts[1], parts[2]];
    }},
    { regex: /^\d{13,19}\|\d{2}\|\d{2}\|\d{3,4}\b/, type: 'format_pipe_mm_yy', extract: (m) => {
        const parts = m[0].split('|');
        return [parts[0], parts[1], parts[2], parts[3]];
    }},
    { regex: /^\d{13,19}\|\d{2}\/\d{2}\|\d{3,4}\|/, type: 'format_slovakia', extract: (m) => {
        const parts = m[0].split('|');
        const dateParts = parts[1].split('/');
        return [parts[0], dateParts[0], dateParts[1], parts[2]];
    }},
];

const SKIP_PREFIXES = ['country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:',
    'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:',
    'order:', 'cc|month|year|cvv'];

// ─── Parse Line Formats ───────────────────────────────────────
function parseLineFormats(text) {
    const results = [];
    const foundCards = new Set();
    const lines = text.split('\n');

    for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.length < 20) continue;

        let skip = false;
        for (const prefix of SKIP_PREFIXES) {
            if (trimmed.startsWith(prefix)) { skip = true; break; }
        }
        if (skip) continue;

        for (const p of LINE_PATTERNS) {
            const m = trimmed.match(p.regex);
            if (m) {
                const [card, mm, yy, cvv] = p.extract(m);
                if (verifyLuhn(card) && !foundCards.has(card)) {
                    foundCards.add(card);
                    results.push({ card, mm, yy, cvv, source: p.type });
                }
                break;
            }
        }
    }
    return { results, foundCards };
}

// ─── Parse Structured Format ──────────────────────────────────
function parseStructured(text) {
    const results = [];
    const foundCards = new Set();
    const blocks = text.split(/\n{2,}/);

    for (const block of blocks) {
        const cardMatch = block.match(/card_number:\s*(\d{13,19})/);
        if (!cardMatch) continue;
        const card = cardMatch[1];

        const cvvMatch = block.match(/secure_code:\s*(\d{3,4})/);
        const expMatch = block.match(/expiration:\s*(\d{2})\/(\d{2})/);

        if (cvvMatch && expMatch && verifyLuhn(card) && !foundCards.has(card)) {
            foundCards.add(card);
            results.push({ card, mm: expMatch[1], yy: expMatch[2], cvv: cvvMatch[1], source: 'structured' });
        }
    }
    return { results, foundCards };
}

// ─── Heuristic Extract ────────────────────────────────────────
function heuristicExtract(text, existingCards) {
    const results = [];
    const foundCards = new Set(existingCards);

    PAN_REGEX.lastIndex = 0;
    let m;

    while ((m = PAN_REGEX.exec(text)) !== null) {
        const card = m[0];
        if (foundCards.has(card) || !verifyLuhn(card)) continue;

        const pStart = m.index;
        const spaceStart = Math.max(0, pStart - 300);
        const spaceEnd = Math.min(text.length, m.index + m[0].length + 400);
        const context = text.slice(spaceStart, spaceEnd);
        const offset = spaceStart;

        let bestDate = '';
        let minDateScore = Infinity;

        let dm;
        DATE_STRICT_REGEX.lastIndex = 0;
        while ((dm = DATE_STRICT_REGEX.exec(context)) !== null) {
            const actualStart = offset + dm.index;
            const dist = Math.abs(actualStart - pStart);
            if (dist < minDateScore) {
                minDateScore = dist;
                const mm = dm[1];
                const yy = dm[2].slice(-2);
                bestDate = `${mm}|${yy}`;
            }
        }

        DATE_YYYYMM_REGEX.lastIndex = 0;
        while ((dm = DATE_YYYYMM_REGEX.exec(context)) !== null) {
            const actualStart = offset + dm.index;
            const dist = Math.abs(actualStart - pStart);
            if (dist < minDateScore) {
                minDateScore = dist;
                bestDate = `${dm[2]}|${dm[1].slice(2)}`;
            }
        }

        if (!bestDate) continue;

        let bestCvv = '';
        let minCvvScore = Infinity;

        CVV_REGEX.lastIndex = 0;
        let cm;
        while ((cm = CVV_REGEX.exec(context)) !== null) {
            const cvvCand = cm[0];
            const actualStart = offset + cm.index;

            if (card.includes(cvvCand) || bestDate.replace('|', '').includes(cvvCand)) continue;
            if (YEAR_EXCLUSIONS.has(cvvCand)) continue;

            const leftIdx = actualStart - 1;
            const rightIdx = actualStart + cvvCand.length;
            if ((leftIdx >= 0 && /\d/.test(text[leftIdx])) || (rightIdx < text.length && /\d/.test(text[rightIdx]))) continue;

            const dist = Math.abs(actualStart - pStart);
            if (dist < minCvvScore) {
                minCvvScore = dist;
                bestCvv = cvvCand;
            }
        }

        if (bestCvv) {
            const [mm, yy] = bestDate.split('|');
            foundCards.add(card);
            results.push({ card, mm, yy, cvv: bestCvv, source: 'heuristic' });
        }
    }
    return results;
}

// ─── Filter Valid Dates ───────────────────────────────────────
function filterValidDates(results) {
    const now = new Date();
    const currentYearShort = now.getFullYear() % 100;
    const currentMonth = now.getMonth() + 1;

    return results.filter(r => {
        const expMonth = parseInt(r.mm, 10);
        const expYear = parseInt(r.yy, 10);
        return expYear > currentYearShort || (expYear === currentYearShort && expMonth >= currentMonth);
    });
}

// ─── Main Extract Function ────────────────────────────────────
function extractCC(text) {
    const allResults = [];
    const foundCards = new Set();

    const lineData = parseLineFormats(text);
    for (const r of lineData.results) {
        if (!foundCards.has(r.card)) {
            foundCards.add(r.card);
            allResults.push(r);
        }
    }

    const structData = parseStructured(text);
    for (const r of structData.results) {
        if (!foundCards.has(r.card)) {
            foundCards.add(r.card);
            allResults.push(r);
        }
    }

    const heurData = heuristicExtract(text, foundCards);
    for (const r of heurData) {
        if (!foundCards.has(r.card)) {
            foundCards.add(r.card);
            allResults.push(r);
        }
    }

    const valid = filterValidDates(allResults);
    return valid.map(r => `${r.card}|${r.mm}|${r.yy}|${r.cvv}`).sort();
}

// ─── Chunk Processing ─────────────────────────────────────────
function processInChunks(text, chunkSize = 500000) {
    const allResults = new Set();
    for (let i = 0; i < text.length; i += chunkSize) {
        const chunk = text.slice(i, i + chunkSize);
        const res = extractCC(chunk);
        res.forEach(r => allResults.add(r));
    }
    return Array.from(allResults).sort();
}

// ═══════════════════════════════════════════════════════════════
// ROUTES
// ═══════════════════════════════════════════════════════════════

// Health check (KHÔNG qua rate limit)
app.get('/health', (req, res) => {
    res.json({ status: 'ok', uptime: process.uptime(), workers: 1 });
});

// Main API endpoint
app.post('/v1/ccclean', limiter, (req, res) => {
    const startTime = Date.now();
    const text = req.body?.text || req.body;

    if (!text || typeof text !== 'string') {
        return res.status(400).json({ error: 'Missing or invalid text body' });
    }

    const useChunking = text.length > 1000000;
    const results = useChunking ? processInChunks(text) : extractCC(text);

    res.json({
        success: true,
        count: results.length,
        processing_time_ms: Date.now() - startTime,
        chunking_used: useChunking,
        data: results
    });
});

// 404 handler
app.use((req, res) => {
    res.status(404).json({ error: 'Not found', path: req.path, method: req.method });
});

// ═══════════════════════════════════════════════════════════════
// START SERVER - Single instance (Render Free = 1 worker)
// ═══════════════════════════════════════════════════════════════
const PORT = process.env.PORT || 3000;

app.listen(PORT, '0.0.0.0', () => {
    console.log(`Server running on port ${PORT} (PID: ${process.pid})`);
    console.log(`Routes: GET /health, POST /v1/ccclean`);
});

module.exports = app;
