# /share 跨裝置剪貼簿功能設計規格

**日期：** 2026-05-21
**專案：** thsr-bot
**路由：** `/share`

---

## 1. 問題陳述

用戶需要在電腦與手機之間快速互傳文字或網址。目前多數人用 LINE 個人聊天室，導致聊天紀錄雜亂且有隱私顧慮。本功能提供一個極簡的配對式即時同步頁面，無需帳號，12 小時後自動銷毀。

---

## 2. 架構決策

### 2.1 為何不用 Flask 記憶體 dict

`thsr-bot` 部署在 Vercel Serverless 環境，模組層級的 Python dict 無法跨請求可靠持久化。不同裝置的請求可能落在不同容器實例，導致同步失效。

### 2.2 採用方案：Supabase 直連（現有帳號）

| 層級 | 負責方 | 說明 |
|------|--------|------|
| 頁面路由 `/share` | Vercel（thsr-bot Flask） | 只回傳 HTML，零後端邏輯 |
| 資料讀寫 | Supabase REST API（前端直連） | 現有帳號，免費額度足夠 |
| 即時同步 | Supabase Realtime WebSocket | 推播取代輪詢 |

Flask 端改動極小，只需新增一個 route。

---

## 3. Supabase 資料表

### 3.1 DDL

```sql
create table share_rooms (
  room_code  text primary key,
  text       text default '',
  updated_at timestamptz default now()
);

-- 開啟 Realtime
alter publication supabase_realtime add table share_rooms;

-- 自動更新 updated_at
create or replace function update_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

create trigger set_updated_at
before update on share_rooms
for each row execute function update_updated_at();
```

### 3.2 Row Level Security

```sql
alter table share_rooms enable row level security;

create policy "public read"  on share_rooms for select using (true);
create policy "public write" on share_rooms for all    using (true) with check (true);
```

### 3.3 TTL 清理策略

不使用 cron job。每次 upsert 文字時，前端同時執行：

```sql
DELETE FROM share_rooms WHERE updated_at < now() - interval '12 hours'
```

---

## 4. 房間碼邏輯

1. 用戶點「建立新房間」
2. 前端生成：`Math.floor(1000 + Math.random() * 9000)`（4 位數，1000–9999）
3. 查詢 Supabase 確認碼未被占用，最多重試 5 次
4. INSERT 建立房間，顯示碼
5. 對方輸入同碼 → SELECT 驗證存在 → 加入 Realtime 訂閱
6. 若房間碼不存在 → 顯示行內錯誤提示「找不到此房間，請確認號碼」，輸入框抖動動畫

---

## 5. 前端同步機制

### 5.1 寫端：Debounce 300ms

```js
let debounceTimer;
textarea.addEventListener('input', () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => pushText(), 300);
});

async function pushText() {
  await supabase.from('share_rooms').upsert({
    room_code: currentRoom,
    text: textarea.value
  });
  await supabase.from('share_rooms')
    .delete()
    .lt('updated_at', new Date(Date.now() - 43200000).toISOString());
}
```

### 5.2 讀端：Supabase Realtime 訂閱

```js
function subscribeRoom(code) {
  supabase.channel(`room:${code}`)
    .on('postgres_changes', {
      event: 'UPDATE',
      schema: 'public',
      table: 'share_rooms',
      filter: `room_code=eq.${code}`
    }, payload => {
      if (document.activeElement !== textarea) {
        textarea.value = payload.new.text;
      }
    })
    .subscribe();
}
```

防閃爍：只在用戶未聚焦 textarea 時套用遠端文字，避免游標跳位。

---

## 6. UI 設計規格

### 6.1 頁面狀態

**狀態 A：配對畫面**（進入頁面預設）

- 大字顯示本機房間碼（如 `8824`）
- 「🔄 換一個」按鈕重新生成
- 分隔線 + 輸入框供對方加入
- 「加入房間」按鈕

**狀態 B：同步畫面**（配對成功後）

- 頂部顯示「房間 8824 ● 已連線」
- 滿版 textarea（無邊框，自動 focus）
- 底部操作列：「📋 複製」「🗑️ 清空」

### 6.2 雙層毛玻璃

```css
.glass-card {
  border-radius: 24px;
  backdrop-filter: blur(40px) saturate(200%);
  -webkit-backdrop-filter: blur(40px) saturate(200%);
  background: rgba(255,255,255,0.18);
  border: 1px solid rgba(255,255,255,0.35);
  box-shadow: 0 8px 32px rgba(0,0,0,0.08);
}
.glass-inner {
  border-radius: 20px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  background: rgba(255,255,255,0.10);
}
```

### 6.3 彈簧阻尼按鈕（Active 0.965x）

```js
function addSpring(el) {
  el.addEventListener('pointerdown', () => {
    el.style.transition = 'transform 0.1s cubic-bezier(0.34,1.56,0.64,1)';
    el.style.transform  = 'scale(0.965)';
    navigator.vibrate?.([8]);
  });
  ['pointerup','pointerleave','pointercancel'].forEach(ev =>
    el.addEventListener(ev, () => {
      el.style.transition = 'transform 0.55s cubic-bezier(0.34,1.56,0.64,1)';
      el.style.transform  = 'scale(1)';
    })
  );
}
document.querySelectorAll('button').forEach(addSpring);
```

### 6.4 一鍵複製反饋

```js
copyBtn.addEventListener('click', async () => {
  await navigator.clipboard.writeText(textarea.value);
  copyBtn.textContent = '✅ 已複製';
  setTimeout(() => copyBtn.textContent = '📋 複製', 1800);
});
```

### 6.5 配色

沿用現有 CSS 變數（`--bg`、`--surface`、`--text`、`--sub`、`--radius-l`），自動支援深色模式。

深色模式下毛玻璃需覆寫：

```css
@media (prefers-color-scheme: dark) {
  .glass-card {
    background: rgba(255,255,255,0.07);
    border-color: rgba(255,255,255,0.12);
  }
  .glass-inner {
    background: rgba(255,255,255,0.04);
  }
}
```

### 6.6 Supabase JS 載入方式

`share.html` 透過 CDN 載入 supabase-js v2，不需要 npm 或打包工具：

```html
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
```

使用現有 `config.js` 已宣告的 `SUPA_URL` 與 `SUPA_KEY` 常數初始化 client。

---

## 7. Flask 路由（thsr-bot/app.py）

```python
@app.route("/share")
def share():
    return render_template("share.html")
```

---

## 8. 首頁整合（home.html）

在現有功能卡片列表末尾新增：

```html
<a class="card" href="/share">
  <div class="card-icon">📋</div>
  <div class="card-body">
    <div class="card-title">跨裝置剪貼簿</div>
    <div class="card-sub">4碼配對，秒級同步</div>
  </div>
  <div class="card-arrow">›</div>
</a>
```

---

## 9. 實作範圍摘要

| 檔案 | 變動類型 | 說明 |
|------|---------|------|
| `app.py` | 新增 2 行 | `/share` route |
| `templates/share.html` | 新建 | 完整頁面邏輯 |
| `templates/home.html` | 微調 | 新增入口卡片 |
| Supabase | 新建資料表 | `share_rooms`（手動執行 DDL） |

不需要新增任何 Python 套件，不需要新建 Vercel 或 Zeabur 服務。
