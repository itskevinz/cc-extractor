const { parentPort, workerData } = require('worker_threads');

// ============ LUHN (trong worker) ============
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

// ============ EXTRACTOR (trong worker) ============
function extract(text) {
  const results = [];
  const found = new Set();
  const yearExclusions = new Set();
  for (let y = 2020; y <= 2040; y++) yearExclusions.add(String(y));

  const skipPrefixes = [
    'country:', 'address:', 'scheme:', 'level:', 'bin:', 'secure_code:',
    'type:', 'bank:', 'full_name:', 'dob:', 'phone_number:', 'expiration:',
    'order:', 'cc|month|year|cvv'
  ];

  // Phase 1: Line formats
  const lines = text.split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const firstWord = trimmed.split(':')[0];
    if (skipPrefixes.some(p => firstWord === p.replace(':', ''))) continue;

    let m = trimmed.match(/^(\d{13,19})\|(\d{6})\|(\d{3,4})\b/);
    if (m && luhnCheck(m[1]) && !found.has(m[1])) {
      found.add(m[1]);
      results.push([m[1], m[2].slice(4, 6), m[2].slice(2, 4), m[3], 'format_yyyymm']);
      continue;
    }

    m = trimmed.match(/^(\d{13,19})\|(\d{2})\/(\d{2})\|(\d{3,4})\b/);
    if (m && luhnCheck(m[1]) && !found.has(m[1])) {
      found.add(m[1]);
      results.push([m[1], m[2], m[3], m[4], 'format_mm_slash_yy']);
      continue;
    }

    m = trimmed.match(/^(\d{13,19})\|(\d{2})\/(\d{2})\|(\d{3,4})\|/);
    if (m && luhnCheck(m[1]) && !found.has(m[1])) {
      found.add(m[1]);
      results.push([m[1], m[2], m[3], m[4], 'format_slovakia']);
      continue;
    }

    m = trimmed.match(/^(\d{13,19})~(\d{2})\/(\d{2})~(\d{3,4})\b/);
    if (m && luhnCheck(m[1]) && !found.has(m[1])) {
      found.add(m[1]);
      results.push([m[1], m[2], m[3], m[4], 'format_tilde']);
      continue;
    }

    m = trimmed.match(/^(\d{13,19})\|(\d{2})\|(\d{2})\|(\d{3,4})\b/);
    if (m && luhnCheck(m[1]) && !found.has(m[1])) {
      found.add(m[1]);
      results.push([m[1], m[2], m[3], m[4], 'format_pipe_mm_yy']);
    }
  }

  // Phase 2: Structured
  const pattern = /card_number:\s*(\d{13,19})\s*\n(?:[^\n]*\n)*?secure_code:\s*(\d{3,4})\s*\n(?:[^\n]*\n)*?expiration:\s*(\d{2})\/(\d{2})/gi;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (luhnCheck(match[1]) && !found.has(match[1])) {
      found.add(match[1]);
      results.push([match[1], match[3], match[4], match[2], 'structured']);
    }
  }

  // Phase 3: Heuristic (giới hạn context để tránh chậm)
  const textLen = text.length;
  const panPattern = /\b\d{13,19}\b/g;
  let panMatch;
  while ((panMatch = panPattern.exec(text)) !== null) {
    const card = panMatch[0];
    if (found.has(card) || !luhnCheck(card)) continue;

    const pStart = panMatch.index;
    const ctxStart = Math.max(0, pStart - 200);  // Giảm từ 300 xuống 200
    const ctxEnd = Math.min(textLen, pStart + card.length + 300);  // Giảm từ 400 xuống 300
    const context = text.slice(ctxStart, ctxEnd);
    const offset = ctxStart;

    let bestDate = '';
    let minDateScore = Infinity;

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
      if (yearExclusions.has(cvv)) continue;
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
  return valid;
}

// Run
const result = extract(workerData.text);
parentPort.postMessage(result);
