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
app.use(express.json({ limit: '5mb' }));  // Giảm từ 10MB xuống 5MB
app.use(express.urlencoded({ extended: true, limit: '5mb' }));

// Rate limit: 60 requests / 1 phút (dễ test hơn)
const limiter = rateLimit({
  windowMs: 60 * 1000,  // 1 phút
  max: 60,
  message: { success: false, error: 'Too many requests', code: 'RATE_LIMIT' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: (req) => req.headers['x-forwarded-for'] || req.ip,
});
app.use('/api/', limiter);

// Cache: 10 phút
const cache = new NodeCache({ stdTTL: 600, checkperiod: 120 });

// Worker pool
const MAX_WORKERS = 4;
const workers = [];
let workerIndex = 0;

function getWorker() {
  if (workers.length === 0) {
    for (let i = 0; i < MAX_WORKERS; i++) {
      workers.push(new Worker(path.resolve(__dirname, 'worker.js')));
    }
  }
  const worker = workers[workerIndex];
  workerIndex = (workerIndex + 1) % workers.length;
  return worker;
}

function extractWithWorker(text) {
  return new Promise((resolve, reject) => {
    const worker = getWorker();
    const timeout = setTimeout(() => {
      reject(new Error('Worker timeout'));
    }, 15000);  // 15 giây timeout

    worker.once('message', (result) => {
      clearTimeout(timeout);
      resolve(result);
    });

    worker.once('error', (err) => {
      clearTimeout(timeout);
      reject(err);
    });

    worker.postMessage({ text });
  });
}

// ============ ROUTES ============
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    uptime: process.uptime(), 
    workers: workers.length,
    timestamp: new Date().toISOString() 
  });
});

app.get('/api/extract', (req, res) => {
  res.json({ status: 'API running', method: 'GET', try: 'POST /api/extract' });
});

app.post('/api/extract', async (req, res) => {
  const start = Date.now();

  const text = req.body.text || req.body.data || '';

  if (!text || typeof text !== 'string') {
    return res.status(400).json({ success: false, error: 'Missing text', code: 'MISSING_INPUT' });
  }

  // Giới hạn input
  if (text.length > 5 * 1024 * 1024) {
    return res.status(413).json({ success: false, error: 'Max 5MB', code: 'TOO_LARGE' });
  }

  // Giới hạn số dòng (tránh DoS)
  const lineCount = text.split('\n').length;
  if (lineCount > 10000) {
    return res.status(413).json({ success: false, error: 'Max 10000 lines', code: 'TOO_MANY_LINES' });
  }

  try {
    // Check cache
    const cacheKey = `extract_${require('crypto').createHash('md5').update(text).digest('hex')}`;
    const cached = cache.get(cacheKey);
    if (cached) {
      return res.json({
        success: true,
        count: cached.length,
        data: cached,
        meta: {
          processing_time_ms: Date.now() - start,
          input_length: text.length,
          cached: true,
          timestamp: new Date().toISOString()
        }
      });
    }

    // Extract trong worker thread
    const results = await extractWithWorker(text);

    // Save cache
    cache.set(cacheKey, results);

    res.json({
      success: true,
      count: results.length,
      data: results,
      meta: {
        processing_time_ms: Date.now() - start,
        input_length: text.length,
        cached: false,
        timestamp: new Date().toISOString()
      }
    });
  } catch (err) {
    console.error('Extract error:', err);
    res.status(500).json({ success: false, error: err.message, code: 'ERROR' });
  }
});

app.use((req, res) => {
  res.status(404).json({ success: false, error: 'Not found', code: 'NOT_FOUND' });
});

app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).json({ success: false, error: 'Internal error', code: 'SERVER_ERROR' });
});

// Graceful shutdown
process.on('SIGTERM', () => {
  workers.forEach(w => w.terminate());
  process.exit(0);
});

app.listen(PORT, () => {
  console.log(`🚀 Card Extract API running on port ${PORT}`);
  console.log(`📡 Health: http://localhost:${PORT}/health`);
  console.log(`📡 API: http://localhost:${PORT}/api/extract`);
});
