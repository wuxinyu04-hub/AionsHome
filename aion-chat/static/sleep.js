// 晚安，小语 · 方案 B「插画房间」重构
// 三屏：入口（继续听+一句话生成）/ 播放（全屏场景+逐句字幕+定时渐弱+白噪音）/ 故事库（分组+离线标记+预载）
// 离线策略：Cache API 显式下载音频+剧本；sw.js 音频 cache-first。缓存名须与 sw.js 一致。
(function () {
  const $ = (id) => document.getElementById(id);
  const audio = $('audio');
  const CACHE = 'aion-sleep-v4'; // 与 sw.js SLEEP_CACHE 保持一致

  const CAT_NAMES = { reading: '他讲的书', boyfriend: '他的晚安', meditation: '助眠冥想', fairytale: '睡前童话', asmr: '白噪与耳语' };
  const CAT_ORDER = ['reading', 'boyfriend', 'meditation', 'fairytale', 'asmr'];
  const MODE_PLACEHOLDER = {
    boyfriend: '今天加班到十点，好累…',
    reading: '想让他边读边说点什么…（可留空）',
    meditation: '想放松的地方，比如肩颈、脑子停不下来…',
  };
  // 封面用同一个房间 SVG 的不同取景（按 id 哈希稳定分配）
  const COVER_VIEWS = ['0 0 390 370', '40 30 260 210', '140 30 260 210', '0 60 280 200', '90 120 260 200', '52 38 200 212'];

  let voicesMap = {};
  try { voicesMap = JSON.parse(localStorage.getItem('sleep_voices_map') || '{}'); } catch { }

  const state = {
    voice: localStorage.getItem('sleep_voice') || '',
    voiceName: localStorage.getItem('sleep_voice_name') || '',
    mode: localStorage.getItem('sleep_mode') || 'boyfriend',
    items: [],
    currentId: null,
    queue: [], qIdx: 0,
    sentences: [], cum: [], totalChars: 0, capIdx: -1, fullText: '',
    pollTimer: null,
    voiceSheetThen: null,
    // 睡眠定时
    timerMode: '0', timerEndAt: 0, timerTick: null, stopAtEnd: false, baseVol: 1,
    // 白噪音（素材文件循环播）
    wnVol: parseInt(localStorage.getItem('sleep_wn_vol') || '35', 10),
  };

  // ── 基础 ──
  function toast(msg, ms = 2200) {
    const t = $('toast'); t.textContent = msg; t.classList.add('show');
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
  }
  function fmtTime(s) {
    if (!s || !isFinite(s)) return '0:00';
    const m = Math.floor(s / 60), x = Math.floor(s % 60);
    return m + ':' + (x < 10 ? '0' : '') + x;
  }
  function esc(s) { return (s || '').replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c])); }

  // ── 导航栈（替换硬编码的 showScreen，支持物理返回键） ──
  const navStack = ['home']; // 栈底 = 入口
  let curScreen = 'home';
  function showScreen(id) {
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('on'));
    $(id).classList.add('on');
    curScreen = id;
    updateMiniPlayer(); // 唯一入口：所有切屏都在这里同步迷你播放器
  }
  function pushScreen(id) {
    navStack.push(id);
    showScreen(id);
    document.title = (id === 'player' ? '播放中 ' : id === 'album' ? '故事 ' : '') + (id === 'player' ? $('pTitle').textContent : '');
  }
  function popScreen() {
    if (navStack.length <= 1) {
      // 回到入口后，再按返回 = 退出回到主页
      try { top.location.href = '/'; } catch { location.href = '/'; }
      return;
    }
    navStack.pop();
    const prev = navStack[navStack.length - 1];
    showScreen(prev);
    document.title = '晚安，小语';
  }
  function setScreen(id) {
    showScreen(id);
  }
  window.addEventListener('popstate', popScreen);
  // 物理返回键兜底：部分 WebView 走 hashchange，这里用 history 空态兜底
  window.addEventListener('hashchange', () => { history.replaceState(null, '', location.href.split('#')[0]); });

  // ── 底部迷你播放器（全局悬浮，播放时滑入覆盖在所有页面上方） ──
  const miniPlayer = $('miniPlayer');
  function updateMiniPlayer() {
    // 有在播/暂停中的音频就显示；播放屏本身有完整控件，迷你条要让位
    const live = !!(state.currentId && audio.src);
    if (live && curScreen !== 'player') {
      const it = state.items.find(x => x.id === state.currentId) || { title: $('pTitle').textContent || '今晚的故事', id: state.currentId };
      $('miniTitle').textContent = it.title || '—';
      renderMiniCover(it.id);
      $('miniPause').textContent = audio.paused ? '▶' : '⏸';
      const d = audio.duration;
      $('miniProgFill').style.width = (isFinite(d) && d > 0 ? audio.currentTime / d * 100 : 0) + '%';
      miniPlayer.classList.add('show');
    } else {
      miniPlayer.classList.remove('show');
    }
  }
  function renderMiniCover(id) {
    const it = state.items.find(x => x.id === id);
    $('miniCover').innerHTML = it ? coverHtml(it) : '';
  }
  // 点迷你播放器 → 进全屏播放页
  miniPlayer.onclick = () => { pushScreen('player'); };
  $('miniPause').onclick = (e) => {
    e.stopPropagation();
    if (!audio.src) return;
    if (audio.paused) audio.play().catch(() => { }); else audio.pause();
  };

  function hashIdx(str, n) {
    let h = 0; for (let i = 0; i < (str || '').length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return h % n;
  }

  // ── 问候语（按时段） ──
  (function initGreet() {
    const h = new Date().getHours();
    let g = '晚安，小语', sub = '灯给你留着，猫也在。今天辛苦了。';
    if (h >= 5 && h < 11) { g = '早，小语'; sub = '这么早来找我？昨晚睡得好吗。'; }
    else if (h >= 11 && h < 18) { g = '午安，小语'; sub = '偷个懒也可以，躺一会儿吧。'; }
    else if (h >= 23 || h < 5) { g = '很晚了，小语'; sub = '还没睡？过来，今晚我来哄。'; }
    $('greet').textContent = g; $('greetSub').textContent = sub;
  })();

  // ── 声音 ──
  async function loadVoices() {
    try {
      const r = await fetch('/api/sleep/voices');
      const d = await r.json();
      const vs = d.voices || [];
      const list = $('voiceList');
      if (!vs.length) {
        list.innerHTML = `<div style="padding:30px 0;text-align:center;color:var(--muted);font-size:12px">${esc(d.error || '没有可用的声音')}</div>`;
        return;
      }
      vs.forEach(v => { voicesMap[v.uri] = v.customName || v.uri; });
      localStorage.setItem('sleep_voices_map', JSON.stringify(voicesMap));
      list.innerHTML = vs.map(v => `
        <div class="vo ${v.uri === state.voice ? 'sel' : ''}" data-uri="${esc(v.uri)}" data-name="${esc(v.customName || v.uri)}">
          <span class="n">${esc(v.customName || v.uri)}</span><span class="chk">✓</span>
        </div>`).join('');
      list.querySelectorAll('.vo').forEach(el => el.onclick = () => {
        state.voice = el.dataset.uri; state.voiceName = el.dataset.name;
        localStorage.setItem('sleep_voice', state.voice);
        localStorage.setItem('sleep_voice_name', state.voiceName);
        list.querySelectorAll('.vo').forEach(x => x.classList.toggle('sel', x === el));
        updateVoiceHint();
        closeSheet('voiceSheet');
        const then = state.voiceSheetThen; state.voiceSheetThen = null;
        if (then) then();
      });
    } catch { /* 离线：用记住的声音名 */ }
    updateVoiceHint();
  }
  function updateVoiceHint() {
    $('voiceHintName').textContent = state.voiceName || '未选声音';
  }
  function openVoiceSheet(then) {
    state.voiceSheetThen = then || null;
    $('voiceSheet').classList.add('on');
  }
  function closeSheet(id) { $(id).classList.remove('on'); }
  document.querySelectorAll('.sheet .mask').forEach(m => m.onclick = () => closeSheet(m.dataset.close));
  $('voiceChange').onclick = () => openVoiceSheet();

  // ── 模式切换（哄睡/讲书/冥想） ──
  function applyMode() {
    document.querySelectorAll('#modeRow .md').forEach(b => b.classList.toggle('on', b.dataset.m === state.mode));
    $('promptInput').placeholder = MODE_PLACEHOLDER[state.mode] || MODE_PLACEHOLDER.boyfriend;
    $('bookPick').classList.toggle('show', state.mode === 'reading');
    if (state.mode === 'reading') loadBooks();
    renderModeChips();
  }

  // 输入框下的快捷 chips 按模式变：哄睡=心情快捷生成；冥想=预制的直接听；讲书=无（书架在上面）
  const MOODS = [
    { label: '失眠翻来覆去', p: '我失眠了，翻来覆去怎么都睡不着' },
    { label: '有点想哭', p: '今天有点难过，有点想哭' },
    { label: '就想听你说话', p: '没什么特别的事，就是想听你说说话' },
  ];
  function renderModeChips() {
    const box = $('moodRow');
    if (state.mode === 'boyfriend') {
      box.innerHTML = MOODS.map((m, i) => `<button class="mood" data-i="${i}">${m.label}</button>`).join('');
      box.querySelectorAll('.mood').forEach(b => b.onclick = () => {
        const m = MOODS[parseInt(b.dataset.i, 10)];
        $('promptInput').value = m.p;
        startGenerate(m.p);
      });
    } else if (state.mode === 'meditation') {
      const meds = state.items.filter(i => i.category === 'meditation' && i.source === 'preset');
      box.innerHTML = meds.map(i => `<button class="mood" data-id="${esc(i.id)}">▶ ${esc(i.title)}</button>`).join('')
        + '<button class="mood" data-new="1">✨ 生成一段新的</button>';
      box.querySelectorAll('.mood').forEach(b => b.onclick = () => {
        if (b.dataset.new) { startGenerate(''); return; }
        const it = state.items.find(x => x.id === b.dataset.id);
        if (it) onItemClick(it);
      });
    } else {
      box.innerHTML = '';
    }
  }

  // ── 讲书：共读书架选书（念真实章节，默认接着读到的那章） ──
  const bookState = { books: null, sel: null };
  async function loadBooks() {
    if (bookState.books) { renderShelf(); return; }
    try { const r = await fetch('/api/books'); const d = await r.json(); bookState.books = d.books || []; }
    catch { bookState.books = []; }
    renderShelf();
  }
  // 讲书续听：从已生成的讲书条目里找这本书上次听到哪一章（听完≈进度到尾）
  function bookResume(bookId) {
    const eps = state.items
      .filter(i => i.category === 'reading' && i.book_id === bookId && i.book_chapter >= 0)
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (!eps.length) return null;
    const last = eps[0];
    const done = last.duration_sec > 0 && (last.progress_sec || 0) >= last.duration_sec - 30;
    return { chapter: last.book_chapter, done };
  }
  function renderShelf() {
    const shelf = $('bpShelf');
    if (!bookState.books.length) {
      shelf.innerHTML = '<div class="bp-empty">书架还空着——去共读页传一本 EPUB，或直接在下面写书名让他讲</div>';
      $('bpChapter').classList.remove('show');
      return;
    }
    shelf.innerHTML = bookState.books.map(b => {
      const res = bookResume(b.book_id);
      const line = res
        ? `上次听到 第 ${res.chapter + 1} 章${res.done ? ' ✓' : ''}`
        : `共读到 第 ${(b.current_chapter || 0) + 1} 章`;
      return `
      <div class="bp-book ${bookState.sel && bookState.sel.book_id === b.book_id ? 'sel' : ''}" data-id="${esc(b.book_id)}">
        <div class="bt">${esc(b.title)}</div>
        <div class="ba">${esc(b.author || '')}</div>
        <div class="bp">${line}</div>
      </div>`;
    }).join('');
    shelf.querySelectorAll('.bp-book').forEach(el => el.onclick = () => {
      const b = bookState.books.find(x => x.book_id === el.dataset.id);
      if (!b) return;
      if (bookState.sel && bookState.sel.book_id === b.book_id) { // 再点一次取消选书 -> 回自由输入
        bookState.sel = null;
        $('bpChapter').classList.remove('show');
        renderShelf();
        return;
      }
      bookState.sel = b;
      renderShelf();
      loadChapters(b);
    });
  }
  async function loadChapters(b) {
    const sel = $('bpChapter');
    sel.innerHTML = '<option>章节加载中…</option>';
    sel.classList.add('show');
    try {
      const r = await fetch('/api/books/' + b.book_id);
      const d = await r.json();
      const chs = d.chapters || [];
      // 默认章节：听过 -> 没听完接着这章 / 听完了下一章；没听过 -> 共读进度那章
      const res = bookResume(b.book_id);
      let def = res ? (res.done ? res.chapter + 1 : res.chapter) : (b.current_chapter || 0);
      if (chs.length) def = Math.min(def, chs[chs.length - 1].chapter_index);
      sel.innerHTML = chs.map(c => `
        <option value="${c.chapter_index}" ${c.chapter_index === def ? 'selected' : ''}>
          ${c.chapter_index === def ? '▸ ' : ''}${esc(c.title || ('第 ' + (c.chapter_index + 1) + ' 章'))}
        </option>`).join('');
    } catch { sel.innerHTML = '<option value="-1">用当前阅读进度</option>'; }
  }
  document.querySelectorAll('#modeRow .md').forEach(b => b.onclick = () => {
    state.mode = b.dataset.m;
    localStorage.setItem('sleep_mode', state.mode);
    applyMode();
  });
  applyMode();

  // ── 故事库 ──
  async function loadLibrary() {
    let items = null;
    try {
      const r = await fetch('/api/sleep/library');
      const d = await r.json();
      items = d.items || [];
      localStorage.setItem('sleep_lib_snapshot', JSON.stringify(items));
    } catch {
      try { items = JSON.parse(localStorage.getItem('sleep_lib_snapshot') || 'null'); } catch { }
    }
    if (!items) { $('libGrid').innerHTML = '<div class="l-empty">加载失败，检查网络后重试</div>'; return; }
    state.items = items;
    // 恢复上次的播放列表（手动排过的顺序）
    if (!state.queue.length) {
      try {
        const ids = JSON.parse(localStorage.getItem('sleep_queue_ids') || '[]');
        const q = ids.map(id => items.find(x => x.id === id && x.has_audio)).filter(Boolean);
        if (q.length) state.queue = q;
      } catch { }
    }
    renderLibrary();
    renderResume();
    updateCounts();
    decorateDownloads();
    renderModeChips();       // 冥想 chips 依赖 items
    if (bookState.books) renderShelf(); // 讲书"上次听到"标记依赖 items
  }

  function statusLine(it) {
    if (it.has_audio) return fmtTime(it.duration_sec) === '0:00' ? '已合成' : fmtTime(it.duration_sec);
    if (it.status === 'generating') return '在写了…';
    if (it.status === 'synthesizing') return '在录音…';
    if (it.status === 'failed') return '出错了，点击重试';
    return '未合成 · 点击生成语音';
  }

  const coverRev = {}; // AI 封面重生成后的 cache-bust 版本号
  function voiceLabel(uri) { return voicesMap[uri] || '他'; }
  function coverHtml(it) {
    if (it && it.has_cover) {
      const r = coverRev[it.id] ? ('?r=' + coverRev[it.id]) : '';
      return `<img src="/api/sleep/${esc(it.id)}/cover${r}" loading="lazy" alt="">`;
    }
    const vb = COVER_VIEWS[hashIdx((it && it.id) || 'x', COVER_VIEWS.length)];
    return `<svg viewBox="${vb}" preserveAspectRatio="xMidYMid slice"><use href="#room"/></svg>`;
  }
  function metaLine(it) {
    const bits = [];
    if (it.voice) bits.push(voiceLabel(it.voice) + ' 读');
    if ((it.play_count || 0) > 0) bits.push(`听过 ${it.play_count} 次`);
    bits.push(it.has_audio && it.duration_sec ? fmtTime(it.duration_sec) : statusLine(it));
    return bits.join(' · ');
  }
  function cardHtml(it) {
    const busy = it.status === 'generating' || it.status === 'synthesizing';
    return `
      <div class="lc" data-id="${esc(it.id)}">
        <div class="cov">
          ${coverHtml(it)}
          ${busy ? '<span class="st">准备中…</span>' : (!it.has_audio ? '<span class="st">未合成</span>' : '')}
        </div>
        <div class="inf">
          <div class="t">${esc(it.title)}</div>
          <div class="d"><span>${esc(metaLine(it))}</span><span class="dl" data-dlid="${esc(it.id)}"></span></div>
          <button class="lmore" data-more="${esc(it.id)}">⋯</button>
        </div>
      </div>`;
  }

  // 书库：讲书条目按 book_id 聚合成"专辑"
  function albumGroups() {
    const map = {};
    state.items.filter(i => i.category === 'reading' && i.book_id).forEach(i => {
      (map[i.book_id] = map[i.book_id] || []).push(i);
    });
    return Object.entries(map).map(([bid, eps]) => {
      eps.sort((a, b) => ((a.book_chapter ?? 0) - (b.book_chapter ?? 0)) || ((a.created_at || 0) - (b.created_at || 0)));
      const title = (eps[0].title || '').split(' · ')[0] || '一本书';
      return { book_id: bid, title, eps };
    });
  }

  let libTab = 'all';
  document.querySelectorAll('#libTabs .lt').forEach(b => b.onclick = () => {
    libTab = b.dataset.c;
    document.querySelectorAll('#libTabs .lt').forEach(x => x.classList.toggle('on', x === b));
    renderLibrary();
  });

  function renderLibrary() {
    const albums = albumGroups();
    const inAlbum = new Set(albums.flatMap(a => a.eps.map(e => e.id)));
    // 书库横滑：全部 / 书库 tab 显示
    const showAlbums = albums.length > 0 && (libTab === 'all' || libTab === 'reading');
    $('albumSec').style.display = showAlbums ? '' : 'none';
    if (showAlbums) {
      $('albumRow').innerHTML = albums.map(a => `
        <div class="alb" data-bid="${esc(a.book_id)}">
          <div class="alb-cov">${coverHtml(a.eps.find(e => e.has_cover) || a.eps[0])}</div>
          <div class="alb-t">${esc(a.title)}</div>
          <div class="alb-d">${a.eps.length} 集 · ${esc(voiceLabel(a.eps[0].voice))} 读</div>
        </div>`).join('');
      $('albumRow').querySelectorAll('.alb').forEach(el => el.onclick = () => openAlbum(el.dataset.bid));
    }
    // 网格（专辑里的集数不重复出现在网格）
    let items = state.items.filter(i => !inAlbum.has(i.id));
    if (libTab !== 'all') items = items.filter(i => i.category === libTab);
    const gen = items.filter(i => i.source === 'ai_generated').sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    const preset = CAT_ORDER.flatMap(c => items.filter(i => i.source === 'preset' && i.category === c));
    items = [...gen, ...preset];
    const grid = $('libGrid');
    grid.innerHTML = items.length ? items.map(cardHtml).join('')
      : (showAlbums ? '' : '<div class="l-empty">这个分类还没有故事</div>');
    grid.querySelectorAll('.lc').forEach(c => c.onclick = () => {
      const it = state.items.find(x => x.id === c.dataset.id);
      if (it) onItemClick(it);
    });
    grid.querySelectorAll('.lmore').forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      openItemSheet(b.dataset.more);
    });
  }

  // ── 专辑页（一本书） ──
  let albumBid = null;
  function openAlbum(bid) {
    const a = albumGroups().find(x => x.book_id === bid);
    if (!a) return;
    albumBid = bid;
    const total = a.eps.reduce((s, e) => s + (e.duration_sec || 0), 0);
    $('alTitle').textContent = a.title;
    $('alMeta').textContent = `${a.eps.length} 集 · 共 ${Math.max(1, Math.round(total / 60))} 分钟 · ${voiceLabel(a.eps[0].voice)} 读`;
    $('alCover').innerHTML = coverHtml(a.eps.find(e => e.has_cover) || a.eps[0]);
    $('alList').innerHTML = a.eps.map(e => {
      const ch = (e.book_chapter ?? -1) >= 0 ? `第 ${e.book_chapter + 1} 章` : '';
      const nm = (e.title || '').split(' · ')[1] || e.title;
      const meta = [ch, (e.play_count || 0) > 0 ? `听过 ${e.play_count} 次` : '',
        e.has_audio && e.duration_sec ? fmtTime(e.duration_sec) : statusLine(e)].filter(Boolean).join(' · ');
      return `
        <div class="al-ep" data-id="${esc(e.id)}">
          <span class="dot ${e.has_audio ? 'ready' : 'raw'}"></span>
          <div class="meta"><div class="t">${esc(nm)}</div><div class="d">${esc(meta)}</div></div>
          <button class="lmore" data-more="${esc(e.id)}">⋯</button>
        </div>`;
    }).join('');
    $('alList').querySelectorAll('.al-ep').forEach(el => el.onclick = () => {
      const it = a.eps.find(x => x.id === el.dataset.id);
      if (it && it.has_audio) playItem(it, a.eps.filter(x => x.has_audio));
      else if (it) onItemClick(it);
    });
    $('alList').querySelectorAll('.lmore').forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      openItemSheet(b.dataset.more);
    });
    pushScreen('album');
  }
  $('alBack').onclick = () => popScreen();
  $('alPlay').onclick = () => {
    const a = albumGroups().find(x => x.book_id === albumBid);
    if (!a) return;
    const ready = a.eps.filter(e => e.has_audio);
    if (!ready.length) { toast('这本书还没有已合成的集数'); return; }
    // 续听：优先没听完的最新一集，否则第一集
    const unfinished = ready.filter(e => (e.progress_sec || 0) > 5 && e.duration_sec > 0 && e.progress_sec < e.duration_sec - 30);
    playItem(unfinished.length ? unfinished[unfinished.length - 1] : ready[0], ready);
  };
  $('alNext').onclick = () => {
    const a = albumGroups().find(x => x.book_id === albumBid);
    if (!a) return;
    if (!navigator.onLine) { toast('联网才能生成'); return; }
    if (!state.voice) { openVoiceSheet(() => $('alNext').onclick()); return; }
    const res = bookResume(a.book_id);
    const next = res ? (res.done ? res.chapter + 1 : res.chapter) : ((a.eps[a.eps.length - 1].book_chapter ?? -1) + 1);
    fetch('/api/sleep/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category: 'reading', prompt: '', voice: state.voice, book_id: a.book_id, chapter_index: next }),
    }).then(async r => {
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || '生成失败'); return; }
      const d = await r.json();
      setScreen('home');
      showGenNote(`在翻《${a.title}》的下一章…`);
      pollStatus(d.id);
    }).catch(() => toast('网络错误'));
  };
  $('alBatch').onclick = async () => {
    const a = albumGroups().find(x => x.book_id === albumBid);
    if (!a) return;
    if (!navigator.onLine) { toast('联网才能生成'); return; }
    if (!state.voice) { openVoiceSheet(() => $('alBatch').onclick()); return; }
    let pending = null;
    try {
      const r = await fetch('/api/books/' + a.book_id);
      if (r.ok) {
        const d = await r.json();
        const ready = new Set(a.eps.filter(e => e.has_audio).map(e => e.book_chapter));
        const active = new Set(a.eps.filter(e => ['generating', 'synthesizing'].includes(e.status)).map(e => e.book_chapter));
        pending = (d.chapters || []).filter(ch => (ch.char_count || 0) > 0 && !ready.has(ch.chapter_index) && !active.has(ch.chapter_index)).length;
      }
    } catch { }
    if (pending === 0) { toast('这本书已经全部生成好了，或正在生成中'); return; }
    const hint = pending == null ? '未完成章节' : `${pending} 章未完成`;
    if (!window.confirm(`《${a.title}》${hint}，开始整本续跑吗？`)) return;
    const btn = $('alBatch');
    btn.disabled = true;
    btn.textContent = '▣ 正在排队…';
    try {
      const r = await fetch('/api/sleep/generate-book', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: a.book_id, voice: state.voice }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(d.detail || '整本生成失败'); return; }
      toast(`已开始续跑 ${a.title}，完成情况会自动更新`);
      setScreen('library');
      pollLibraryWhileGenerating(a.book_id);
    } catch { toast('网络错误'); }
    finally {
      btn.disabled = false;
      btn.textContent = '▣ 整本生成';
    }
  };
  let batchPollTimer = null;
  function pollLibraryWhileGenerating(bookId) {
    clearInterval(batchPollTimer);
    let rounds = 0;
    batchPollTimer = setInterval(async () => {
      rounds++;
      await loadLibrary();
      const a = albumGroups().find(x => x.book_id === bookId);
      const active = a && a.eps.some(e => ['generating', 'synthesizing'].includes(e.status));
      if (!active || rounds >= 360) clearInterval(batchPollTimer);
    }, 5000);
  }

  // ── 条目操作单：重命名 / AI 封面 / 加列表 ──
  let sheetItemId = null;
  function openItemSheet(id) {
    const it = state.items.find(x => x.id === id);
    if (!it) return;
    sheetItemId = id;
    $('isTitle').textContent = it.title;
    $('isQueue').style.display = it.has_audio ? '' : 'none';
    $('itemSheet').classList.add('on');
  }
  function refreshLibViews() {
    renderLibrary(); renderResume();
    if (albumBid && $('album').classList.contains('on')) openAlbum(albumBid);
    decorateDownloads();
  }
  $('isRename').onclick = async () => {
    const it = state.items.find(x => x.id === sheetItemId);
    closeSheet('itemSheet');
    if (!it) return;
    const name = (window.prompt('给这个故事起个名字', it.title) || '').trim();
    if (!name || name === it.title) return;
    try {
      const r = await fetch('/api/sleep/' + it.id + '/title', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: name }),
      });
      if (!r.ok) throw 0;
      it.title = name;
      if (state.currentId === it.id) $('pTitle').textContent = name;
      refreshLibViews();
      toast('改好了');
    } catch { toast('改名失败'); }
  };
  $('isCover').onclick = async () => {
    const it = state.items.find(x => x.id === sheetItemId);
    closeSheet('itemSheet');
    if (!it) return;
    if (!navigator.onLine) { toast('联网才能画封面'); return; }
    const extra = (window.prompt('封面想要什么画面？（留空让他自己画）', '') || '').trim();
    toast('在画封面了，大概半分钟…', 5000);
    try {
      const r = await fetch('/api/sleep/' + it.id + '/cover', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: extra }),
      });
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || '生成失败，稍后再试'); return; }
      it.has_cover = true;
      coverRev[it.id] = Date.now();
      refreshLibViews();
      toast('封面画好了 🖼');
    } catch { toast('网络错误'); }
  };
  $('isQueue').onclick = () => {
    const it = state.items.find(x => x.id === sheetItemId);
    closeSheet('itemSheet');
    if (it) queueAdd(it);
  };
  $('isDelete').onclick = async () => {
    const it = state.items.find(x => x.id === sheetItemId);
    closeSheet('itemSheet');
    if (!it) return;
    if (!window.confirm(`确定删除「${it.title}」吗？\n\n音频文件也会一起删除，不可恢复。`)) return;
    try {
      const r = await fetch('/api/sleep/' + it.id, { method: 'DELETE' });
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || '删除失败'); return; }
      // 如果正在播这条，停掉
      if (state.currentId === it.id) {
        audio.pause(); audio.src = '';
        state.currentId = null; state.queue = []; state.qIdx = 0;
        saveQueue();
        setScreen('home');
      }
      // 从队列里移除
      state.queue = state.queue.filter(x => x.id !== it.id);
      saveQueue();
      await loadLibrary();
      toast('已删除');
    } catch { toast('网络错误'); }
  };

  function updateCounts() {
    const total = state.items.length;
    isDownloadedMany(state.items.filter(i => i.has_audio).map(i => i.id)).then(n => {
      $('libCount').textContent = total ? `${total} 篇 · ${n} 篇已离线` : '';
      $('libSub').innerHTML = total ? `${total} 篇 · <b>${n} 篇已离线，断网也能听</b>` : '';
    });
  }

  // ── 继续听卡片 ──
  function renderResume() {
    const lastId = localStorage.getItem('sleep_last_id');
    const it = state.items.find(x => x.id === lastId && x.has_audio);
    const card = $('resumeCard');
    if (!it) { card.classList.remove('show'); return; }
    card.classList.add('show');
    $('resumeTitle').textContent = it.title;
    const p = it.progress_sec || 0, d = it.duration_sec || 0;
    $('resumeMeta').textContent = p > 5 ? `上次到 ${fmtTime(p)}` : '从头开始';
    $('resumeBar').style.width = (d > 0 ? Math.min(100, p / d * 100) : 0) + '%';
    isDownloaded(it.id).then(ok => { $('resumeDl').textContent = ok ? '✓ 已离线' : ''; });
    card.onclick = () => playItem(it, readyQueue());
  }
  function readyQueue() { return state.items.filter(i => i.has_audio); }

  // ── 条目点击 ──
  function onItemClick(it) {
    if (it.has_audio) { playItem(it, readyQueue()); return; }
    if (!navigator.onLine) { toast('离线中，只能听已下载的'); return; }
    if (it.status === 'generating' || it.status === 'synthesizing') {
      showGenNote(it.status === 'generating' ? '在写这一篇了，先躺好' : '写好了，正在录音…',
        it.progress_pct, it.progress_detail);
      pollStatus(it.id);
      setScreen('home');
      return;
    }
    // 未合成 / 失败：触发 TTS 合成
    if (!state.voice) { openVoiceSheet(() => onItemClick(it)); return; }
    synthesizeItem(it);
  }
  async function synthesizeItem(it) {
    try {
      // 重合成前清掉旧音频缓存：SW 音频 cache-first，不清则重合成后仍播旧音色
      if ('caches' in window) {
        try { await (await caches.open(CACHE)).delete('/api/sleep/' + it.id + '/audio'); } catch {}
      }
      const r = await fetch('/api/sleep/' + it.id + '/synthesize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice: state.voice }),
      });
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || '合成失败'); return; }
      showGenNote('在录《' + it.title + '》…', 0);
      setScreen('home');
      pollStatus(it.id);
    } catch { toast('网络错误'); }
  }

  // ── 生成 ──
  function startGenerate(prompt) {
    prompt = (prompt || '').trim();
    const withBook = state.mode === 'reading' && bookState.sel;
    // 冥想不用输入（后端自动"整体放松"）
    if (!prompt && !withBook && state.mode !== 'meditation') {
      toast(state.mode === 'reading' ? '先在书架选一本，或写个书名' : '写一句话吧'); return;
    }
    if (!navigator.onLine) { toast('离线不能生成，请联网'); return; }
    if (!state.voice) { openVoiceSheet(() => startGenerate(prompt)); return; }
    doGenerate(prompt);
  }
  async function doGenerate(prompt) {
    const withBook = state.mode === 'reading' && bookState.sel;
    showGenNote(
      withBook ? `在翻《${bookState.sel.title}》今晚要读的那章…`
        : state.mode === 'meditation' ? '在写今晚的冥想引导…'
          : '在写今晚的故事了，先躺好');
    const body = { category: state.mode, prompt, voice: state.voice };
    if (withBook) {
      body.book_id = bookState.sel.book_id;
      const ch = parseInt($('bpChapter').value, 10);
      body.chapter_index = isNaN(ch) ? -1 : ch;
    }
    try {
      const r = await fetch('/api/sleep/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { const e = await r.json().catch(() => ({})); toast(e.detail || '生成失败'); hideGenNote(); return; }
      const d = await r.json();
      $('promptInput').value = '';
      pollStatus(d.id);
    } catch { toast('网络错误'); hideGenNote(); }
  }
  function showGenNote(text, pct, detail) {
    $('genText').textContent = text || '在写今晚的故事了，先躺好';
    $('genBar').style.width = (pct || 0) + '%';
    $('genPct').textContent = pct != null ? Math.round(pct) + '%' : '';
    $('genNote').classList.add('on');
  }
  function hideGenNote() {
    $('genNote').classList.remove('on');
    $('genBar').style.width = '0%';
    $('genPct').textContent = '';
    clearInterval(state.pollTimer); state.pollTimer = null;
  }
  function updateGenProgress(pct, detail) {
    $('genBar').style.width = (pct || 0) + '%';
    $('genPct').textContent = pct != null ? Math.round(pct) + '%' : '';
    if (detail) $('genText').textContent = detail;
  }
  $('genCancel').onclick = () => { hideGenNote(); toast('后台还在写，写完去故事库找'); };

  function pollStatus(id) {
    clearInterval(state.pollTimer);
    let n = 0;
    state.pollTimer = setInterval(async () => {
      // 放弃时必须 clearInterval：只 return 的话定时器还在，会一直 2.5s 打一次 /status
      if (++n > 150) { clearInterval(state.pollTimer); state.pollTimer = null; hideGenNote(); toast('等太久了，稍后去故事库看看'); return; }
      let it = null;
      try { const r = await fetch('/api/sleep/' + id + '/status'); it = await r.json(); } catch { return; }
      if (!it || !it.status) return;
      // 更新进度条
      if (it.progress_pct != null) updateGenProgress(it.progress_pct, it.progress_detail);
      if (it.status === 'synthesizing') showGenNote('写好了，正在录音…', it.progress_pct, it.progress_detail);
      else if (it.status === 'ready') {
        hideGenNote();
        await loadLibrary();
        const fresh = state.items.find(x => x.id === id) || it;
        playItem(fresh, readyQueue());
        cacheItem(id).then(ok => { if (ok) { decorateDownloads(); updateCounts(); updateDlBtn(); } }); // 自动落离线
      } else if (it.status === 'failed') {
        hideGenNote(); toast('出错了，稍后再试'); loadLibrary();
      }
    }, 2500);
  }

  // ── 播放 ──
  async function playItem(it, queue) {
    if (state.queue.some(x => x.id === it.id)) { // 已在手动列表里：不打散列表，只跳位置
      state.qIdx = state.queue.findIndex(x => x.id === it.id);
    } else if (queue && queue.length) {
      state.queue = queue; state.qIdx = Math.max(0, queue.findIndex(x => x.id === it.id));
    } else { state.queue = [it]; state.qIdx = 0; }
    saveQueue();
    state.currentId = it.id;
    localStorage.setItem('sleep_last_id', it.id);
    // 播放计数（离线不报，回本地乐观 +1）
    if (navigator.onLine) fetch('/api/sleep/' + it.id + '/played', { method: 'POST' }).catch(() => { });
    it.play_count = (it.play_count || 0) + 1;
    $('pTitle').textContent = it.title || '今晚的故事';
    $('pMid').classList.remove('expanded');
    $('capPast').textContent = ''; $('capNow').textContent = '';
    state.capIdx = -1;
    pushScreen('player');

    // 剧本分句（去掉 [SFX:...] 标记），按字数权重对齐进度
    try {
      const r = await fetch('/api/sleep/' + it.id + '/script');
      const d = await r.json();
      prepSentences(d.script_text || '');
    } catch { prepSentences(''); }

    // 进度记忆
    let saved = it.progress_sec || 0;
    try { const r = await fetch('/api/sleep/' + it.id + '/progress'); const d = await r.json(); saved = d.progress_sec || saved; } catch { }

    audio.src = '/api/sleep/' + it.id + '/audio';
    const onMeta = () => {
      audio.removeEventListener('loadedmetadata', onMeta);
      if (saved > 5 && isFinite(audio.duration) && saved < audio.duration - 5) audio.currentTime = saved;
      // autoplay 受限时给提示，避免无反馈
      audio.play().catch(() => { toast('点 ▶ 开始播放'); });
      $('pDur').textContent = fmtTime(audio.duration);
    };
    audio.addEventListener('loadedmetadata', onMeta);
    setupMediaSession(it);
    updateDlBtn();
    renderQueueSheet();
  }

  function prepSentences(text) {
    state.fullText = (text || '').replace(/\[SFX:[^\]]+\]/g, '').trim();
    const parts = state.fullText.split(/(?<=[。！？!?])|(?<=……)|\n+/).map(s => s.trim()).filter(s => s.length > 1);
    state.sentences = parts.length ? parts : ['今晚好好睡。'];
    state.cum = []; let acc = 0;
    state.sentences.forEach(s => { acc += s.length; state.cum.push(acc); });
    state.totalChars = acc || 1;
  }
  function updateCaptions() {
    if (!state.sentences.length || $('pMid').classList.contains('expanded')) return;
    if (!isFinite(audio.duration) || audio.duration <= 0) return;
    const target = (audio.currentTime / audio.duration) * state.totalChars;
    let idx = state.cum.findIndex(c => c > target);
    if (idx < 0) idx = state.sentences.length - 1;
    if (idx === state.capIdx) return;
    state.capIdx = idx;
    const now = $('capNow'), past = $('capPast');
    now.style.opacity = 0; past.style.opacity = 0;
    setTimeout(() => {
      past.textContent = idx > 0 ? state.sentences[idx - 1] : '';
      now.textContent = state.sentences[idx];
      past.style.opacity = ''; now.style.opacity = 1;
    }, 350);
  }
  // 点字幕区：切全文/逐句
  $('pMid').onclick = () => {
    const mid = $('pMid');
    if (mid.classList.contains('expanded')) {
      mid.classList.remove('expanded');
      state.capIdx = -1; updateCaptions();
    } else {
      mid.classList.add('expanded');
      $('capPast').textContent = '';
      $('capNow').textContent = state.fullText || '今晚好好睡。';
    }
  };

  function setupMediaSession(it) {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: it.title || '今晚的故事', artist: '温叙远', album: '晚安，小语',
    });
    navigator.mediaSession.setActionHandler('play', () => audio.play().catch(() => { }));
    navigator.mediaSession.setActionHandler('pause', () => audio.pause());
    navigator.mediaSession.setActionHandler('seekbackward', () => { audio.currentTime = Math.max(0, audio.currentTime - 15); });
    navigator.mediaSession.setActionHandler('seekforward', () => { audio.currentTime = Math.min(audio.duration || 1e9, audio.currentTime + 15); });
    try {
      navigator.mediaSession.setActionHandler('seekto', (d) => { if (d.seekTime != null) audio.currentTime = d.seekTime; });
    } catch { }
  }

  // 控制
  function updatePlayUi() { $('pPlay').textContent = audio.paused ? '▶' : '⏸'; }
  $('pPlay').onclick = () => { if (!audio.src) return; if (audio.paused) audio.play().catch(() => { }); else audio.pause(); };
  $('pRew').onclick = () => { audio.currentTime = Math.max(0, audio.currentTime - 15); };
  $('pFwd').onclick = () => { audio.currentTime = Math.min(audio.duration || 1e9, audio.currentTime + 15); };
  $('pBack').onclick = () => popScreen();

  // 进度条：pointer 拖动 scrub（按住实时预览，松手 seek），不再是点一下才跳
  let scrubbing = false;
  const pBar = $('pBar');
  function barFrac(e) {
    const r = pBar.getBoundingClientRect();
    return Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1);
  }
  function scrubShow(f) {
    $('pFill').style.width = (f * 100) + '%';
    $('pCur').textContent = fmtTime(f * (audio.duration || 0));
  }
  pBar.addEventListener('pointerdown', (e) => {
    if (!isFinite(audio.duration) || audio.duration <= 0) return;
    scrubbing = true;
    try { pBar.setPointerCapture(e.pointerId); } catch { }
    scrubShow(barFrac(e));
    e.preventDefault();
  });
  pBar.addEventListener('pointermove', (e) => { if (scrubbing) scrubShow(barFrac(e)); });
  pBar.addEventListener('pointerup', (e) => {
    if (!scrubbing) return;
    scrubbing = false;
    audio.currentTime = barFrac(e) * audio.duration;
  });
  pBar.addEventListener('pointercancel', () => { scrubbing = false; });

  audio.addEventListener('play', () => { updatePlayUi(); updateMiniPlayer(); });
  audio.addEventListener('pause', () => { updatePlayUi(); updateMiniPlayer(); });
  audio.addEventListener('timeupdate', () => {
    if (!isFinite(audio.currentTime) || !isFinite(audio.duration)) return;
    if (!scrubbing) { // 拖动中进度条归手指管
      $('pFill').style.width = (audio.currentTime / audio.duration * 100) + '%';
      $('pCur').textContent = fmtTime(audio.currentTime);
    }
    $('pDur').textContent = fmtTime(audio.duration);
    // 迷你播放器进度同步
    if (miniPlayer.classList.contains('show')) {
      $('miniProgFill').style.width = (audio.currentTime / audio.duration * 100) + '%';
      $('miniPause').textContent = audio.paused ? '▶' : '⏸';
    }
    updateCaptions();
    saveProgressThrottled();
  });
  audio.addEventListener('ended', () => {
    if (state.stopAtEnd) { resetTimer(); toast('播完了，晚安 🌙'); wnStop(); updateMiniPlayer(); return; }
    if (state.qIdx < state.queue.length - 1) { state.qIdx++; playItem(state.queue[state.qIdx], state.queue); }
  });

  let _st = null;
  function saveProgressThrottled() {
    if (state.currentId == null || _st || !navigator.onLine) return;
    _st = setTimeout(() => {
      _st = null;
      if (state.currentId == null) return;
      fetch('/api/sleep/' + state.currentId + '/progress', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ progress_sec: Math.floor(audio.currentTime) }),
      }).catch(() => { });
    }, 3000);
  }

  // ── 睡眠定时：选中 15/30 分钟 -> 最后 60 秒音量线性渐弱 -> 暂停 ──
  document.querySelectorAll('#timerRow .tm').forEach(b => b.onclick = () => {
    document.querySelectorAll('#timerRow .tm').forEach(x => x.classList.toggle('on', x === b));
    setTimer(b.dataset.t);
  });
  function setTimer(mode) {
    clearInterval(state.timerTick); state.timerTick = null;
    state.timerMode = mode; state.stopAtEnd = false;
    audio.volume = state.baseVol;
    if (mode === '0') { $('timerHint').textContent = ''; return; }
    if (mode === 'end') { state.stopAtEnd = true; $('timerHint').textContent = '播完这篇就安静'; return; }
    const mins = parseInt(mode, 10);
    state.timerEndAt = Date.now() + mins * 60000;
    state.timerTick = setInterval(() => {
      const remain = Math.max(0, (state.timerEndAt - Date.now()) / 1000);
      if (remain <= 0) {
        audio.pause(); wnStop();
        resetTimer();
        $('timerHint').textContent = '时间到了，晚安 🌙';
        return;
      }
      if (remain <= 60) {
        audio.volume = state.baseVol * (remain / 60);
        if (wn.on) wnAudio.volume = (state.wnVol / 100) * (remain / 60); // 白噪音一起渐弱
      }
      $('timerHint').textContent = fmtTime(remain) + ' 后声音慢慢变小';
    }, 1000);
    $('timerHint').textContent = mins + ' 分钟后声音慢慢变小';
  }
  function resetTimer() {
    clearInterval(state.timerTick); state.timerTick = null;
    state.timerMode = '0'; state.stopAtEnd = false;
    audio.volume = state.baseVol;
    document.querySelectorAll('#timerRow .tm').forEach(x => x.classList.toggle('on', x.dataset.t === '0'));
  }

  // ── 白噪音：data/sleep_noise/ 素材循环播（留空位，用户放 mp3 进目录就出现） ──
  const wnAudio = new Audio(); wnAudio.loop = true;
  const wn = { files: null, idx: -1, on: false };
  async function wnLoad() {
    if (wn.files) return;
    try { const r = await fetch('/api/sleep/noise'); const d = await r.json(); wn.files = d.files || []; }
    catch { wn.files = []; }
  }
  function renderWn() {
    const btn = $('wnBtn');
    if (wn.on) {
      btn.classList.add('wn-on');
      btn.textContent = '♪ ' + (wn.files[wn.idx] || '').replace(/\.[^.]+$/, '');
      $('wnVol').classList.add('show');
    } else {
      btn.classList.remove('wn-on');
      btn.textContent = '♪ 白噪音';
      $('wnVol').classList.remove('show');
    }
  }
  async function wnToggle() {
    await wnLoad();
    if (!wn.files.length) { toast('还没有素材——把 mp3 放进 data/sleep_noise 就有了'); return; }
    // 点击循环：关 -> 素材1 -> 素材2 -> … -> 关
    wn.idx++;
    if (wn.idx >= wn.files.length) { wnStop(); wn.idx = -1; return; }
    wn.on = true;
    wnAudio.src = '/api/sleep/noise/' + encodeURIComponent(wn.files[wn.idx]);
    wnAudio.volume = state.wnVol / 100;
    wnAudio.play().catch(() => { });
    localStorage.setItem('sleep_wn_file', wn.files[wn.idx]);
    renderWn();
  }
  function wnStop() {
    wnAudio.pause();
    wn.on = false;
    renderWn();
  }
  $('wnBtn').onclick = wnToggle;
  $('wnRange').value = state.wnVol;
  $('wnRange').oninput = () => {
    state.wnVol = parseInt($('wnRange').value, 10);
    localStorage.setItem('sleep_wn_vol', String(state.wnVol));
    wnAudio.volume = state.wnVol / 100;
  };

  // ── 离线：显式下载音频+剧本进 Cache ──
  async function cacheItem(id) {
    if (!('caches' in window)) return false;
    try {
      const c = await caches.open(CACHE);
      for (const path of ['/api/sleep/' + id + '/audio', '/api/sleep/' + id + '/script']) {
        const hit = await c.match(path);
        if (hit) continue;
        const resp = await fetch(path);
        if (!resp.ok) throw new Error('fetch ' + path + ' ' + resp.status);
        await c.put(path, resp.clone());
      }
      return true;
    } catch (e) { console.warn('离线下载失败', id, e); return false; }
  }
  async function isDownloaded(id) {
    if (!('caches' in window)) return false;
    try { return !!(await (await caches.open(CACHE)).match('/api/sleep/' + id + '/audio')); } catch { return false; }
  }
  async function isDownloadedMany(ids) {
    let n = 0;
    for (const id of ids) if (await isDownloaded(id)) n++;
    return n;
  }
  async function decorateDownloads() {
    for (const it of state.items) {
      if (!it.has_audio) continue;
      const el = document.querySelector(`.dl[data-dlid="${CSS.escape(it.id)}"]`);
      if (el) el.textContent = (await isDownloaded(it.id)) ? '✓ 已离线' : '';
    }
  }
  async function updateDlBtn() {
    const btn = $('pDlBtn');
    if (!state.currentId) { btn.textContent = '⇣ 离线保存'; btn.classList.remove('dl-done'); return; }
    const ok = await isDownloaded(state.currentId);
    btn.textContent = ok ? '✓ 已离线' : '⇣ 离线保存';
    btn.classList.toggle('dl-done', ok);
  }
  $('pDlBtn').onclick = async () => {
    if (!state.currentId) return;
    if (await isDownloaded(state.currentId)) { toast('已经存好了'); return; }
    toast('下载中…');
    const ok = await cacheItem(state.currentId);
    toast(ok ? '存好了，断网也能听' : '下载失败，稍后再试');
    updateDlBtn(); decorateDownloads(); updateCounts();
  };
  $('preloadBtn').onclick = async () => {
    if (!navigator.onLine) { toast('联网后才能预载'); return; }
    const targets = readyQueue().sort((a, b) => (b.created_at || 0) - (a.created_at || 0)).slice(0, 3);
    if (!targets.length) { toast('还没有已合成的故事'); return; }
    toast('预载中…');
    let ok = 0;
    for (const it of targets) if (await cacheItem(it.id)) ok++;
    toast(`预载完成 ${ok}/${targets.length} 篇，断网也能听`);
    decorateDownloads(); updateCounts(); renderResume();
  };

  // ── 播放列表：可增删、可上下移、localStorage 持久化 ──
  function saveQueue() {
    localStorage.setItem('sleep_queue_ids', JSON.stringify(state.queue.map(i => i.id)));
  }
  function syncQIdx() {
    state.qIdx = Math.max(0, state.queue.findIndex(x => x.id === state.currentId));
  }
  function queueAdd(it) {
    if (state.queue.some(x => x.id === it.id)) { toast('已经在列表里了'); return; }
    state.queue.push(it);
    saveQueue(); syncQIdx();
    toast('已加入播放列表');
    if ($('queueSheet').classList.contains('on')) renderQueueSheet();
  }
  function queueRemove(i) {
    state.queue.splice(i, 1);
    saveQueue(); syncQIdx();
    renderQueueSheet();
  }
  let _dragJustEnded = false;
  function _startQueueDrag(row, e) {
    e.preventDefault();
    const list = $('queueList');
    row.classList.add('dragging');
    const move = (ev) => {
      const y = ev.clientY;
      const others = [...list.querySelectorAll('.vo:not(.dragging)')];
      const next = others.find(r => { const rc = r.getBoundingClientRect(); return y < rc.top + rc.height / 2; });
      if (next) list.insertBefore(row, next); else list.appendChild(row);
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      row.classList.remove('dragging');
      // 按 DOM 顺序提交新队列
      const order = [...list.querySelectorAll('.vo')].map(r => parseInt(r.dataset.i, 10));
      state.queue = order.map(i => state.queue[i]).filter(Boolean);
      saveQueue(); syncQIdx();
      _dragJustEnded = true;
      setTimeout(() => { _dragJustEnded = false; }, 50);
      renderQueueSheet();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
  }
  function renderQueueSheet() {
    const list = $('queueList');
    $('queueSub').textContent = state.queue.length ? state.queue.length + ' 篇 · 按住 ≡ 拖动排序' : '空的 · 去故事库 ⋯ 里加进来';
    list.innerHTML = state.queue.map((it, i) => `
      <div class="vo ${i === state.qIdx ? 'sel' : ''}" data-i="${i}">
        <span class="q-drag" data-i="${i}">≡</span>
        <span class="n">${esc(it.title || '今晚的故事')}</span>
        <span class="d">${i === state.qIdx ? '▶ 在念' : fmtTime(it.duration_sec)}</span>
        <span class="q-ops"><button data-op="del">✕</button></span>
      </div>`).join('');
    list.querySelectorAll('.vo').forEach(el => {
      const i = parseInt(el.dataset.i, 10);
      el.onclick = () => {
        if (_dragJustEnded) return;
        closeSheet('queueSheet');
        if (state.queue[i]) { state.qIdx = i; playItem(state.queue[i], state.queue); }
      };
      el.querySelector('.q-drag').addEventListener('pointerdown', (e) => _startQueueDrag(el, e));
      el.querySelector('[data-op="del"]').onclick = (e) => { e.stopPropagation(); queueRemove(i); };
    });
  }
  $('pQueueBtn').onclick = () => { renderQueueSheet(); $('queueSheet').classList.add('on'); };

  // ── 入口屏事件 ──
  $('goBtn').onclick = () => startGenerate($('promptInput').value);
  $('promptInput').addEventListener('keydown', e => { if (e.key === 'Enter') startGenerate($('promptInput').value); });
  $('openLib').onclick = () => setScreen('library');
  $('libBack').onclick = () => { setScreen('home'); renderResume(); };
  // 旧的静态 .mood 绑定已由 renderModeChips 接管

  // ── 启动 ──
  loadVoices();
  loadLibrary();
  updateMiniPlayer();
})();
