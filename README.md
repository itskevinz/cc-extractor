# 🧹 CCClean API

> **Choose your language / Chọn ngôn ngữ:**  
> [ English](#english) | [ Tiếng Việt](#tiếng-việt)

---

##  English <a name="english"></a>

API to extract credit/debit card details (PAN, expiry, CVV) from raw text. Automatically detects multiple formats, validates Luhn, and filters expired cards.

**Live URL:** `https://your-app.onrender.com`

---

### 📡 Endpoint

#### `POST /v1/ccclean`

Extract cards from text.

**Headers**
```
Content-Type: text/plain
```

**Body**  
Raw text containing card data. The API auto‑detects these formats:

| Format | Example |
|--------|---------|
| Pipe yyyymm | `4111111111111111\|202512\|123` |
| Pipe mm/yy | `4111111111111111\|12/25\|123` |
| Tilde | `4111111111111111~12/25~123` |
| Pipe full | `4111111111111111\|12\|25\|123` |
| Slovakia | `4111111111111111\|12/25\|123\|John Doe` |
| Structured | `card_number: ...\\nsecure_code: ...\\nexpiration: ...` |
| Heuristic | Auto‑find PAN, date, CVV in context |

**Response**

*Success:*
```json
{
  "success": true,
  "count": 3,
  "processing_time_ms": 12,
  "chunking_used": false,
  "data": [
    "4111111111111111|12|25|123",
    "5500000000000004|06|26|456",
    "371449635398431|11|27|789"
  ]
}
```

*Error:*
```json
{
  "error": "Missing or invalid text body"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Processing status |
| `count` | int | Number of extracted cards |
| `processing_time_ms` | int | Time taken (ms) |
| `chunking_used` | boolean | Whether input was chunked (>1MB) |
| `data` | string[] | Sorted array of `PAN\|MM\|YY\|CVV` |

---

#### `GET /health`

Check server status.

**Response**
```json
{
  "status": "ok",
  "uptime": 437.02,
  "workers": 8
}
```

---

### 🛠️ Usage Examples

**cURL**
```bash
curl -X POST https://your-app.onrender.com/v1/ccclean \
  -H "Content-Type: text/plain" \
  -d "4111111111111111|12/25|123
5500000000000004|06/26|456
371449635398431~11/27~789"
```

**Python (requests)**
```python
import requests

url = "https://your-app.onrender.com/v1/ccclean"
data = """4111111111111111|12/25|123
5500000000000004|06/26|456"""

resp = requests.post(url, data=data, headers={"Content-Type": "text/plain"})
print(resp.json())
# {'success': True, 'count': 2, ...}
```

**Python (async – faster for batches)**
```python
import aiohttp
import asyncio

async def extract(session, text):
    async with session.post(
        "https://your-app.onrender.com/v1/ccclean",
        data=text,
        headers={"Content-Type": "text/plain"}
    ) as resp:
        return await resp.json()

async def main():
    texts = ["data1...", "data2...", "data3..."]
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[extract(session, t) for t in texts])
        for r in results:
            print(f"Found {r['count']} cards")

asyncio.run(main())
```

**Node.js (axios)**
```javascript
const axios = require('axios');

const text = `4111111111111111|12/25|123
5500000000000004|06/26|456`;

axios.post('https://your-app.onrender.com/v1/ccclean', text, {
    headers: { 'Content-Type': 'text/plain' }
}).then(res => {
    console.log(res.data.count, 'cards found');
    console.log(res.data.data);
});
```

**JavaScript (fetch)**
```javascript
const text = `4111111111111111|12/25|123`;

fetch('https://your-app.onrender.com/v1/ccclean', {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: text
})
.then(r => r.json())
.then(data => console.log(data));
```

---

### ⚙️ Features

| Feature | Description |
|---------|-------------|
| ✅ Luhn validation | Validates card numbers using the Luhn algorithm |
| ✅ Multi‑format | Supports 6+ different formats |
| ✅ Heuristic | Finds cards even without a fixed pattern |
| ✅ Filter expired | Discards cards with expiry in the past |
| ✅ Chunk processing | Automatically splits input >1MB to avoid OOM |
| ✅ Rate limiting | 200 req/minute to protect the server |
| ✅ Cluster mode | Multi‑worker when `NODE_ENV=production` |
| ✅ Trust proxy | Works behind Render’s reverse proxy |

---

### 🚀 Deploy to Render

1. **Push code to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/ccclean-api.git
   git push -u origin main
   ```

2. **Create a Web Service**
   - Go to [Render Dashboard](https://dashboard.render.com/)
   - Click **New** → **Web Service**
   - Connect your GitHub repo
   - Configure:
     - **Runtime**: Node
     - **Build Command**: `npm install`
     - **Start Command**: `npm start`
     - **Plan**: Free or Starter

3. **Environment Variables (optional)**
   ```
   NODE_ENV=production   # Enables cluster mode (multi-worker)
   ```

---

### 📊 Performance

| Input size | Latency | Cards | Notes |
|------------|---------|-------|-------|
| ~50 KB | 50‑200 ms | 10‑100 | Single request |
| ~1 MB | 2‑5 s | 50‑500 | Chunking enabled |
| 20 concurrent req | 7‑8 s avg | 60/card | Free tier limits |

> **Tip:** If latency is high, upgrade to **Render Starter** ($7/month) for dedicated CPU and no spin‑down.

---

### 🐛 Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR` | Missing `trust proxy` | Already fixed in latest code – pull it |
| 429 Too Many Requests | Exceeded 200 req/min | Reduce frequency or upgrade plan |
| Timeout | Payload too large | Split payload or use a queue |
| 0 cards found | Unrecognised format | Check the format, try heuristic mode |

---

### 📄 License

MIT

---

---

##  Tiếng Việt <a name="tiếng-việt"></a>

API trích xuất thông tin thẻ tín dụng/ghi nợ (PAN, hạn sử dụng, CVV) từ văn bản thô. Tự động nhận diện nhiều định dạng, kiểm tra Luhn và lọc thẻ hết hạn.

**Live URL:** `https://your-app.onrender.com`

---

### 📡 Endpoint

#### `POST /v1/ccclean`

Trích xuất thẻ từ văn bản.

**Headers**
```
Content-Type: text/plain
```

**Body**  
Văn bản thô chứa dữ liệu thẻ. API tự động nhận diện các định dạng sau:

| Định dạng | Ví dụ |
|-----------|-------|
| Pipe yyyymm | `4111111111111111\|202512\|123` |
| Pipe mm/yy | `4111111111111111\|12/25\|123` |
| Tilde | `4111111111111111~12/25~123` |
| Pipe đầy đủ | `4111111111111111\|12\|25\|123` |
| Slovakia | `4111111111111111\|12/25\|123\|John Doe` |
| Có cấu trúc | `card_number: ...\\nsecure_code: ...\\nexpiration: ...` |
| Heuristic | Tự tìm PAN, ngày, CVV trong ngữ cảnh |

**Phản hồi**

*Thành công:*
```json
{
  "success": true,
  "count": 3,
  "processing_time_ms": 12,
  "chunking_used": false,
  "data": [
    "4111111111111111|12|25|123",
    "5500000000000004|06|26|456",
    "371449635398431|11|27|789"
  ]
}
```

*Lỗi:*
```json
{
  "error": "Missing or invalid text body"
}
```

| Trường | Kiểu | Mô tả |
|--------|------|-------|
| `success` | boolean | Trạng thái xử lý |
| `count` | int | Số thẻ trích xuất được |
| `processing_time_ms` | int | Thời gian xử lý (ms) |
| `chunking_used` | boolean | Có chia nhỏ đầu vào không (>1MB) |
| `data` | string[] | Mảng `PAN\|MM\|YY\|CVV` đã được sắp xếp |

---

#### `GET /health`

Kiểm tra trạng thái server.

**Phản hồi**
```json
{
  "status": "ok",
  "uptime": 437.02,
  "workers": 8
}
```

---

### 🛠️ Ví dụ sử dụng

**cURL**
```bash
curl -X POST https://your-app.onrender.com/v1/ccclean \
  -H "Content-Type: text/plain" \
  -d "4111111111111111|12/25|123
5500000000000004|06/26|456
371449635398431~11/27~789"
```

**Python (requests)**
```python
import requests

url = "https://your-app.onrender.com/v1/ccclean"
data = """4111111111111111|12/25|123
5500000000000004|06/26|456"""

resp = requests.post(url, data=data, headers={"Content-Type": "text/plain"})
print(resp.json())
# {'success': True, 'count': 2, ...}
```

**Python (bất đồng bộ – nhanh hơn cho nhiều yêu cầu)**
```python
import aiohttp
import asyncio

async def extract(session, text):
    async with session.post(
        "https://your-app.onrender.com/v1/ccclean",
        data=text,
        headers={"Content-Type": "text/plain"}
    ) as resp:
        return await resp.json()

async def main():
    texts = ["data1...", "data2...", "data3..."]
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[extract(session, t) for t in texts])
        for r in results:
            print(f"Tìm thấy {r['count']} thẻ")

asyncio.run(main())
```

**Node.js (axios)**
```javascript
const axios = require('axios');

const text = `4111111111111111|12/25|123
5500000000000004|06/26|456`;

axios.post('https://your-app.onrender.com/v1/ccclean', text, {
    headers: { 'Content-Type': 'text/plain' }
}).then(res => {
    console.log(res.data.count, 'thẻ được tìm thấy');
    console.log(res.data.data);
});
```

**JavaScript (fetch)**
```javascript
const text = `4111111111111111|12/25|123`;

fetch('https://your-app.onrender.com/v1/ccclean', {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: text
})
.then(r => r.json())
.then(data => console.log(data));
```

---

### ⚙️ Tính năng

| Tính năng | Mô tả |
|-----------|-------|
| ✅ Kiểm tra Luhn | Xác thực số thẻ bằng thuật toán Luhn |
| ✅ Đa định dạng | Hỗ trợ 6+ định dạng khác nhau |
| ✅ Heuristic | Tìm thẻ ngay cả khi không có mẫu cố định |
| ✅ Lọc hết hạn | Loại bỏ thẻ đã quá hạn sử dụng |
| ✅ Xử lý chunk | Tự động chia nhỏ đầu vào >1MB để tránh quá bộ nhớ |
| ✅ Giới hạn tần suất | 200 yêu cầu/phút để bảo vệ server |
| ✅ Chế độ cluster | Nhiều worker khi `NODE_ENV=production` |
| ✅ Trust proxy | Hoạt động tốt với reverse proxy của Render |

---

### 🚀 Triển khai lên Render

1. **Đẩy code lên GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/itskevinz/api.git
   git push -u origin main
   ```

2. **Tạo Web Service**
   - Truy cập [Render Dashboard](https://dashboard.render.com/)
   - Chọn **New** → **Web Service**
   - Kết nối kho GitHub của bạn
   - Cấu hình:
     - **Runtime**: Node
     - **Build Command**: `npm install`
     - **Start Command**: `npm start`
     - **Plan**: Free hoặc Starter

3. **Biến môi trường (tuỳ chọn)**
   ```
   NODE_ENV=production   # Bật chế độ cluster (nhiều worker)
   ```

---

### 📊 Hiệu suất

| Kích thước đầu vào | Độ trễ | Số thẻ | Ghi chú |
|-------------------|--------|--------|---------|
| ~50 KB | 50‑200 ms | 10‑100 | Yêu cầu đơn |
| ~1 MB | 2‑5 s | 50‑500 | Bật chunking |
| 20 yêu cầu đồng thời | Trung bình 7‑8 s | 60/thẻ | Giới hạn gói miễn phí |

> **Mẹo:** Nếu độ trễ cao, hãy nâng lên gói **Render Starter** ($7/tháng) để có CPU chuyên dụng và không bị spin‑down.

---

### 🐛 Xử lý sự cố

| Lỗi | Nguyên nhân | Cách khắc phục |
|-----|-------------|----------------|
| `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR` | Thiếu `trust proxy` | Đã sửa trong code mới – hãy kéo về |
| 429 Too Many Requests | Vượt quá 200 yêu cầu/phút | Giảm tần suất hoặc nâng cấp gói |
| Timeout | Payload quá lớn | Chia nhỏ payload hoặc sử dụng hàng đợi |
| 0 cards found | Định dạng không được nhận diện | Kiểm tra lại định dạng, thử chế độ heuristic |

---

### 📄 Giấy phép

MIT
```
