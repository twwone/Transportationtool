/* config.js — 多用戶設定檔管理（LocalStorage 方案）*/
const CFG_KEY = 'thsr_cfg_v1';

const Config = (() => {
  function _read() {
    try { return JSON.parse(localStorage.getItem(CFG_KEY)); }
    catch { return null; }
  }
  function _write(data) {
    localStorage.setItem(CFG_KEY, JSON.stringify(data));
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
    getAll() { return _bootstrap(); },

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
    }
  };
})();
