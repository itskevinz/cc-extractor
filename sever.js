const express = require('express');
const rateLimit = require('express-rate-limit');
const helmet = require('helmet');
const cors = require('cors');
const compression = require('compression');
const NodeCache = require('node-cache');
const { Worker } = require('worker_threads');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// ============ MIDDLEWARE ============
app.use(helmet());
app.use(cors());
app.use(compression());
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true, limit: '10mb' }));

// Rate limiting: 100 requests / 15 minutes per IP
const limiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 100,
  message: { success: false, error: 'Too many requests', code: 'RATE_LIMIT' },
  standardHeaders: true,
  legacyHeaders: false,
});
app.use('/api/', limiter);

// Cache: TTL 5 phút
const cache = new NodeCache({ stdTTL: 300, checkperiod: 60 });

// ============ LUHN CHECKER ============
function luhnCheck(cardNumber) {
  const len = cardNumber.length;
  if (len < 13 || len > 19) return false;

  let sum = 0;
  let alternate = false;

  for (let i = len - 1; i >= 0; i--) {
    let n = parseInt(cardNumber[i], 10);
    if (alternate) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    sum += n;
    alternate = !alternate;
  }

  return sum % 10 === 0;
}

// ============ EXTRACTOR CLASS ============
class CardExtractor {
  constructor() {
    this.skipPrefixes = [
      'country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:',
      'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:',
      'order:', 'cc|month|year|cvv'
    ];
    this.yearExclusions = new Set();
    for (let y = 2020; y <= 2040; y++) {
      this.yearExclusions.add(String(y));
    }
  }

  // Phase 1: Line formats (fastest)
  parseLines(text) {
    const results = [];
    const found = new Set();
    const lines = text.split('\n');

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      // Quick skip check
      const firstWord = trimmed.split(':')[0];
      if (this.skipPrefixes.some(p => firstWord === p.replace(':', ''))) continue;

      // card|yyyymm|cvv
      let m = trimmed.match(/^(\d{13,19})\|(\d{6})\|(\d{3,4})\b/);
      if (m) {
        if (luhnCheck(m[1]) && !found.has(m[1])) {
          found.add(m[1]);
          results.push([m[1], m[2].slice(4, 6), m[2].slice(2, 4), m[3], 'format_yyyymm']);
        }
        continue;
      }

      // card|mm/yy|cvv
      m = trimmed.match(/^(\d{13,19})\|(\d{2})\/(\d{2})\|(\d{3,4})\b/);
      if (m) {
        if (luhnCheck(m[1]) && !found.has(m[1])) {
          found.add(m[1]);
          results.push([m[1], m[2], m[3], m[4], 'format_mm_slash_yy']);
        }
        continue;
      }

      // card|mm/yy|cvv|name (Slovakia)
      m = trimmed.match(/^(\d{13,19})\|(\d{2})\/(\d{2})\|(\d{3,4})\|/);
      if (m) {
        if (luhnCheck(m[1]) && !found.has(m[1])) {
          found.add(m[1]);
          results.push([m[1], m[2], m[3], m[4], 'format_slovakia']);
        }
        continue;
      }

      // card~mm/yy~cvv
      m = trimmed.match(/^(\d{13,19})~(\d{2})\/(\d{2})~(\d{3,4})\b/);
      if (m) {
        if (luhnCheck(m[1]) && !found.has(m[1])) {
          found.add(m[1]);
          results.push([m[1], m[2], m[3], m[4], 'format_tilde']);
        }
        continue;
      }

      // card|mm|yy|cvv
      m = trimmed.match(/^(\d{13,19})\|(\d{2})\|(\d{2})\|(\d{3,4})\b/);
      if (m) {
        if (luhnCheck(m[1]) && !found.has(m[1])) {
          found.add(m[1]);
          results.push([m[1], m[2], m[3], m[4], 'format_pipe_mm_yy']);
        }
        continue;
      }
    }

    return { results, found };
  }

  // Phase 2: Structured blocks
  parseStructured(text, found) {
    const results = [];
    const pattern = /card_number:\s*(\d{13,19})\s*\n(?:[^\n]*\n)*?secure_code:\s*(\d{3,4})\s*\n(?:[^\n]*\n)*?expiration:\s*(\d{2})\/(\d{2})/gi;
    let match;

    while ((match = pattern.exec(text)) !== null) {
      if (luhnCheck(match[1]) && !found.has(match[1])) {
        found.add(match[1]);
        results.push([match[1], match[3], match[4], match[2], 'structured']);
      }
    }

    return { results, found };
  }

  // Phase 3: Heuristic extraction
  heuristicExtract(text, found) {
    const results = [];
    const textLen = text.length;
    const panPattern = /\b\d{13,19}\b/g;
    let panMatch;

    while ((panMatch = panPattern.exec(text)) !== null) {
      const card = panMatch[0];
      if (found.has(card) || !luhnCheck(card)) continue;

      const pStart = panMatch.index;
      const ctxStart = Math.max(0, pStart - 300);
      const ctxEnd = Math.min(textLen, pStart + card.length + 400);
      const context = text.slice(ctxStart, ctxEnd);
      const offset = ctxStart;

      let bestDate = '';
      let minDateScore = Infinity;

      // Date strict
      const datePattern = /\b(0[1-9]|1[0-2])[\s\-\/|]?(20\d{2}|\d{2})\b/g;
      let dateMatch;
      while ((dateMatch = datePattern.exec(context)) !== null) {
        const dist = Math.abs((offset + dateMatch.index) - pStart);
        if (dist < minDateScore) {
          minDateScore = dist;
          const yy = dateMatch[2];
          bestDate = `${dateMatch[1]}|${yy.length === 4 ? yy.slice(2) : yy}`;
        }
      }

      // Date YYYYMM
      const yyyymmPattern = /\b(20\d{2})(0[1-9]|1[0-2])\b/g;
      let yyyymmMatch;
      while ((yyyymmMatch = yyyymmPattern.exec(context)) !== null) {
        const dist = Math.abs((offset + yyyymmMatch.index) - pStart);
        if (dist < minDateScore) {
          minDateScore = dist;
          bestDate = `${yyyymmMatch[2]}|${yyyymmMatch[1].slice(2)}`;
        }
      }

      if (!bestDate) continue;

      let bestCvv = '';
      let minCvvScore = Infinity;
      const cvvPattern = /\b\d{3,4}\b/g;
      let cvvMatch;

      while ((cvvMatch = cvvPattern.exec(context)) !== null) {
        const cvv = cvvMatch[0];
        const pos = offset + cvvMatch.index;

        if (card.includes(cvv) || bestDate.replace('|', '').includes(cvv)) continue;
        if (this.yearExclusions.has(cvv)) continue;

        const left = pos - 1;
        const right = pos + cvv.length;
        if ((left >= 0 && /\d/.test(text[left])) || (right < textLen && /\d/.test(text[right]))) continue;

        const dist = Math.abs(pos - pStart);
        if (dist < minCvvScore) {
          minCvvScore = dist;
          bestCvv = cvv;
        }
      }

      if (bestCvv) {
        const [m, y] = bestDate.split('|');
        results.push([card, m, y, bestCvv, 'heuristic']);
        found.add(card);
      }
    }

    return results;
  }

  // Main extraction
  extract(text) {
    // Check cache
    const cacheKey = `extract_${require('crypto').createHash('md5').update(text).digest('hex')}`;
    const cached = cache.get(cacheKey);
    if (cached) return cached;

    // Phase 1
    let { results, found } = this.parseLines(text);

    // Phase 2
    const structured = this.parseStructured(text, found);
    results = results.concat(structured.results);
    found = structured.found;

    // Phase 3
    const heuristic = this.heuristicExtract(text, found);
    results = results.concat(heuristic);

    // Filter expired
    const now = new Date();
    const curYear = now.getFullYear() % 100;
    const curMonth = now.getMonth() + 1;

    const valid = [];
    for (const row of results) {
      const ey = parseInt(row[2], 10);
      const em = parseInt(row[1], 10);
      if (ey < curYear || (ey === curYear && em < curMonth)) continue;
      valid.push(`${row[0]}|${row[1]}|${row[2]}|${row[3]}`);
    }

    valid.sort();

    // Save cache
    cache.set(cacheKey, valid);
    return valid;
  }
}

const extractor = new CardExtractor();

// ============ ROUTES ============

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptime: process.uptime(), timestamp: new Date().toISOString() });
});

// API endpoint
app.post('/api/extract', (req, res) => {
  const start = Date.now();

  const text = req.body.text || req.body.data || '';

  if (!text || typeof text !== 'string') {
    return res.status(400).json({ success: false, error: 'Missing text', code: 'MISSING_INPUT' });
  }

  // Size limit: 10MB
  if (text.length > 10 * 1024 * 1024) {
    return res.status(413).json({ success: false, error: 'Max 10MB', code: 'TOO_LARGE' });
  }

  try {
    const results = extractor.extract(text);
    const time = Date.now() - start;

    res.json({
      success: true,
      count: results.length,
      data: results,
      meta: {
        processing_time_ms: time,
        input_length: text.length,
        timestamp: new Date().toISOString()
      }
    });
  } catch (err) {
    console.error('Extract error:', err);
    res.status(500).json({ success: false, error: err.message, code: 'ERROR' });
  }
});

// GET test
app.get('/api/extract', (req, res) => {
  res.json({ status: 'API running', method: 'GET', try: 'POST /api/extract' });
});

// 404
app.use((req, res) => {
  res.status(404).json({ success: false, error: 'Not found', code: 'NOT_FOUND' });
});

// Error handler
app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).json({ success: false, error: 'Internal error', code: 'SERVER_ERROR' });
});

// ============ START ============
app.listen(PORT, () => {
  console.log(`Card Extract API running on port ${PORT}`);
  console.log(`Health: http://localhost:${PORT}/health`);
  console.log(`API: http://localhost:${PORT}/api/extract`);
});
