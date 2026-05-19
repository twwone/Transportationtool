/* config.js — 多用戶設定檔管理（LocalStorage + Supabase 雲端同步）*/
const CFG_KEY  = 'thsr_cfg_v1';
const SUPA_URL = 'https://bqapzqdfgnoghtgdakdw.supabase.co';
const SUPA_KEY = 'sb_publishable_5Gw7rYaKnI3_fzcNpLbmwA_h0CgB7Fv';

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

  /* 首次使用：遷移舊版散落 key 並建立預設 profile */
  function _bootstrap() {
    const existing = _read();
    if (existing && existing.profiles) return existing;
    const data = {
      active: 'default',
      profiles: {
        default: {
          id: 'default',
          name: '我',
          thsr: { origin: '台北', destination: '左營', seatType: '1', discount: '全票', interval: '30' },
          mrt:  { starred: localStorage.getItem('mrt_starred') || '' },
          schedule: { airasiaOnly: true },
          tg: {
            token:  localStorage.getItem('tg_token')   || '',
            chatId: localStorage.getItem('tg_chat_id') || ''
          },
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

    /* patch: { thsr?: {…}, mrt?: {…}, schedule?: {…}, tg?: {…} } */
    save(patch) {
      const d = _bootstrap();
      const p = d.profiles[d.active];
      if (!p) return;
      for (const k of Object.keys(patch)) p[k] = { ...p[k], ...patch[k] };
      p.updatedAt = new Date().toISOString();
      _write(d);
    },

    create(name) {
      const d  = _bootstrap();
      const id = 'u' + Date.now();
      d.profiles[id] = {
        id, name,
        thsr: { origin: '台北', destination: '左營', seatType: '1', discount: '全票', interval: '30' },
        mrt:  { starred: '' },
        schedule: { airasiaOnly: true },
        tg: { token: '', chatId: '' },
        createdAt: new Date().toISOString()
      };
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

    /* 推送指定 profile 到 Supabase；若尚無 syncKey 則自動產生 */
    async push(id) {
      const d   = _bootstrap();
      const pid = id || d.active;
      const p   = d.profiles[pid];
      if (!p) return { ok: false };
      if (!p.syncKey) {
        p.syncKey = Math.random().toString(36).slice(2, 10);
        _write(d);
      }
      try {
        const r = await fetch(`${SUPA_URL}/rest/v1/user_configs`, {
          method: 'POST',
          headers: _hdr({ 'Prefer': 'resolution=merge-duplicates,return=minimal' }),
          body: JSON.stringify({ sync_key: p.syncKey, config: p, updated_at: new Date().toISOString() })
        });
        return { ok: r.ok, syncKey: p.syncKey };
      } catch { return { ok: false }; }
    },

    /* 從 Supabase 拉取並合併到本地（已有同碼的 profile 覆蓋；新的則新增） */
    async pull(syncKey) {
      try {
        const r = await fetch(
          `${SUPA_URL}/rest/v1/user_configs?sync_key=eq.${encodeURIComponent(syncKey)}&select=config`,
          { headers: _hdr() }
        );
        if (!r.ok) return { ok: false };
        const rows = await r.json();
        if (!rows.length) return { ok: false, error: '找不到此同步碼' };
        const remote = rows[0].config;
        const d = _bootstrap();
        const found = Object.values(d.profiles).find(p => p.syncKey === syncKey);
        if (found) {
          d.profiles[found.id] = { ...remote, id: found.id };
          d.active = found.id;
        } else {
          const nid = 'u' + Date.now();
          d.profiles[nid] = { ...remote, id: nid };
          d.active = nid;
        }
        _write(d);
        return { ok: true };
      } catch { return { ok: false }; }
    },

    /* 移除 profile 的 syncKey（停用雲端同步，不刪除雲端資料） */
    clearSync(id) {
      const d   = _bootstrap();
      const pid = id || d.active;
      const p   = d.profiles[pid];
      if (p) { delete p.syncKey; _write(d); }
    }
  };
})();
