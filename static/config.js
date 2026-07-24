/* config.js — 多用戶設定檔管理（LocalStorage + Supabase 雲端同步）*/
const CFG_KEY  = 'thsr_cfg_v1';
const SUPA_URL = 'https://bqapzqdfgnoghtgdakdw.supabase.co';
const SUPA_KEY = 'sb_publishable_5Gw7rYaKnI3_fzcNpLbmwA_h0CgB7Fv';

const _PROFILE_DEFAULTS = () => ({
  thsr:     { origin: '台北', destination: '左營', seatType: '1', discount: '全票', interval: '30' },
  mrt:      { starred: '' },
  schedule: { airasiaOnly: true },
  tg:       { token: '', chatId: '' },
});

const Config = (() => {
  function _read() {
    try { return JSON.parse(localStorage.getItem(CFG_KEY)); }
    catch { return null; }
  }
  function _write(data) {
    localStorage.setItem(CFG_KEY, JSON.stringify(data));
  }
  function _hdr(extra = {}) {
    return {
      'Content-Type': 'application/json',
      'apikey': SUPA_KEY,
      'Authorization': `Bearer ${SUPA_KEY}`,
      ...extra
    };
  }

  function _bootstrap() {
    const existing = _read();
    if (existing && existing.profiles) {
      // 已登入但 syncKey 還沒綁定時自動補上
      try {
        const sess = JSON.parse(localStorage.getItem('userSession') || 'null');
        if (sess?.code) {
          const p = existing.profiles[existing.active] || Object.values(existing.profiles)[0];
          if (p && !p.syncKey) { p.syncKey = sess.code; _write(existing); }
        }
      } catch {}
      return existing;
    }
    const data = {
      active: 'default',
      profiles: {
        default: {
          id: 'default',
          name: '我',
          ..._PROFILE_DEFAULTS(),
          mrt: { starred: localStorage.getItem('mrt_starred') || '' },
          tg:  { token: localStorage.getItem('tg_token') || '', chatId: localStorage.getItem('tg_chat_id') || '' },
          createdAt: new Date().toISOString()
        }
      }
    };
    _write(data);
    return data;
  }

  return {
    getAll()    { return _bootstrap(); },

    getActive() {
      const d = _bootstrap();
      return d.profiles[d.active] || Object.values(d.profiles)[0];
    },

    setActive(id) {
      const d = _bootstrap();
      if (d.profiles[id]) { d.active = id; _write(d); }
    },

    /* patch: { thsr?: {…}, mrt?: {…}, schedule?: {…}, tg?: {…}, homeOrder?: [...] }
     * 物件值走淺層合併（保留其他子欄位），陣列/字串等非物件值直接覆蓋
     * （不能對陣列做 {...} 展開，那會把它拆成 {0:.., 1:..} 變成物件）。 */
    save(patch) {
      const d = _bootstrap();
      const p = d.profiles[d.active];
      if (!p) return;
      for (const k of Object.keys(patch)) {
        const v = patch[k];
        const isPlainObj = v && typeof v === 'object' && !Array.isArray(v);
        p[k] = isPlainObj ? { ...p[k], ...v } : v;
      }
      p.updatedAt = new Date().toISOString();
      _write(d);
    },

    create(name) {
      const d  = _bootstrap();
      const id = 'u' + Date.now();
      d.profiles[id] = { id, name, ..._PROFILE_DEFAULTS(), createdAt: new Date().toISOString() };
      d.active = id;
      _write(d);
      return d.profiles[id];
    },

    del(id) {
      const d = _bootstrap();
      if (Object.keys(d.profiles).length <= 1) return false;
      delete d.profiles[id];
      if (d.active === id) d.active = Object.keys(d.profiles)[0];
      _write(d);
      return true;
    },

    exportB64() {
      const raw = localStorage.getItem(CFG_KEY) || '{}';
      return btoa(unescape(encodeURIComponent(raw)));
    },

    importB64(b64) {
      try {
        const raw = decodeURIComponent(escape(atob(b64.trim())));
        const obj = JSON.parse(raw);
        if (!obj.profiles) throw 0;
        localStorage.setItem(CFG_KEY, raw);
        return true;
      } catch { return false; }
    },

    /* ── 雲端同步 ── */

    async push(id) {
      const d   = _bootstrap();
      const pid = id || d.active;
      const p   = d.profiles[pid];
      if (!p || !p.syncKey) return { ok: false };
      const dietSyncId = localStorage.getItem('dietSyncId');
      if (dietSyncId) { p.dietSyncId = dietSyncId; _write(d); }
      try {
        const r = await fetch(`${SUPA_URL}/rest/v1/user_configs`, {
          method: 'POST',
          headers: _hdr({ 'Prefer': 'resolution=merge-duplicates,return=minimal' }),
          body: JSON.stringify({ sync_key: p.syncKey, config: p, updated_at: new Date().toISOString() })
        });
        return { ok: r.ok, syncKey: p.syncKey };
      } catch { return { ok: false }; }
    },

    async pull(syncKey) {
      try {
        const r = await fetch(
          `${SUPA_URL}/rest/v1/user_configs?sync_key=eq.${encodeURIComponent(syncKey)}&select=config`,
          { headers: _hdr() }
        );
        if (!r.ok) return { ok: false };
        const rows = await r.json();
        if (!rows.length) return { ok: false, error: '找不到此號碼' };
        const remote = rows[0].config;
        // 確保 mrt/thsr 等子物件一定存在（遠端若是新帳號只有 name/code）
        const merged = { ..._PROFILE_DEFAULTS(), ...remote, syncKey };
        const d = _bootstrap();
        const found = Object.values(d.profiles).find(p => p.syncKey === syncKey);
        if (found) {
          d.profiles[found.id] = { ...merged, id: found.id };
          d.active = found.id;
        } else {
          const nid = 'u' + Date.now();
          d.profiles[nid] = { ...merged, id: nid };
          d.active = nid;
        }
        _write(d);
        return { ok: true };
      } catch { return { ok: false }; }
    },

    /* 登入後呼叫：把帳號號碼設為 syncKey
     * isNewAccount=true → 先 push 本地設定（避免 pull 回空資料蓋掉本機設定）
     * isNewAccount=false → 直接 pull 遠端設定（雲端為主） */
    async connectAccount(code, isNewAccount = false) {
      const d = _bootstrap();
      const p = d.profiles[d.active];
      if (!p) return;
      p.syncKey = code;
      _write(d);
      if (isNewAccount) {
        await this.push();
      } else {
        await this.pull(code);
      }
    },

    /* 登出時解除帳號綁定 */
    disconnectAccount() {
      const d = _bootstrap();
      const p = d.profiles[d.active];
      if (p) { delete p.syncKey; _write(d); }
    },

    clearSync(id) {
      const d   = _bootstrap();
      const pid = id || d.active;
      const p   = d.profiles[pid];
      if (p) { delete p.syncKey; _write(d); }
    }
  };
})();
