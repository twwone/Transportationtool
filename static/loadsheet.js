const AC = {
  /* cargoHolds：貨物可選的艙位 */
  A320: { cargoHolds: ['HOLD3'], holds: [
    { id:'HOLD1', label:'HOLD 1', tag:'FWD',  badge:'',    maxPcs:80, maxKg:3402 },
    { id:'HOLD3', label:'HOLD 3', tag:'AFT',  badge:'aft', maxPcs:80, maxKg:2426 },
    { id:'HOLD4', label:'HOLD 4', tag:'AFT',  badge:'aft', maxPcs:40, maxKg:2110 },
    { id:'HOLD5', label:'HOLD 5', tag:'BULK', badge:'bulk',maxPcs:0,  maxKg:1497, nodist:true },
  ]},
  /* fillOrder：計算建議裝載時的填艙順序（沒設就照 holds 順序）；softPcs：優先只裝到這個件數；HOLD5 不分配，塞小件 */
  A321: { fillOrder: ['HOLD3','HOLD4','HOLD2','HOLD1'], cargoHolds: ['HOLD2','HOLD3'], holds: [
    { id:'HOLD1', label:'HOLD 1', tag:'FWD',  badge:'',    maxPcs:50, maxKg:2202 },
    { id:'HOLD2', label:'HOLD 2', tag:'FWD',  badge:'',    maxPcs:90, softPcs:80, maxKg:3468 },
    { id:'HOLD3', label:'HOLD 3', tag:'AFT',  badge:'aft', maxPcs:90, softPcs:80, maxKg:3587 },
    { id:'HOLD4', label:'HOLD 4', tag:'AFT',  badge:'aft', maxPcs:50, maxKg:2083 },
    { id:'HOLD5', label:'HOLD 5', tag:'BULK', badge:'bulk',maxPcs:0,  maxKg:800,  nodist:true },
  ]}
};

const STORE_KEY   = 'loadsheet_v1';
/* Notion CGO：選航班時記下這班有沒有貨，NIL 時貨物欄顯示 NIL（仍可修改） */
let _cgo = null;   // { flight, status: 'YES' | 'NIL' | '' }
function applyCgo() {
  const badge = document.getElementById('cgoBadge');
  const st = _cgo ? _cgo.status : '';
  badge.hidden = !_cgo;
  if (_cgo) {
    badge.className = 'cgo-badge ' + (st === 'YES' ? 'yes' : 'nil');
    badge.textContent = (st === 'YES' ? 'CGO' : st === 'NIL' ? 'NIL' : 'NO INFO') + ' · ' + _cgo.flight.split('/')[0];
  }
  ['cargo-pcs','cargo-kg'].forEach(id => { document.getElementById(id).placeholder = st === 'NIL' ? 'NIL' : '0'; });
}
function setCgoFromFlight(flight, raw) {
  const v = String(raw || '').trim().toUpperCase();
  const status = /^(Y|YES)\b/.test(v) ? 'YES' : /^(NIL|NO|N|NONE|0)$/.test(v) ? 'NIL' : '';
  _cgo = { flight, status };
  if (status === 'NIL') ['cargo-hold','cargo-pcs','cargo-kg'].forEach(id => { document.getElementById(id).value = ''; });
  applyCgo();
  onSpecialChange();
}
const SYNCID_KEY  = 'loadsheet_syncid';
let curAc   = 'A320';
let holdPcs = {};
let _syncDebounce = null;

/* ── 同步碼 ── */
function getSyncId() { return localStorage.getItem(SYNCID_KEY) || ''; }
function setSyncId(id) {
  localStorage.setItem(SYNCID_KEY, id);
  document.getElementById('syncCodeInput').value = id;
}
function genSyncId() {
  return Math.random().toString(36).slice(2,7) + Math.random().toString(36).slice(2,7);
}
function toggleSyncSheet() {
  const s = document.getElementById('syncSheet');
  s.style.display = s.style.display === 'none' ? '' : 'none';
}
function copySyncCode() {
  const code = document.getElementById('syncCodeInput').value.trim();
  if (!code) return;
  navigator.clipboard.writeText(code).then(() => setSyncStatus('Copied'));
}
function onSyncCodeInput() {
  const code = document.getElementById('syncCodeInput').value.trim();
  if (code.length >= 6) {
    setSyncId(code);
    setSyncStatus('Fetching data…');
    pullState();
  }
}
function setSyncStatus(msg) {
  document.getElementById('syncStatus').textContent = msg;
  setTimeout(() => { if (document.getElementById('syncStatus').textContent === msg) document.getElementById('syncStatus').textContent = ''; }, 3000);
}

async function pushState() {
  const id = getSyncId();
  if (!id) return;
  const payload = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
  try {
    await fetch(`/api/loadsheet/state/${id}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  } catch(e) {}
}

async function pullState() {
  const id = getSyncId();
  if (!id) return;
  try {
    const res = await fetch(`/api/loadsheet/state/${id}`);
    const data = await res.json();
    if (data && (data.fields || data.holdPcs)) {
      localStorage.setItem(STORE_KEY, JSON.stringify(data));
      loadState();
      setSyncStatus('Synced');
    } else {
      /* 雲端沒資料：把本機資料推上去當起點 */
      if (localStorage.getItem(STORE_KEY)) pushState();
      setSyncStatus('No cloud data yet');
    }
  } catch(e) { setSyncStatus('Sync failed'); }
}

function schedulePush() {
  clearTimeout(_syncDebounce);
  _syncDebounce = setTimeout(pushState, 1500);
}

/* ── localStorage ── */
function saveState() {
  const fields = ['crew-hold','crew-pcs','crew-kg','toolbox-hold','toolbox-pcs','toolbox-kg',
                  'transit-hold','transit-pcs','transit-kg','trash-hold','trash-pcs','trash-kg',
                  'cargo-hold','cargo-pcs','cargo-kg',
                  'totalPcs','totalKg'];
  const data = { ac: curAc, holdPcs, fields: {}, cgo: _cgo };
  fields.forEach(id => { data.fields[id] = document.getElementById(id)?.value || ''; });
  localStorage.setItem(STORE_KEY, JSON.stringify(data));
  schedulePush();
}

function loadState() {
  try {
    const data = JSON.parse(localStorage.getItem(STORE_KEY));
    if (!data) return;
    if (data.ac && AC[data.ac]) {
      curAc = data.ac;
      document.querySelectorAll('.seg-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.ac === curAc);
      });
    }
    buildHoldOptions();
    if (data.fields) {
      Object.entries(data.fields).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el) el.value = val;
      });
    }
    _cgo = data.cgo || null;
    applyCgo();
    /* 雲端是空的分配（另一台按了 CLEAR）也要蓋掉，不然會留著舊件數 */
    holdPcs = data.holdPcs || {};
    if (Object.keys(holdPcs).length) {
      document.getElementById('resultLabel').textContent = `Suggested distribution · ${curAc}`;
    }
    onBagsInput();
    updateWeightSummary();
  } catch (e) {}
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
document.addEventListener('touchmove', e => { if (e.touches.length > 1) e.preventDefault(); }, { passive:false });
let _lt = 0; document.addEventListener('touchend', e => { const n = Date.now(); if (n - _lt < 300) e.preventDefault(); _lt = n; }, { passive:false });

/* 點進數字框時游標放到最後面，方便直接刪改（數字框右對齊，iOS 點下去游標常停在最前面）。
   type="number" 不能設定游標位置，所以數字框都用 type="text" + inputmode 叫出數字鍵盤 */
function caretToEnd(el) { const n = el.value.length; try { el.setSelectionRange(n, n); } catch (e) {} }
let _caretFresh = null;
document.addEventListener('focusin', e => {
  const el = e.target;
  if (!el.matches || !el.matches('input[inputmode]')) return;
  _caretFresh = el;
  setTimeout(() => caretToEnd(el), 0);
});
document.addEventListener('click', e => {
  if (e.target === _caretFresh) { caretToEnd(e.target); _caretFresh = null; }
});
document.addEventListener('focusout', () => { _caretFresh = null; });

/* ── 機型切換 ── */
document.getElementById('acSwitch').addEventListener('click', e => {
  const btn = e.target.closest('.seg-btn');
  if (!btn) return;
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  curAc = btn.dataset.ac;
  buildHoldOptions();
  if (_ldmParsed && !_ldmParsed.ac) renderLdmParse();  // LDM 看不出機型時，手動切換後重新檢查艙位
  if (document.getElementById('resultSection').style.display !== 'none') calcDistribution();
  saveState();
});

function buildHoldOptions() {
  const holds = AC[curAc].holds;
  ['crew-hold','toolbox-hold','transit-hold','trash-hold','cargo-hold'].forEach(id => {
    const sel = document.getElementById(id);
    const cur = sel.value;
    const opts = id === 'cargo-hold' && AC[curAc].cargoHolds
      ? holds.filter(h => AC[curAc].cargoHolds.includes(h.id))
      : holds;
    sel.innerHTML = '<option value="">— Hold</option>' +
      opts.map(h => `<option value="${h.id}">${h.label}</option>`).join('');
    if (opts.find(h => h.id === cur)) sel.value = cur;
  });
}

/* ── 輔助 ── */
function round1(n) { return Math.round(n * 10) / 10; }
function v(id)     { return parseFloat(document.getElementById(id).value) || 0; }

function avgWeight() {
  const pcs = v('totalPcs'), kg = v('totalKg');
  return pcs ? round1(kg / pcs) : null;
}

/* ── 特殊項目讀取 ── */
function getSpecials() {
  return {
    crew:    { hold: document.getElementById('crew-hold').value,    pcs: v('crew-pcs'),    kg: v('crew-kg')    },
    toolbox: { hold: document.getElementById('toolbox-hold').value, pcs: v('toolbox-pcs'), kg: v('toolbox-kg') },
    transit: { hold: document.getElementById('transit-hold').value, pcs: v('transit-pcs'), kg: v('transit-kg') },
    trash:   { hold: document.getElementById('trash-hold').value,   pcs: v('trash-pcs'),   kg: v('trash-kg')   },
    cargo:   { hold: document.getElementById('cargo-hold').value,   pcs: v('cargo-pcs'),   kg: v('cargo-kg')   },
  };
}

/* ── 總重合計 ── */
function updateWeightSummary() {
  const sp     = getSpecials();
  const bagsKg = v('totalKg');
  const extraKg = sp.crew.kg + sp.toolbox.kg + sp.trash.kg + sp.cargo.kg; // 轉機不加（已含在出發行李）
  const total   = round1(bagsKg + extraKg);
  const hasAny  = bagsKg > 0 || extraKg > 0;

  if (!hasAny) { document.getElementById('weightSummary').style.display = 'none'; return; }

  document.getElementById('ws-bags').textContent  = bagsKg ? bagsKg + ' KG' : '—';
  document.getElementById('ws-total').textContent = total + ' KG';

  const showRow = (rowId, valId, kg) => {
    const show = kg > 0;
    document.getElementById(rowId).style.display = show ? '' : 'none';
    if (show) document.getElementById(valId).textContent = kg + ' KG';
  };
  showRow('ws-crew-row',    'ws-crew',    sp.crew.kg);
  showRow('ws-toolbox-row', 'ws-toolbox', sp.toolbox.kg);
  showRow('ws-trash-row',   'ws-trash',   sp.trash.kg);
  showRow('ws-cargo-row',   'ws-cargo',   sp.cargo.kg);
  document.getElementById('weightSummary').style.display = '';
}

function onBagsInput() {
  const avg = avgWeight();
  document.getElementById('avgHint').innerHTML = avg != null
    ? `Avg per piece: <b>${avg} KG</b> (incl. transit)` : 'Enter total pieces and weight';
  updateWeightSummary();
  renderResult();
  saveState();
}

function onSpecialChange() {
  updateWeightSummary();
  renderResult();
  saveState();
}

/* ── 計算分配 ── */
function calcDistribution() {
  const totalPcs = v('totalPcs');
  if (!totalPcs) return;

  const sp      = getSpecials();
  const holds   = AC[curAc].holds;
  holdPcs       = {};

  // 1. 轉機行李先固定放好（位置不動）
  if (sp.transit.hold && sp.transit.pcs > 0) {
    holdPcs[sp.transit.hold] = sp.transit.pcs;
  }

  // 2. 剩下的出發行李依優先順序填（不含 nodist，排除已被轉機固定的件數）
  // 轉機件數已含在 totalPcs 內，只有在艙位+件數都填好時才從 remaining 扣除
  const transitFixed = sp.transit.hold && sp.transit.pcs > 0 ? sp.transit.pcs : 0;
  let remaining = totalPcs - transitFixed;
  const fillOrder = AC[curAc].fillOrder;
  const fillHolds = fillOrder
    ? fillOrder.map(id => holds.find(h => h.id === id))
    : holds.filter(h => !h.nodist);
  /* 第一輪先裝到建議件數（softPcs，沒設就是上限），全部艙都裝滿還有剩，第二輪才補到上限 maxPcs */
  const passes = [h => h.softPcs ?? h.maxPcs, h => h.maxPcs];
  for (const cap of passes) {
    if (remaining <= 0 && cap !== passes[0]) break;
    for (const h of fillHolds) {
      const alreadyFilled = holdPcs[h.id] || 0;
      const available     = cap(h) - alreadyFilled;
      if (available <= 0) continue;
      const fill = Math.min(remaining, available);
      holdPcs[h.id] = alreadyFilled + fill;
      remaining -= fill;
      if (remaining <= 0) break;
    }
  }

  document.getElementById('resultLabel').textContent = `Suggested distribution · ${curAc}`;
  renderResult();
  saveState();
}

/* ── 渲染結果 ── */
function renderResult() {
  const avg      = avgWeight() || 0;
  const totalPcs = v('totalPcs');
  const assigned = Object.values(holdPcs).reduce((a, b) => a + b, 0);
  const remain   = totalPcs - assigned;
  const sp       = getSpecials();

  /* banner */
  const banner = document.getElementById('remainBanner');
  const val    = document.getElementById('remainVal');
  val.textContent  = remain === 0 ? '0 PCS ✓' : remain > 0 ? `+${remain} PCS` : `${remain} PCS`;
  banner.className = 'remain-banner' + (remain === 0 ? ' ok' : ' warn');
  val.className    = 'remain-val'    + (remain === 0 ? ' ok' : ' warn');

  const list = document.getElementById('holdList');
  list.innerHTML = '';

  /* 件數全部分配完時，最後一個有行李的艙（非 nodist）用總重減法算，吸收四捨五入誤差；
     還有未分配件數時一律件數 × 平均重，避免把沒分配的重量算進最後一艙 */
  const totalKg     = v('totalKg');
  /* 轉機行李單獨放一艙（該艙件數剛好等於轉機件數，沒混一般行李）：那艙行李重量用轉機實重，
     其他艙改用扣掉轉機後的出發行李平均重；有混到一般行李就照原本平均重算 */
  const tHold        = sp.transit.hold;
  const transitAlone = !!(tHold && sp.transit.pcs > 0 && sp.transit.kg > 0 && (holdPcs[tHold] || 0) === sp.transit.pcs);
  const restPcs      = totalPcs - sp.transit.pcs;
  const bagAvg       = transitAlone && restPcs > 0 && avg ? round1((totalKg - sp.transit.kg) / restPcs) : avg;
  const activeHolds = AC[curAc].holds.filter(h => !h.nodist && (holdPcs[h.id] || 0) > 0 && !(transitAlone && h.id === tHold));
  const lastHold    = remain === 0 ? activeHolds[activeHolds.length - 1] : null;
  let   usedKg      = 0;   // 前面各艙已算出的重量加總

  /* 先算非最後一艙的重量，記錄加總 */
  const holdKg = {};
  AC[curAc].holds.forEach(h => {
    const pcs = holdPcs[h.id] || 0;
    if (!pcs) { holdKg[h.id] = 0; return; }
    if (lastHold && h.id === lastHold.id) return; // 最後一艙稍後算
    if (transitAlone && h.id === tHold) { holdKg[h.id] = sp.transit.kg; usedKg += sp.transit.kg; return; }
    const kg = bagAvg ? Math.round(pcs * bagAvg) : 0;
    holdKg[h.id] = kg;
    usedKg += kg;
  });
  /* 最後一艙 = 總重 − 前面加總 */
  if (lastHold) holdKg[lastHold.id] = avg ? Math.max(0, totalKg - usedKg) : 0;

  AC[curAc].holds.forEach((h, i) => {
    const pcs     = holdPcs[h.id] || 0;
    const kg      = holdKg[h.id] ?? 0;
    /* 該艙特殊項目（轉機已含在行李件數內，不另外加） */
    const spItems = [
      { label:'Crew',    s: sp.crew    },
      { label:'Toolbox', s: sp.toolbox },
      { label:'Garbage', s: sp.trash   },
      { label:'Cargo',   s: sp.cargo   },
    ].filter(x => x.s.hold === h.id && x.s.kg > 0);
    const spKg    = round1(spItems.reduce((sum, x) => sum + x.s.kg, 0));
    const holdTotal = round1(kg + spKg);
    const overPcs = pcs > h.maxPcs && h.maxPcs > 0;
    /* 貨物不設上限：超重判斷不算貨物重量 */
    const overKg  = holdTotal - (sp.cargo.hold === h.id ? sp.cargo.kg : 0) > h.maxKg;
    const hasTransit = sp.transit.hold === h.id && sp.transit.pcs > 0;

    /* 特殊項目 tags */
    const tagItems = [
      sp.crew.hold    === h.id && (sp.crew.pcs    > 0 || sp.crew.kg    > 0) ? `<span class="extra-tag">Crew ${sp.crew.pcs||'—'} pcs / ${sp.crew.kg||'—'} KG</span>` : '',
      sp.toolbox.hold === h.id && (sp.toolbox.pcs > 0 || sp.toolbox.kg > 0) ? `<span class="extra-tag">Toolbox ${sp.toolbox.pcs||'—'} pcs / ${sp.toolbox.kg||'—'} KG</span>` : '',
      hasTransit ? `<span class="extra-tag transit">Transit ${sp.transit.pcs} pcs${sp.transit.kg ? ' / '+sp.transit.kg+' KG' : ''} (fixed)</span>` : '',
      sp.trash.hold === h.id ? `<span class="extra-tag">Garbage${sp.trash.pcs > 0 ? ' '+sp.trash.pcs+' pcs' : ''}${sp.trash.kg ? ' / '+sp.trash.kg+' KG' : ''}</span>` : '',
      sp.cargo.hold === h.id && sp.cargo.kg > 0 ? `<span class="extra-tag cargo">Cargo ${sp.cargo.kg} KG</span>` : '',
    ].filter(Boolean).join('');

    const inputCls = (overPcs || overKg) ? 'err' : h.badge === 'aft' || h.badge === 'bulk' ? 'aft' : '';
    const barCls   = h.badge === 'aft' || h.badge === 'bulk' ? 'bar-aft' : 'bar-fwd';

    const card = document.createElement('div');
    card.className = 'hold-card' + (hasTransit ? ' fixed-transit' : '');
    card.style.animationDelay = (i * 0.05) + 's';
    card.innerHTML = `
      <div class="hold-top">
        <div class="hold-badge ${h.badge}">${h.label}</div>
        <div class="hold-tag">${h.tag}</div>
        <div class="hold-max"></div>
      </div>
      <div class="hold-body">
        <div class="hold-input-wrap">
          <div class="hold-input-label">PCS</div>
          <input class="hold-input ${inputCls}" type="text" inputmode="numeric"
            value="${pcs || ''}" placeholder="0" data-hold="${h.id}"
            onchange="onHoldInput(this)">
        </div>
        ${(()=>{
          const hasBags = pcs && avg;
          if (!hasBags && !spKg) return '<div class="hold-calc"></div>';
          /* 總式：行李 + 各特殊項目 */
          const terms = [hasBags ? kg : null, ...spItems.map(x => x.s.kg)].filter(t => t != null);
          const sumLine = terms.length > 1 ? `<div class="hold-calc-sub">= ${terms.join(' + ')}</div>` : '';
          /* 明細：行李怎麼算、每項特殊項目各多少 */
          const bagLine = hasBags
            ? (transitAlone && h.id === tHold
                ? `Transit actual ${kg}`
                : lastHold && h.id === lastHold.id
                  ? `Bags ${totalKg}−${usedKg} = ${kg}`
                  : `Bags ${pcs}×${bagAvg} = ${kg}`)
            : '';
          const spLines = spItems.map(x => x.s === sp.cargo ? `<span class="cargo-line">${x.label} ${x.s.kg}</span>` : `${x.label} ${x.s.kg}`);
          const detail = [bagLine, ...spLines].filter(Boolean).join('<br>');
          return `<div class="hold-calc">
            <div class="hold-calc-kg">${holdTotal} KG</div>
            ${sumLine}
            <div class="hold-calc-special">${detail}</div>
          </div>`;
        })()}
      </div>
      ${tagItems ? `<div class="hold-extras">${tagItems}</div>` : ''}
    `;
    list.appendChild(card);
  });
}

function onHoldInput(el) {
  const holdId = el.dataset.hold;
  holdPcs[holdId] = parseFloat(el.value) || 0;
  const h = AC[curAc].holds.find(x => x.id === holdId);
  const avg = avgWeight() || 0;
  const overPcs = h && h.maxPcs > 0 && holdPcs[holdId] > h.maxPcs;
  const overKg  = h && avg && round1(holdPcs[holdId] * avg) > h.maxKg;
  el.className = 'hold-input ' + (overPcs || overKg ? 'err' : h.badge === 'aft' ? 'aft' : '');
  renderResult();
  saveState();
}

/* ── 清除（雙確認）── */
let _clearPending = false, _clearTimer = null;
function clearAll() {
  if (!_clearPending) {
    _clearPending = true;
    const btn = document.getElementById('clearBtn');
    btn.textContent = 'Confirm?';
    btn.style.background = 'rgba(255,59,48,0.15)';
    btn.style.color = '#ff3b30';
    _clearTimer = setTimeout(() => {
      _clearPending = false;
      btn.textContent = 'CLEAR';
      btn.style.background = '';
      btn.style.color = '';
    }, 3000);
    return;
  }
  clearTimeout(_clearTimer);
  _clearPending = false;

  /* 清除所有欄位 */
  ['crew-hold','crew-pcs','crew-kg','toolbox-hold','toolbox-pcs','toolbox-kg',
   'transit-hold','transit-pcs','transit-kg','trash-hold','trash-pcs','trash-kg',
                  'cargo-hold','cargo-pcs','cargo-kg',
   'totalPcs','totalKg'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  holdPcs = {};
  _cgo = null;
  applyCgo();
  document.getElementById('avgHint').textContent = 'Enter total pieces and weight';
  document.getElementById('weightSummary').style.display = 'none';
  localStorage.removeItem(STORE_KEY);
  renderResult();
  /* 雲端也清空，避免重新整理又被拉回舊資料 */
  clearTimeout(_syncDebounce);
  pushState();

  const btn = document.getElementById('clearBtn');
  btn.textContent = 'CLEAR';
  btn.style.background = '';
  btn.style.color = '';
  _chkActive = false;
  chkClearMarks();
}

/* ── 圖片 Modal ── */
let _slCount = 0, _slY = 0;
/* 逐一設定/清除屬性：瀏覽器會把 cssText 改寫成 "position: fixed;" 這種格式，用 regex 清會比對不到 */
const _SL_PROPS = ['position','top','left','right','width'];
function lockScroll()   { if(_slCount++>0)return; _slY=window.scrollY; Object.assign(document.body.style,{position:'fixed',top:`-${_slY}px`,left:'0',right:'0',width:'100%'}); }
function unlockScroll() { if(_slCount<=0)return; if(--_slCount>0)return; _SL_PROPS.forEach(p => document.body.style.removeProperty(p)); window.scrollTo(0,_slY); }
function openImgModal(url, title) {
  lockScroll();
  document.getElementById('imgModalImg').src = url;
  document.getElementById('imgModalTitle').textContent = title || '';
  document.getElementById('imgModalOverlay').classList.add('open');
}
function closeImgModal() {
  document.getElementById('imgModalOverlay').classList.remove('open');
  unlockScroll();
}

/* ── 落地 LDM ── */
let _ldmDate = '';  // 空字串 = 今天

/* 日期一律用台灣時間（跟後端 _asiaair_today 一致），不用 toISOString 的 UTC 日期 */
function ldmShiftDate(dateStr, days) {
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}
function ldmDateStr(offset = 0) {
  const today = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Taipei' });
  return ldmShiftDate(today, offset);
}
function ldmOffsetFromToday() {
  if (!_ldmDate) return 0;
  const today = ldmDateStr(0);
  const diff  = (new Date(_ldmDate) - new Date(today)) / 86400000;
  return Math.round(diff);
}
/* 2026-10-23 → 23OCT2026（航空常用格式） */
function ldmFormatDate(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return d + ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][+m - 1] + y;
}
function updateLdmDateLabel() {
  const offset = ldmOffsetFromToday();
  document.getElementById('ldmDateLabel').textContent = ldmFormatDate(_ldmDate || ldmDateStr(0));
  document.getElementById('ldmNextBtn').disabled = offset >= 1;
}
function ldmPrevDay() {
  _ldmDate  = ldmShiftDate(_ldmDate || ldmDateStr(0), -1);
  closeLdm();
  loadLdmFlights();
}
function ldmNextDay() {
  const next = ldmShiftDate(_ldmDate || ldmDateStr(0), 1);
  if (next > ldmDateStr(1)) return;   // 最多看到明天
  _ldmDate = next;
  closeLdm();
  loadLdmFlights();
}

async function loadLdmFlights() {
  updateLdmDateLabel();
  document.getElementById('ldmFlightList').innerHTML = '<div class="ldm-loading"><i class="bi bi-arrow-clockwise ldm-spin"></i> Loading flights…</div>';
  try {
    const url  = _ldmDate ? `/api/loadsheet/flights?date=${_ldmDate}` : '/api/loadsheet/flights';
    const res  = await fetch(url);
    const data = await res.json();
    renderLdmFlights(data.flights || []);
  } catch(e) {
    document.getElementById('ldmFlightList').innerHTML = '<div class="ldm-no-flights">Failed to load flights</div>';
  }
}

function renderLdmFlights(flights) {
  const el = document.getElementById('ldmFlightList');
  if (!flights.length) {
    el.innerHTML = '<div class="ldm-no-flights">No arrivals for this date</div>';
    return;
  }
  el.innerHTML = flights.map(f => {
    const prefix = f.flight.slice(0,2).toLowerCase();
    const codeCls = ['fd','ak','z2'].includes(prefix) ? prefix : '';
    const sub = [f.origin ? f.origin+'→TPE' : '', f.sta ? 'ARR '+f.sta : '', f.reg ? escHtml(f.reg) : ''].filter(Boolean).join(' · ');
    return `<div class="ldm-flight-item" data-notion="${f.notion_id}" data-flight="${f.flight}" data-cgo="${f.cgo || ''}" onclick="selectLdmFlight(this)">
      <div class="ldm-fl-code ${codeCls}">${f.flight.slice(0,2)}</div>
      <div class="ldm-fl-info">
        <div class="ldm-fl-no">${f.flight}</div>
        <div class="ldm-fl-sub">${sub || '—'}</div>
      </div>
      <i class="bi bi-chevron-right" style="color:var(--sub);font-size:0.8rem"></i>
    </div>`;
  }).join('');
}

async function selectLdmFlight(el) {
  document.querySelectorAll('.ldm-flight-item').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  const notionId = el.dataset.notion;
  const flight   = el.dataset.flight;
  setCgoFromFlight(flight, el.dataset.cgo);

  const content = document.getElementById('ldmContent');
  const items   = document.getElementById('ldmItems');
  content.style.display = '';
  items.innerHTML = '<div class="ldm-loading"><i class="bi bi-arrow-clockwise ldm-spin"></i> Loading Notion data…</div>';
  loadLdmParse(notionId, flight);

  try {
    const res  = await fetch(`/api/asia-airlines/detail/${notionId}`);
    const data = await res.json();
    const list = data.items || [];
    if (!list.length) {
      items.innerHTML = '<div class="ldm-no-flights">No content on the Notion page for this flight</div>';
      return;
    }
    items.innerHTML = list.map(it => {
      if (it.type === 'image') {
        return `<img class="ldm-img" src="${it.url}" alt="${flight} LDM" onclick="openImgModal('${it.url}','${flight} LDM')">`;
      }
      if (it.type === 'text' && it.text) {
        return `<div class="ldm-text-block">${it.text.replace(/</g,'&lt;')}</div>`;
      }
      return '';
    }).join('');
  } catch(e) {
    items.innerHTML = '<div class="ldm-no-flights">Failed to load, please try again later</div>';
  }
}

/* ── LDM 自動辨識：先顯示結果與原文給人核對，按「套用」才寫進特殊項目 ── */
const LDM_ITEMS = [
  { key:'crew',    label:'Crew bag' },
  { key:'toolbox', label:'Toolbox'  },
  { key:'transit', label:'Transit bag' },
  { key:'trash',   label:'Garbage'  },
];
let _ldmParsed = null;   // { data, ac, flight }，ac 由 LDM 機號／座位數判斷，判斷不出來是空字串
let _ldmParseSeq = 0;    // 快速切換航班時，丟掉舊的回應

function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function loadLdmParse(notionId, flight) {
  const box = document.getElementById('ldmParse');
  const seq = ++_ldmParseSeq;
  _ldmParsed = null;
  box.innerHTML = '<div class="ldm-loading"><i class="bi bi-arrow-clockwise ldm-spin"></i> AI reading onward load (~10 s)…</div>';
  try {
    const res  = await fetch(`/api/loadsheet/parse/${notionId}`);
    const data = await res.json();
    if (seq !== _ldmParseSeq) return;
    if (!res.ok) throw new Error(data.error || 'Recognition failed');
    _ldmParsed = { data, ac: (data.aircraft && AC[data.aircraft.type]) ? data.aircraft.type : '', flight };
    renderLdmParse();
  } catch (e) {
    if (seq !== _ldmParseSeq) return;
    box.innerHTML = `<div class="ldm-parse-empty">Auto-recognition failed: ${escHtml(e.message)}. Check the images below and fill in manually.</div>`;
  }
}

function renderLdmParse() {
  const box = document.getElementById('ldmParse');
  const { data, ac: ldmAc, flight } = _ldmParsed;
  const ac = ldmAc || curAc;   // LDM 看不出機型時，以目前選的機型檢查艙位
  const acft = data.aircraft || {};
  const found = LDM_ITEMS.filter(it => data[it.key]);
  // 186 座是 A320neo、180 座是 A320，艙位設定相同，只差顯示
  const acNote = ldmAc
    ? `${ldmAc === 'A320' && acft.seats === 186 ? 'A320neo' : ldmAc}${acft.reg ? ' · ' + escHtml(acft.reg) : ''}${acft.seats ? ' · Y' + acft.seats : ''}`
    : '<span class="warn">Type unknown</span>';
  const head = `<div class="ldm-parse-head">
      <span class="ldm-parse-title">AI LDM · ${escHtml(flight)}</span>
      <span class="ldm-parse-note">${acNote}${data.onward ? ' · onward ' + escHtml(data.onward) : ''}</span>
    </div>` + (data.ldm_found && !ldmAc
      ? `<div class="ldm-parse-empty">Aircraft type not found in the LDM — select A320 / A321 above manually (holds checked against ${curAc}).</div>`
      : '');

  if (!data.ldm_found) {
    box.innerHTML = head + '<div class="ldm-parse-empty">No LDM found in the attachments — it may not be uploaded yet. Check the images below.</div>';
    return;
  }
  if (!found.length) {
    box.innerHTML = head + '<div class="ldm-parse-empty">No onward load in the LDM (TPE may be the final stop).</div>';
    return;
  }
  const validHolds = AC[ac] ? AC[ac].holds.map(h => h.id) : [];
  const rows = found.map(it => {
    const d = data[it.key];
    const holdOk  = d.hold && validHolds.includes(d.hold);
    const holdTxt = !d.hold ? '<span class="warn">Hold not stated</span>'
                  : holdOk  ? d.hold.replace('HOLD', 'HOLD ')
                  : `<span class="warn">${d.hold} (not on ${ac})</span>`;
    const nums = [d.pcs != null ? d.pcs + ' pcs' : 'pcs n/a', d.kg != null ? d.kg + ' KG' : 'kg n/a'].join(' / ');
    return `<div class="ldm-parse-row">
      <div class="ldm-parse-main"><span>${it.label}</span><span>${holdTxt} · ${nums}</span></div>
      ${d.quote ? `<div class="ldm-parse-quote">Source: ${escHtml(d.quote)}</div>` : ''}
    </div>`;
  }).join('');
  box.innerHTML = head + rows +
    `<button class="ldm-apply-btn" onclick="applyLdmParse()">Apply to Special Load (${ldmAc ? 'switch to ' + ldmAc + ', ' : ''}replaces current items)</button>`;
}

function applyLdmParse() {
  if (!_ldmParsed) return;
  const { data, ac } = _ldmParsed;
  const acChanged = AC[ac] && ac !== curAc;
  if (acChanged) {
    curAc = ac;
    document.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b.dataset.ac === curAc));
    buildHoldOptions();
  }
  LDM_ITEMS.forEach(({ key }) => {
    const d = data[key];
    const holdOk = d && d.hold && AC[curAc].holds.some(h => h.id === d.hold);
    document.getElementById(`${key}-hold`).value = holdOk ? d.hold : '';
    document.getElementById(`${key}-pcs`).value  = d && d.pcs != null ? d.pcs : '';
    document.getElementById(`${key}-kg`).value   = d && d.kg  != null ? d.kg  : '';
  });
  onSpecialChange();
  /* 換機型後舊的分配含有新機型沒有的艙位（例如 A321 HOLD2），要跟手動切換一樣重算 */
  if (acChanged) calcDistribution();
  const btn = document.querySelector('.ldm-apply-btn');
  if (btn) btn.textContent = 'Applied — please check Special Load below';
}

function closeLdm() {
  _ldmParseSeq++;
  _ldmParsed = null;
  document.getElementById('ldmContent').style.display = 'none';
  document.querySelectorAll('.ldm-flight-item').forEach(x => x.classList.remove('active'));
}

/* ── 數字鍵盤彈窗：點數字框不叫出系統鍵盤，改跳 FlySmart 樣式的鍵盤。
   每按一鍵就照打字的方式觸發 input 事件；下方各艙件數（onchange）在 Done/Next/關閉時才觸發 change，
   跟原本「打完離開才更新」一樣，不改任何計算函式 ── */
const _kp = { key: null, fresh: false, orig: '' };
function kpFieldOf(target) {
  if (!target || !target.closest) return null;
  const input = target.closest('input[inputmode]');
  if (input) return input;
  const wrap = target.closest('.si-field, .field');
  return wrap ? wrap.querySelector('input[inputmode]') : null;
}
function kpKeyOf(el) { return el.id ? '#' + el.id : `.hold-input[data-hold="${el.dataset.hold}"]`; }
function kpTarget() { return _kp.key ? document.querySelector(_kp.key) : null; }
function kpAnchor(el) { return el.closest('.si-field, .field') || el; }
function kpFields() { return [...document.querySelectorAll('input[inputmode]')].filter(el => el.offsetParent !== null); }
function kpTitleOf(el) {
  if (el.classList.contains('hold-input')) return el.closest('.hold-card').querySelector('.hold-badge').textContent + ' · PCS';
  const unit = (el.closest('.si-field, .field').querySelector('.si-field-label, label') || {}).textContent || '';
  const item = el.closest('.special-item');
  const name = item ? item.querySelector('.row-label').textContent : el.closest('.card').querySelector('.card-title').firstChild.textContent.trim();
  return name + ' · ' + unit;
}
function kpUnitOf(el) {
  if (el.classList.contains('hold-input')) return 'PCS';
  return (el.closest('.si-field, .field').querySelector('.si-field-label, label') || {}).textContent || '';
}

function kpOpen(el) {
  if (!el) return;
  const key = kpKeyOf(el);
  if (_kp.key === key) return;
  if (_kp.key) kpCommit();
  _kp.key = key; _kp.fresh = true; _kp.orig = el.value;
  el.readOnly = true; el.blur();
  kpAnchor(el).classList.add('kp-active');
  el.classList.add('kp-active');
  document.getElementById('kpTitle').textContent = kpTitleOf(el);
  document.getElementById('kpUnit').textContent = kpUnitOf(el);
  document.getElementById('kpOverlay').classList.add('open');
  document.getElementById('kp').classList.add('open');
  kpRender();
  /* 下方空間不夠就先把欄位捲到上面一點 */
  const kpH = document.getElementById('kp').offsetHeight;
  const headerB = document.querySelector('.header').getBoundingClientRect().bottom;
  const r = kpAnchor(el).getBoundingClientRect();
  if (window.innerHeight - r.bottom < kpH + 24) window.scrollBy(0, r.top - headerB - 12);
  kpPlace();
}
function kpPlace() {
  const el = kpTarget();
  if (!el) return;
  const kp = document.getElementById('kp'), arrow = document.getElementById('kpArrow');
  const r = kpAnchor(el).getBoundingClientRect();
  const w = kp.offsetWidth, h = kp.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
  const headerB = document.querySelector('.header').getBoundingClientRect().bottom;
  const below = vh - r.bottom, above = r.top - headerB;
  const up = below >= h + 14 || below >= above;
  let top = up ? r.bottom + 11 : r.top - 11 - h;
  top = Math.max(headerB + 4, Math.min(top, vh - h - 4));
  let left = r.left + r.width / 2 - w / 2;
  left = Math.max(8, Math.min(left, vw - w - 8));
  kp.style.top = top + 'px';
  kp.style.left = left + 'px';
  kp.classList.toggle('up', up);
  kp.classList.toggle('down', !up);
  arrow.style.left = Math.max(12, Math.min(r.left + r.width / 2 - left - 9, w - 30)) + 'px';
}
function kpRender() {
  const el = kpTarget();
  if (!el) return;
  const v = document.getElementById('kpValue');
  v.textContent = el.value || '0';
  v.classList.toggle('empty', !el.value);
}
function kpPress(k) {
  const el = kpTarget();
  if (!el) return;
  if (k === 'done') { kpClose(); return; }
  if (k === 'next') { kpNext(); return; }
  /* 還原鍵：回到打開鍵盤前的值（也就是上一次按 Done 確定的數字），接著打數字會重新取代 */
  if (k === 'restore') {
    el.value = _kp.orig;
    _kp.fresh = true;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    kpRender();
    return;
  }
  let v = el.value;
  if (/^[0-9]$/.test(k)) {
    if (_kp.fresh || v === '0') v = '';
    if (v.replace('.', '').length >= 6) return;
    v += k;
  } else if (k === '.') {
    if (el.getAttribute('inputmode') !== 'decimal') return;
    if (_kp.fresh) v = '';
    if (v.includes('.')) return;
    v = (v || '0') + '.';
  } else if (k === 'del') {
    v = v.slice(0, -1);
  } else if (k === 'clear') {
    v = '';
  }
  _kp.fresh = false;
  el.value = v;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  kpRender();
}
function kpCommit() {
  const el = kpTarget();
  if (!el) return;
  kpAnchor(el).classList.remove('kp-active');
  el.classList.remove('kp-active');
  const changed = el.value !== _kp.orig;
  _kp.key = null;
  if (changed) el.dispatchEvent(new Event('change', { bubbles: true }));
}
function kpClose() {
  kpCommit();
  document.getElementById('kp').classList.remove('open');
  document.getElementById('kpOverlay').classList.remove('open');
}
function kpNext() {
  const el = kpTarget();
  const list = kpFields();
  const nxt = list[list.indexOf(el) + 1];
  const nextKey = nxt ? kpKeyOf(nxt) : null;
  kpCommit();   // 各艙件數 commit 會重畫卡片，所以先記下一格的選擇器再重新找
  if (nextKey && document.querySelector(nextKey)) kpOpen(document.querySelector(nextKey));
  else kpClose();
}

/* 按下就觸發（pointerdown），不等 click：頁面防雙擊放大的 touchend 會吃掉 0.3 秒內的第二次 click，連按會掉字 */
/* 防穿透：按鍵在按下時就動作（Done 會關掉鍵盤、Next 會移位），手指放開後瀏覽器補發的 click
   會落到原本被鍵盤蓋住的按鈕（曾誤觸 COMPUTE）。iOS 在 touchend 取消補發；另外按完 0.35 秒內
   鍵盤以外的 click 一律攔掉。這個攔截要比下面「點數字框開鍵盤」的監聽先註冊才擋得到 */
let _kpPressing = false, _kpGuardUntil = 0;
document.addEventListener('click', e => {
  if (Date.now() < _kpGuardUntil && !(e.target.closest && e.target.closest('#kp'))) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }
}, true);
document.getElementById('kp').addEventListener('touchend', e => {
  if (e.target.closest('[data-k]')) e.preventDefault();
}, { passive: false });
document.getElementById('kp').addEventListener('pointerdown', e => {
  const b = e.target.closest('[data-k]');
  if (!b || b.disabled) return;
  e.preventDefault();
  _kpPressing = true;
  b.classList.add('pressed');
  kpPress(b.dataset.k);
});
const kpRelease = () => {
  if (_kpPressing) { _kpPressing = false; _kpGuardUntil = Date.now() + 350; }
  document.querySelectorAll('.kp-key.pressed, .kp-clear.pressed').forEach(b => b.classList.remove('pressed'));
};
document.addEventListener('pointerup', kpRelease);
document.addEventListener('pointercancel', kpRelease);
document.getElementById('kpOverlay').addEventListener('click', e => {
  /* 點彈窗外：完成目前這格；如果點到的是另一個數字框就直接換過去 */
  const ov = e.currentTarget;
  ov.style.display = 'none';
  const under = kpFieldOf(document.elementFromPoint(e.clientX, e.clientY));
  ov.style.display = '';
  const underKey = under ? kpKeyOf(under) : null;
  if (underKey && underKey !== _kp.key) { kpCommit(); kpOpen(document.querySelector(underKey)); }
  else kpClose();
});
document.addEventListener('pointerdown', e => {
  const el = kpFieldOf(e.target);
  if (el) el.readOnly = true;   // 不讓系統鍵盤跳出來
}, true);
document.addEventListener('click', e => {
  const el = kpFieldOf(e.target);
  if (!el) return;
  e.preventDefault();
  kpOpen(el);
}, true);
document.addEventListener('keydown', e => {
  if (!_kp.key) return;
  const map = { Backspace: 'del', Delete: 'clear', Enter: 'done', Escape: 'done', Tab: 'next', '.': '.' };
  const k = /^[0-9]$/.test(e.key) ? e.key : map[e.key];
  if (!k) return;
  e.preventDefault();
  kpPress(k);
});
window.addEventListener('scroll', () => { if (_kp.key) kpPlace(); }, { passive: true });
window.addEventListener('resize', () => { if (_kp.key) kpPlace(); });

/* ── COMPUTE 前檢查：沒填好的項目跳警示框、欄位標紅字；不改 calcDistribution 本身 ── */
const CHK_ITEMS = [
  { key:'crew',    name:'CREW BAG',    need:'kg'  },
  { key:'toolbox', name:'TOOLBOX',     need:'kg'  },
  { key:'transit', name:'TRANSIT BAG', need:'pcs' },
  { key:'trash',   name:'GARBAGE',     need:'kg'  },
  { key:'cargo',   name:'CARGO',       need:'kg'  },
];
let _chkActive = false;   // 按過 COMPUTE 且有錯之後，改資料就即時更新紅字
function chkCollect() {
  const issues = [];
  CHK_ITEMS.forEach(({ key, name, need }) => {
    const hold = document.getElementById(key + '-hold').value;
    const pcs = v(key + '-pcs'), kg = v(key + '-kg');
    if ((pcs > 0 || kg > 0) && !hold) issues.push({ key, name, msg:'Select a hold', target: key + '-hold' });
    else if (hold && need === 'kg' && !(kg > 0)) issues.push({ key, name, msg:'Enter KG', target: key + '-kg' });
    else if (hold && need === 'pcs' && !(pcs > 0)) issues.push({ key, name, msg:'Enter PCS', target: key + '-pcs' });
  });
  if (!(v('totalPcs') > 0)) issues.push({ key:'bags', name:'DEPARTURE BAGS', msg:'Enter total PCS', target:'totalPcs' });
  if (!(v('totalKg')  > 0)) issues.push({ key:'bags', name:'DEPARTURE BAGS', msg:'Enter total KG',  target:'totalKg' });
  return issues;
}
function chkClearMarks() {
  document.querySelectorAll('.si-error').forEach(e => e.remove());
  document.querySelectorAll('.invalid').forEach(e => e.classList.remove('invalid'));
}
function chkMark(issues) {
  chkClearMarks();
  issues.forEach(it => {
    const el = document.getElementById(it.target);
    (el.closest('.si-field, .field') || el).classList.add('invalid');
    const box = it.key === 'bags' ? el.closest('.card') : el.closest('.special-item');
    let msg = box.querySelector(':scope > .si-error');
    if (!msg) { msg = document.createElement('div'); msg.className = 'si-error'; box.appendChild(msg); }
    msg.textContent = msg.textContent ? msg.textContent + ' · ' + it.msg : it.msg;
  });
}
function onComputeClick() {
  const issues = chkCollect();
  if (!issues.length) { _chkActive = false; chkClearMarks(); calcDistribution(); return; }
  _chkActive = true;
  chkMark(issues);
  document.getElementById('chkList').innerHTML = issues.map(it => `<li><b>${it.name}</b>${it.msg}</li>`).join('');
  document.getElementById('chkOverlay').classList.add('open');
}
function chkFix() {
  document.getElementById('chkOverlay').classList.remove('open');
  const first = document.querySelector('.invalid');
  if (first) first.scrollIntoView({ behavior:'smooth', block:'center' });
}
function chkComputeAnyway() {
  document.getElementById('chkOverlay').classList.remove('open');
  calcDistribution();
}
['input','change'].forEach(t => document.addEventListener(t, () => {
  if (!_chkActive) return;
  const issues = chkCollect();
  chkMark(issues);
  if (!issues.length) _chkActive = false;
}));

/* 初始化 */
buildHoldOptions();
renderResult();
loadLdmFlights();
const _initSyncId = getSyncId() || genSyncId();
setSyncId(_initSyncId);
/* 先顯示本機資料，雲端有資料再覆蓋；雲端沒有或連不上就維持本機 */
loadState();
pullState();
