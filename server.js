const express = require('express');
const rateLimit = require('express-rate-limit');
const { Worker, isMainThread, parentPort, workerData } = require('worker_threads');

if (isMainThread) {
    const app = express();

    app.set('trust proxy', 1);
    app.use(express.json({ limit: '50mb' }));
    app.use(express.text({ limit: '50mb' }));

    const limiter = rateLimit({
        windowMs: 60 * 1000,
        max: 200,
        standardHeaders: true,
        legacyHeaders: false,
        skip: (req) => req.path === '/health',
        validate: { xForwardedForHeader: false }
    });

    app.get('/health', (req, res) => {
        res.json({ status: 'ok', uptime: process.uptime() });
    });

    app.post('/v1/ccclean', limiter, (req, res) => {
        const text = req.body?.text || req.body;

        if (!text || typeof text !== 'string') {
            return res.status(400).json({ error: 'Missing or invalid text body' });
        }

        const worker = new Worker(__filename, { workerData: text });

        worker.on('message', (result) => {
            res.json(result);
        });

        worker.on('error', (err) => {
            res.status(500).json({ error: err.message });
        });
    });

    const PORT = process.env.PORT || 3000;
    app.listen(PORT, '0.0.0.0');

    module.exports = app;
} else {
    const text = workerData;
    const startTime = Date.now();

    function verifyLuhn(cardNumber) {
        const digits = [];
        for (let i = 0; i < cardNumber.length; i++) {
            const char = cardNumber[i];
            if (char >= '0' && char <= '9') {
                digits.push(Number(char));
            }
        }
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

    const PAN_REGEX = /\b\d{13,19}\b/g;
    const CVV_REGEX = /\b\d{3,4}\b/g;
    const DATE_STRICT_REGEX = /\b(0[1-9]|1[0-2])[\s\-\/|]?(20\d{2}|\d{2})\b/g;
    const DATE_YYYYMM_REGEX = /\b(20\d{2})(0[1-9]|1[0-2])\b/g;
    const YEAR_EXCLUSIONS = new Set(Array.from({ length: 20 }, (_, i) => String(2020 + i)));

    const LINE_PATTERNS = [
        { regex: /^\d{13,19}\b\|\d{6}\|\d{3,4}\b/, type: 'format_yyyymm', extract: (m) => {
            const parts = m[0].split('|');
            return [parts[0], parts[1].slice(4, 6), parts[1].slice(2, 4), parts[2]];
        }},
        { regex: /^\d{13,19}\b\|\d{2}\/\d{2}\|\d{3,4}\b/, type: 'format_mm_slash_yy', extract: (m) => {
            const parts = m[0].split('|');
            const dateParts = parts[1].split('/');
            return [parts[0], dateParts[0], dateParts[1], parts[2]];
        }},
        { regex: /^\d{13,19}\b~\d{2}\/\d{2}~\d{3,4}\b/, type: 'format_tilde', extract: (m) => {
            const parts = m[0].split('~');
            const dateParts = parts[1].split('/');
            return [parts[0], dateParts[0], dateParts[1], parts[2]];
        }},
        { regex: /^\d{13,19}\b\|\d{2}\|\d{2}\|\d{3,4}\b/, type: 'format_pipe_mm_yy', extract: (m) => {
            const parts = m[0].split('|');
            return [parts[0], parts[1], parts[2], parts[3]];
        }},
        { regex: /^\d{13,19}\b\|\d{2}\/\d{2}\|\d{3,4}\|/, type: 'format_slovakia', extract: (m) => {
            const parts = m[0].split('|');
            const dateParts = parts[1].split('/');
            return [parts[0], dateParts[0], dateParts[1], parts[2]];
        }},
    ];

    const SKIP_PREFIXES = ['country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:', 'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:', 'order:', 'cc|month|year|cvv'];

    const allResults = [];
    const foundCards = new Set();

    let pos = 0;
    while (pos < text.length) {
        let nextNL = text.indexOf('\n', pos);
        if (nextNL === -1) nextNL = text.length;
        const line = text.substring(pos, nextNL);
        pos = nextNL + 1;

        const trimmed = line.trim();
        if (trimmed.length < 20) continue;

        let skip = false;
        for (let i = 0; i < SKIP_PREFIXES.length; i++) {
            if (trimmed.startsWith(SKIP_PREFIXES[i])) {
                skip = true;
                break;
            }
        }
        if (skip) continue;

        let matchedLine = false;
        for (let i = 0; i < LINE_PATTERNS.length; i++) {
            const p = LINE_PATTERNS[i];
            const m = trimmed.match(p.regex);
            if (m) {
                const [card, mm, yy, cvv] = p.extract(m);
                if (!foundCards.has(card) && verifyLuhn(card)) {
                    foundCards.add(card);
                    allResults.push({ card, mm, yy, cvv, source: p.type });
                }
                matchedLine = true;
                break;
            }
        }

        if (!matchedLine && trimmed.includes('card_number:')) {
            const cardMatch = trimmed.match(/card_number:\s*(\d{13,19})/);
            if (cardMatch) {
                const card = cardMatch[1];
                const blockEnd = text.indexOf('\n\n', pos);
                const contextEnd = blockEnd === -1 ? text.length : blockEnd;
                const blockContext = text.substring(pos - line.length - 1, contextEnd);

                const cvvMatch = blockContext.match(/secure_code:\s*(\d{3,4})/);
                const expMatch = blockContext.match(/expiration:\s*(\d{2})\/(\d{2})/);

                if (cvvMatch && expMatch && !foundCards.has(card) && verifyLuhn(card)) {
                    foundCards.add(card);
                    allResults.push({ card, mm: expMatch[1], yy: expMatch[2], cvv: cvvMatch[1], source: 'structured' });
                }
            }
        }
    }

    PAN_REGEX.lastIndex = 0;
    let m;
    while ((m = PAN_REGEX.exec(text)) !== null) {
        const card = m[0];
        if (foundCards.has(card) || !verifyLuhn(card)) continue;

        const pStart = m.index;
        const spaceStart = Math.max(0, pStart - 300);
        const spaceEnd = Math.min(text.length, pStart + card.length + 400);
        const context = text.substring(spaceStart, spaceEnd);

        let bestDate = '';
        let minDateScore = Infinity;

        DATE_STRICT_REGEX.lastIndex = 0;
        let dm;
        while ((dm = DATE_STRICT_REGEX.exec(context)) !== null) {
            const dist = Math.abs((spaceStart + dm.index) - pStart);
            if (dist < minDateScore) {
                minDateScore = dist;
                bestDate = `${dm[1]}|${dm[2].slice(-2)}`;
            }
        }

        DATE_YYYYMM_REGEX.lastIndex = 0;
        while ((dm = DATE_YYYYMM_REGEX.exec(context)) !== null) {
            const dist = Math.abs((spaceStart + dm.index) - pStart);
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
            const actualStart = spaceStart + cm.index;

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
            allResults.push({ card, mm, yy, cvv: bestCvv, source: 'heuristic' });
        }
    }

    const now = new Date();
    const currentYearShort = now.getFullYear() % 100;
    const currentMonth = now.getMonth() + 1;

    const valid = allResults.filter(r => {
        const expMonth = parseInt(r.mm, 10);
        const expYear = parseInt(r.yy, 10);
        return expYear > currentYearShort || (expYear === currentYearShort && expMonth >= currentMonth);
    });

    const formatted = valid.map(r => `${r.card}|${r.mm}|${r.yy}|${r.cvv}`).sort();

    parentPort.postMessage({
        success: true,
        count: formatted.length,
        processing_time_ms: Date.now() - startTime,
        data: formatted
    });
}
