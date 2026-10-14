/* QQ 群相册备份 · WebUI 前端逻辑 */
'use strict';

const state = {
  view: 'albums',
  overview: null,
  filters: { album_id: '', uploader: '', tag_id: '', q: '', page: 1, per_page: 60,
             order: 'upload_desc', has_comment: '', has_like: '', downloaded: '', thumb_only: '',
             aiDateFrom: '', aiDateTo: '' },
  photos: null,
  detail: null,
  detailStack: [],
  polling: null,
  aiStatus: null,
  aiTags: [],
  aiPolling: null,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};
const esc = (text) => String(text === null || text === undefined ? '' : text)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.remove('hidden');
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add('hidden'), 2600);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text.slice(0, 300) }; }
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if (n >= 1e4) return (n / 1e4).toFixed(2) + ' 万';
  return n.toLocaleString('zh-CN');
}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

/* ------------------------------------------------------------------ backup */
async function loadOverview() {
  state.overview = await api('/api/overview');
  const s = state.overview.stats || {};
  $('#brandSub').textContent = `群 ${state.overview.group_id}`;
  const ai = state.overview.ai || {};
  $('#dbInfo').innerHTML =
    `相册 <b>${fmtNum(s.albums)}</b> · 照片 <b>${fmtNum(s.photos)}</b><br>` +
    `已下载 <b>${fmtNum(s.photos_downloaded)}</b>（${fmtBytes(s.downloaded_bytes)}）<br>` +
    `仅缩略图 <b>${fmtNum(s.photos_thumb_only || 0)}</b><br>` +
    `评论 <b>${fmtNum(s.comments)}</b> · 点赞 <b>${fmtNum(s.likes)}</b><br>` +
    `动态 <b>${fmtNum(s.feeds)}</b> · 成员 <b>${fmtNum(s.members)}</b><br>` +
    `上传者 <b>${fmtNum(s.uploaders)}</b><br>` +
    `AI 索引 <b>${fmtNum(ai.embedded || 0)}</b> / ${fmtNum(ai.local_files || 0)}`;
  return state.overview;
}

function setStatus(text, kind) {
  const pill = $('#statusPill');
  pill.textContent = text;
  pill.className = 'status' + (kind ? ' ' + kind : '');
}

function renderProgress(sync) {
  const wrap = $('#progressWrap');
  if (!sync || (!sync.running && sync.stage === 'idle')) {
    wrap.classList.add('hidden');
    setStatus(sync && sync.running ? '运行中' : (sync && sync.error ? '出错' : '就绪'),
              sync && sync.error ? 'err' : (sync && sync.running ? 'busy' : ''));
    return;
  }
  wrap.classList.remove('hidden');
  const pct = sync.total ? Math.min(100, (sync.current / sync.total) * 100) : (sync.running ? 35 : 100);
  $('#progressFill').style.width = pct.toFixed(1) + '%';
  const stageNames = { crawl: '抓取中', albums: '相册', photos: '照片', feeds: '动态/评论',
                       members: '成员', download: '下载中', done: '完成', error: '错误', cancelled: '已取消', idle: '空闲' };
  $('#progressText').textContent =
    `${stageNames[sync.stage] || sync.stage} · ${sync.message || ''} ` +
    `(${fmtNum(sync.current)}/${fmtNum(sync.total)})`;
  setStatus(sync.running ? '运行中…' : (sync.error ? '出错' : '完成'), sync.running ? 'busy' : (sync.error ? 'err' : 'ok'));
}

async function pollSync() {
  try {
    const sync = await api('/api/sync/status');
    renderProgress(sync);
    if (sync.running) {
      if (!state.polling) state.polling = setInterval(pollSync, 1500);
    } else if (state.polling) {
      clearInterval(state.polling);
      state.polling = null;
      state.detailStack = [];
      await loadOverview();
      render();
      if (state.view === 'photos') loadPhotos();
    }
  } catch (e) { /* keep polling quiet */ }
}

/* ------------------------------------------------------------------ views */
function render() {
  const view = $('#view');
  view.innerHTML = '';
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === state.view);
  });
  if (state.view === 'albums') renderAlbums(view);
  else if (state.view === 'photos') { renderPhotoHeader(view); loadPhotos(); }
  else if (state.view === 'videos') renderVideos(view);
  else if (state.view === 'ai') renderAi(view);
  else if (state.view === 'uploaders') renderUploaders(view);
  else if (state.view === 'comments') renderComments(view);
  else if (state.view === 'gallery') renderGallery(view);
  else if (state.view === 'timeline') renderTimeline(view);
}

/* --------------------------------------------------------------- 视频 */
// 视频地址是引擎现场签发的、会过期，所以库里存的 file_path 才是本体：
// 已下载的用 <video> 播本地文件，没下载的显示封面 + 提示。
const videoState = { page: 1, album_id: '', downloaded: '', data: null, busy: false };

async function renderVideos(root) {
  const head = el('div', 'view-head');
  head.appendChild(el('h2', '', '视频'));
  const stats = (state.overview && state.overview.videos) || {};
  head.appendChild(el('div', 'ai-hint',
    `库内 ${fmtNum(stats.total || 0)} 个，已下载 ${fmtNum(stats.done || 0)}，`
    + `失败 ${fmtNum(stats.failed || 0)}，合计 ${fmtBytes(stats.bytes || 0)}`));

  const filters = el('div', 'ai-row');
  const albumSel = el('select', 'select');
  albumSel.appendChild(new Option('全部相册', ''));
  ((state.overview && state.overview.albums) || []).forEach((a) =>
    albumSel.appendChild(new Option(`${a.title} (${a.stored})`, a.album_id)));
  albumSel.value = videoState.album_id;
  albumSel.onchange = () => { videoState.album_id = albumSel.value; videoState.page = 1; renderVideos(root); };
  filters.appendChild(albumSel);

  const dlSel = el('select', 'select');
  dlSel.appendChild(new Option('全部', ''));
  dlSel.appendChild(new Option('已下载', '1'));
  dlSel.appendChild(new Option('未下载', '0'));
  dlSel.value = videoState.downloaded;
  dlSel.onchange = () => { videoState.downloaded = dlSel.value; videoState.page = 1; renderVideos(root); };
  filters.appendChild(dlSel);

  const reload = el('button', 'btn tiny', '刷新列表');
  reload.onclick = () => renderVideos(root);
  filters.appendChild(reload);
  head.appendChild(filters);
  root.appendChild(head);

  // ⚠️ 取列表是 async 的，而这期间 render() 可能被再次触发（会把整个视图清空重建）。
  //    所以结果必须写进一个"重新取回来"的容器里，而不是闭包里那个 root ——
  //    否则卡片会 append 到一个已经脱离文档的节点上，页面看起来就是空的。
  let body = root.querySelector('.video-body');
  if (!body) {
    body = el('div', 'video-body');
    root.appendChild(body);
  }

  const params = new URLSearchParams({ page: String(videoState.page), per_page: '60' });
  if (videoState.album_id) params.set('album_id', videoState.album_id);
  if (videoState.downloaded) params.set('downloaded', videoState.downloaded);
  try {
    videoState.data = await api('/api/videos?' + params.toString());
  } catch (e) {
    body.appendChild(el('div', 'empty', '读取视频列表失败：' + esc(e.message)));
    return;
  }
  const fresh = root.querySelector('.video-body');
  if (fresh) body = fresh;      // 期间被重建过 → 用新的容器
  if (!root.isConnected) return;  // 已经切到别的视图了，别再画
  body.innerHTML = '';

  const items = (videoState.data && videoState.data.items) || [];
  if (!items.length) {
    const hint = (state.overview && state.overview.videos && state.overview.videos.total)
      ? '这个筛选下没有视频。'
      : '还没有视频记录 —— 先在左栏勾上「视频元数据」，点「开始抓取元数据」。'
        + '视频清单来自 QQ Agent 的引擎，所以抓的时候它要在运行。';
    body.appendChild(el('div', 'empty', hint));
    return;
  }

  const grid = el('div', 'video-grid');
  items.forEach((v) => {
    const card = el('div', 'video-card');
    const downloaded = v.file_status === 'done';
    if (downloaded) {
      const player = document.createElement('video');
      player.controls = true;
      player.preload = 'metadata';
      player.poster = `/api/video-cover?video_id=${encodeURIComponent(v.video_id)}`;
      player.src = `/api/video?video_id=${encodeURIComponent(v.video_id)}`;
      card.appendChild(player);
    } else {
      const cover = el('img', 'video-cover');
      cover.loading = 'lazy';
      cover.src = `/api/video-cover?video_id=${encodeURIComponent(v.video_id)}`;
      cover.onerror = () => { cover.style.display = 'none'; };
      card.appendChild(cover);
      card.appendChild(el('div', 'video-badge',
        v.file_status === 'failed' ? '下载失败' : '未下载'));
    }
    const meta = el('div', 'video-meta');
    const dur = v.duration_ms ? `${Math.round(v.duration_ms / 1000)}s` : '';
    const size = v.file_size ? fmtBytes(v.file_size) : '';
    const dim = (v.width && v.height) ? `${v.width}×${v.height}` : '';
    meta.appendChild(el('div', 'video-title', esc(v.album_title || v.album_id || '未知相册')));
    meta.appendChild(el('div', 'video-sub',
      [dim, dur, size].filter(Boolean).map(esc).join(' · ') || '—'));
    meta.appendChild(el('div', 'video-sub', esc((v.upload_time || '').slice(0, 16))));
    if (downloaded) {
      const dl = el('a', 'btn tiny', '另存为');
      dl.href = `/api/video-file?video_id=${encodeURIComponent(v.video_id)}`;
      meta.appendChild(dl);
    }
    if (v.file_error) {
      meta.appendChild(el('div', 'video-sub', `<span style="color:#ffb3b3">${esc(v.file_error.slice(0, 80))}</span>`));
    }
    card.appendChild(meta);
    grid.appendChild(card);
  });
  body.appendChild(grid);

  const total = videoState.data.total || 0;
  const pages = Math.max(1, Math.ceil(total / (videoState.data.per_page || 60)));
  if (pages > 1) {
    const pager = el('div', 'ai-row');
    const prev = el('button', 'btn tiny', '上一页');
    prev.disabled = videoState.page <= 1;
    prev.onclick = () => { videoState.page -= 1; renderVideos(root); };
    const next = el('button', 'btn tiny', '下一页');
    next.disabled = videoState.page >= pages;
    next.onclick = () => { videoState.page += 1; renderVideos(root); };
    pager.appendChild(prev);
    pager.appendChild(el('span', 'ai-hint', `第 ${videoState.page} / ${pages} 页，共 ${total} 个`));
    pager.appendChild(next);
    body.appendChild(pager);
  }
}

/* --------------------------------------------------------------- AI 检索 */
const aiState = {
  mode: 'text',          // text | image | similar
  query: '',
  similar: '',
  tags: [],
  results: null,
  busy: false,
  uploading: false,
  sort: 'relevance',     // 结果排序（服务端 /api/ai/search?sort=…）
};

// 与后端 AI_SORT_KEYS 对应
const AI_SORT_OPTIONS = [
  ['relevance', '排序：相关度'],
  ['similar', '排序：纯语义相似度'],
  ['newest', '排序：最新上传'],
  ['oldest', '排序：最早上传'],
  ['uploader', '排序：上传者'],
  ['interactions', '排序：点赞+评论'],
];

async function loadAiStatus() {
  try {
    state.aiStatus = await api('/api/ai/status');
  } catch (e) {
    state.aiStatus = { available: false, reason: String(e.message) };
  }
  const info = $('#aiInfo');
  const status = state.aiStatus || {};
  const stats = status.stats || {};
  if (!status.available) {
    info.innerHTML = `<span style="color:#ffb3b3">模型不可用</span><br>${esc(status.reason || '')}`;
  } else {
    const indexed = status.indexed || 0;
    const total = status.local_files || 0;
    const pct = total ? Math.round(indexed / total * 100) : 0;
    info.innerHTML =
      `模型 <b>${esc(status.model || '')}</b><br>` +
      `已索引 <b>${fmtNum(indexed)}</b> / ${fmtNum(total)}（${pct}%）<br>` +
      `标签 <b>${fmtNum(stats.tags_used || 0)}</b> 个 · 记录 <b>${fmtNum(stats.ai_tag_rows || 0)}</b> 条`;
  }
  const st = status.state || {};
  if (st.running) {
    $('#btnAiIndex').disabled = true;
    setStatus(`AI ${st.stage} ${st.current}/${st.total}`, 'busy');
  } else {
    $('#btnAiIndex').disabled = false;
  }

  // caption model
  const capInfo = $('#captionInfo');
  const capState = status.caption_state || {};
  if (!status.caption_available) {
    capInfo.innerHTML = `<span style="color:#ffb3b3">不可用</span><br>${esc(status.caption_reason || '')}`;
  } else {
    const captioned = status.captioned || 0;
    const total = status.local_files || 0;
    capInfo.innerHTML =
      `模型 <b>${esc(status.caption_model || '')}</b><br>` +
      `已生成 <b>${fmtNum(captioned)}</b> / ${fmtNum(total)}（${total ? Math.round(captioned / total * 100) : 0}%）`;
  }
  $('#btnCaption').disabled = !!capState.running;
  return status;
}

async function loadAiTags() {
  const data = await api('/api/ai/tags');
  aiState.vocab = data.items || [];
  return aiState.vocab;
}

function renderAi(container) {
  const status = state.aiStatus || {};
  const panel = el('div', 'ai-panel');

  const row1 = el('div', 'ai-row');
  const search = el('div', 'ai-search');
  search.innerHTML = '<span>✨</span>';
  const input = el('input');
  input.id = 'aiQuery';
  input.placeholder = '用中文或英文描述你要找的图片，例如：夜晚的城市街道 / 一只猫 / 动漫插画 / 电脑截图';
  input.value = aiState.query;
  search.appendChild(input);
  row1.appendChild(search);
  const go = el('button', 'btn primary', '语义搜索');
  go.onclick = () => runAiText();
  row1.appendChild(go);
  panel.appendChild(row1);

  input.onkeydown = (event) => { if (event.key === 'Enter') runAiText(); };

  const row2 = el('div', 'ai-row');
  const dz = el('div', 'dropzone', '🖼 拖入图片，或点击选择一张本地图片 → 以图搜图（也可直接 Ctrl+V 粘贴）');
  dz.style.flex = '1';
  const fileInput = el('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  fileInput.style.display = 'none';
  fileInput.onchange = () => { if (fileInput.files[0]) runAiImage(fileInput.files[0]); };
  dz.onclick = () => fileInput.click();
  dz.ondragover = (event) => { event.preventDefault(); dz.classList.add('over'); };
  dz.ondragleave = () => dz.classList.remove('over');
  dz.ondrop = (event) => {
    event.preventDefault();
    dz.classList.remove('over');
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) runAiImage(file);
  };
  panel.appendChild(dz);
  panel.appendChild(fileInput);
  row2.appendChild(el('span', 'ai-hint',
    '提示：任意图片都能搜；相似度按余弦距离排序。'));
  panel.appendChild(row2);

  if (aiState.similar) {
    const row3 = el('div', 'ai-row');
    row3.appendChild(el('span', 'ai-hint', `当前：以「${esc(aiState.similarLabel || aiState.similar)}」找相似`));
    const clear = el('button', 'btn tiny ghost', '取消');
    clear.onclick = () => { aiState.similar = ''; aiState.similarLabel = ''; render(); };
    row3.appendChild(clear);
    panel.appendChild(row3);
  }

  // filters: album / uploader / date range
  const row4 = el('div', 'ai-row');
  const albumSel = el('select', 'select');
  albumSel.appendChild(new Option('全部相册', ''));
  ((state.overview && state.overview.albums) || []).forEach((a) =>
    albumSel.appendChild(new Option(`${a.title} (${a.stored})`, a.album_id)));
  albumSel.value = state.filters.album_id || '';
  albumSel.onchange = () => { state.filters.album_id = albumSel.value; };
  row4.appendChild(albumSel);

  const upSel = el('select', 'select');
  upSel.appendChild(new Option('全部上传者', ''));
  (state.uploaders || []).forEach((u) =>
    upSel.appendChild(new Option(`${u.nick || u.uin} (${u.photos})`, u.uin)));
  upSel.value = state.filters.uploader || '';
  upSel.onchange = () => { state.filters.uploader = upSel.value; };
  row4.appendChild(upSel);

  const from = el('input', 'input');
  from.type = 'date';
  from.title = '起始日期';
  from.value = state.filters.aiDateFrom || '';
  from.onchange = () => { state.filters.aiDateFrom = from.value; };
  const to = el('input', 'input');
  to.type = 'date';
  to.title = '结束日期';
  to.value = state.filters.aiDateTo || '';
  to.onchange = () => { state.filters.aiDateTo = to.value; };
  row4.appendChild(el('span', 'ai-hint', '时间：'));
  row4.appendChild(from);
  row4.appendChild(el('span', 'ai-hint', '→'));
  row4.appendChild(to);

  const apply = el('button', 'btn tiny', '应用筛选');
  apply.onclick = () => { aiState.query ? runAiText() : runAiBrowse(); };
  row4.appendChild(apply);

  // 排序：相似度是**相对**分数（SigLIP2 余弦挤在 0.85~1.0），光看分数没什么区分度，
  // 所以给出按时间/上传者/互动的重排，改完立即重跑当前检索。
  const sortSel = el('select', 'select');
  AI_SORT_OPTIONS.forEach(([value, label]) => sortSel.appendChild(new Option(label, value)));
  sortSel.value = aiState.sort || 'relevance';
  sortSel.title = '结果排序方式';
  sortSel.onchange = () => {
    aiState.sort = sortSel.value;
    if (aiState.similar) runAiSimilar(aiState.similar);
    else if (aiState.query) runAiText();
    else runAiBrowse();
  };
  row4.appendChild(sortSel);
  panel.appendChild(row4);

  // AI tag cloud
  const tagRow = el('div', 'ai-row');
  tagRow.appendChild(el('span', 'ai-hint', 'AI 标签：'));
  panel.appendChild(tagRow);
  const cloud = el('div', 'tag-cloud');
  const vocab = (state.aiTags || []).slice(0, 60);
  vocab.forEach((tag) => {
    const chip = el('span', 'tag-chip' + (aiState.tags.includes(tag.tag_id) ? ' on' : ''),
      `${esc(tag.name)}<span class="n">${fmtNum(tag.photos)}</span>`);
    chip.style.borderColor = aiState.tags.includes(tag.tag_id) ? tag.color : '';
    chip.onclick = () => {
      const index = aiState.tags.indexOf(tag.tag_id);
      if (index >= 0) aiState.tags.splice(index, 1);
      else aiState.tags.push(tag.tag_id);
      render();
      runAiBrowse();
    };
    cloud.appendChild(chip);
  });
  if (!vocab.length) cloud.appendChild(el('span', 'ai-hint', '还没有标签，先建立 AI 索引。'));
  panel.appendChild(cloud);
  container.appendChild(panel);

  // results
  const head = el('div', 'section-head');
  head.appendChild(el('h2', '', '检索结果'));
  const meta = el('div', 'row');
  if (aiState.results) {
    meta.appendChild(el('span', 'ai-hint',
      `${aiState.results.mode === 'text' ? '文本语义' : aiState.results.mode === 'image' ? '以图搜图' : aiState.results.mode === 'similar' ? '相似图片' : '按标签浏览'}` +
      ` · ${fmtNum(aiState.results.total || 0)} 张`));
  }
  head.appendChild(meta);
  container.appendChild(head);

  const grid = el('div', 'photo-grid');
  grid.id = 'aiGrid';
  container.appendChild(grid);
  renderAiResults();
}

function renderAiResults() {
  const grid = $('#aiGrid');
  if (!grid) return;
  grid.innerHTML = '';
  const results = aiState.results;
  if (!results) {
    grid.style.gridTemplateColumns = '1fr';
    grid.appendChild(el('div', 'empty',
      '输入描述后回车，或拖入一张图片；也可以直接点上面的 AI 标签浏览。'));
    return;
  }
  grid.style.gridTemplateColumns = '';
  if (!results.items.length) {
    grid.style.gridTemplateColumns = '1fr';
    grid.appendChild(el('div', 'empty', '没有匹配的图片'));
    return;
  }
  results.items.forEach((photo, index) => {
    const card = el('div', 'photo-card');
    const wrap = el('div', 'thumb-wrap');
    const img = el('img', 'photo-thumb');
    img.loading = 'lazy';
    img.src = `/api/thumb?photo_id=${encodeURIComponent(photo.photo_id)}&size=320`;
    wrap.appendChild(img);
    if (typeof photo.score === 'number' && results.mode && results.mode !== 'browse') {
      wrap.appendChild(el('span', 'score-pill', (photo.score * 100).toFixed(1) + '%'));
    }
    card.appendChild(wrap);    const find = el('button', 'btn tiny ghost', '找相似');
    find.style.position = 'absolute';
    find.style.left = '7px';
    find.style.bottom = '7px';
    find.style.display = 'none';
    find.onclick = (event) => {
      event.stopPropagation();
      runAiSimilar(photo.photo_id, photo.description || photo.name || photo.photo_id);
    };
    wrap.appendChild(find);
    card.onmouseenter = () => { find.style.display = 'block'; };
    card.onmouseleave = () => { find.style.display = 'none'; };
    card.style.position = 'relative';
    const tags = (photo.tags || []).slice(0, 3).map((t) => esc(t.name)).join(' · ');
    const source = photo.match === 'caption' ? '描述匹配' :
      (typeof photo.vector_score === 'number' && photo.vector_score > 0 ? '语义' : '');
    const caption = (photo.caption || '').slice(0, 90);
    card.appendChild(el('div', 'photo-foot',
      `<div class="who">${esc(photo.uploader_nick || photo.uploader_uin || '未知')}</div>
       <div>${esc((photo.upload_time || '').slice(0, 16))}${source ? ` · <span style="color:#a8ccff">${source}</span>` : ''}</div>
       ${caption ? `<div style="color:#d9b8ff">“${esc(caption)}”</div>` : ''}
       <div style="color:#93a1b5">${tags}</div>`));
    card.onclick = () => openDetail(photo.photo_id, results.items.map((p) => p.photo_id), index);
    grid.appendChild(card);
  });
}

function aiFilterParams(extra) {
  const params = new URLSearchParams(extra || {});
  if (state.filters.album_id) params.set('album_id', state.filters.album_id);
  if (state.filters.uploader) params.set('uploader', state.filters.uploader);
  if (state.filters.aiDateFrom) params.set('date_from', state.filters.aiDateFrom);
  if (state.filters.aiDateTo) params.set('date_to', state.filters.aiDateTo);
  if (aiState.sort) params.set('sort', aiState.sort);
  aiState.tags.forEach((t) => params.append('ai_tag', String(t)));
  return params;
}

async function runAiText() {
  const input = $('#aiQuery');
  const query = (input ? input.value : aiState.query).trim();
  aiState.query = query;
  if (!query) return;
  aiState.similar = '';
  aiState.busy = true;
  setStatus('语义检索中…', 'busy');
  try {
    const params = aiFilterParams({ q: query, top_k: '120' });
    aiState.results = await api('/api/ai/search?' + params.toString());
  } catch (e) {
    toast('检索失败：' + e.message);
    aiState.results = { mode: 'text', items: [], total: 0 };
  }
  aiState.busy = false;
  setStatus('就绪');
  renderAiResults();
  const head = document.querySelector('.section-head .ai-hint');
  if (head && aiState.results) head.textContent = `文本语义 · ${fmtNum(aiState.results.total || 0)} 张`;
}

async function runAiBrowse() {
  aiState.busy = true;
  try {
    const params = aiFilterParams({ top_k: '120' });
    aiState.results = await api('/api/ai/search?' + params.toString());
  } catch (e) {
    toast('筛选失败：' + e.message);
  }
  aiState.busy = false;
  renderAiResults();
}

async function runAiImage(file) {
  aiState.busy = true;
  aiState.similar = '';
  setStatus('以图搜图中…', 'busy');
  try {
    const form = new FormData();
    form.append('file', file, file.name || 'query.jpg');
    form.append('top_k', '120');
    if (state.filters.album_id) form.append('album_id', state.filters.album_id);
    if (state.filters.uploader) form.append('uploader', state.filters.uploader);
    if (state.filters.aiDateFrom) form.append('date_from', state.filters.aiDateFrom);
    if (state.filters.aiDateTo) form.append('date_to', state.filters.aiDateTo);
    if (aiState.sort) form.append('sort', aiState.sort);
    const res = await fetch('/api/ai/search-image', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    aiState.results = data;
    toast(`已用「${file.name}」找到 ${data.total} 张相似图片`);
  } catch (e) {
    toast('以图搜图失败：' + e.message);
  }
  aiState.busy = false;
  setStatus('就绪');
  renderAiResults();
}

async function runAiSimilar(photoId, label) {
  aiState.similar = photoId;
  aiState.similarLabel = label || '';
  aiState.query = '';
  state.view = 'ai';
  render();
  setStatus('查找相似图片…', 'busy');
  try {
    const params = aiFilterParams({ similar_to: photoId, top_k: '120' });
    aiState.results = await api('/api/ai/search?' + params.toString());
    aiState.results.mode = 'similar';
  } catch (e) {
    toast('相似检索失败：' + e.message);
  }
  setStatus('就绪');
  renderAiResults();
}


function renderAlbumGrid(container) {
  const overview = state.overview;
  const grid = el('div', 'album-grid');
  const albums = (overview && overview.albums) || [];
  if (!albums.length) {
    container.appendChild(el('div', 'empty',
      '还没有数据。请点击左侧「开始抓取元数据」，程序会用已登录的 Edge 抓取群相册。'));
    return;
  }
  albums.forEach((album) => {
    const card = el('div', 'album-card');
    const cover = album.cover_url
      ? `style="background-image:url('/api/cover?album_id=${encodeURIComponent(album.album_id)}')"`
      : '';
    card.innerHTML =
      `<div class="album-cover" ${cover}><span class="count">${fmtNum(album.stored)} 张</span></div>
       <div class="album-info">
         <div class="album-title" title="${esc(album.title)}">${esc(album.title || '(无标题)')}</div>
         <div class="album-desc">${esc(album.description || '')}</div>
         <div class="album-meta"><span>${esc(album.creator_nick || '')}</span>
           <span>${esc((album.create_time || '').slice(0, 10))}</span></div>
         <div class="album-meta"><span class="muted">已下载 ${fmtNum(album.downloaded)}</span>
           <span class="muted">${album.stored ? Math.round(album.downloaded / album.stored * 100) : 0}%</span></div>
       </div>`;
    card.onclick = () => {
      state.filters.album_id = album.album_id;
      state.filters.page = 1;
      state.view = 'photos';
      render();
    };
    grid.appendChild(card);
  });
  container.appendChild(grid);
}

function renderAlbums(container) {
  const s = (state.overview && state.overview.stats) || {};
  const stats = el('div', 'stat-grid');
  [
    ['相册', s.albums], ['照片', s.photos], ['已下载原图', s.photos_downloaded],
    ['仅缩略图', s.photos_thumb_only], ['评论', s.comments], ['点赞', s.likes],
    ['动态', s.feeds], ['上传者', s.uploaders], ['占磁盘', fmtBytes(s.downloaded_bytes)],
  ].forEach(([label, value]) => {
    stats.appendChild(el('div', 'stat-card', `<div class="label">${label}</div>
      <div class="value">${typeof value === 'number' ? fmtNum(value) : value}</div>`));
  });
  container.appendChild(stats);

  const head = el('div', 'section-head', '<h2>相册</h2>');
  const right = el('div', 'row');
  const autoTag = el('button', 'btn tiny', '按上传者自动打标签');
  autoTag.onclick = async () => {
    autoTag.disabled = true;
    const res = await api('/api/tags/auto', { method: 'POST', body: '{}' });
    toast('已生成 ' + res.tags + ' 个上传者标签');
    autoTag.disabled = false;
  };
  right.appendChild(autoTag);
  head.appendChild(right);
  container.appendChild(head);
  renderAlbumGrid(container);

  const sync = (state.overview && state.overview.sync) || null;
  if (sync && sync.log && sync.log.length) {
    const panel = el('div', 'panel');
    panel.style.marginTop = '18px';
    panel.appendChild(el('div', 'panel-title', '任务日志'));
    panel.appendChild(el('div', 'log-box', esc(sync.log.join('\n'))));
    container.appendChild(panel);
  }
}

function renderPhotoHeader(container) {
  const bar = el('div', 'section-head');
  const title = el('h2', '', '照片');
  bar.appendChild(title);
  const filters = el('div', 'row');
  const albumSel = el('select', 'select');
  albumSel.appendChild(new Option('全部相册', ''));
  ((state.overview && state.overview.albums) || []).forEach((a) => {
    albumSel.appendChild(new Option(`${a.title} (${a.stored})`, a.album_id));
  });
  albumSel.value = state.filters.album_id;
  albumSel.onchange = () => { state.filters.album_id = albumSel.value; state.filters.page = 1; loadPhotos(); };
  filters.appendChild(albumSel);

  const upSel = el('select', 'select');
  upSel.appendChild(new Option('全部上传者', ''));
  state.uploaders.forEach((u) => upSel.appendChild(new Option(`${u.nick || u.uin} (${u.photos})`, u.uin)));
  upSel.value = state.filters.uploader;
  upSel.onchange = () => { state.filters.uploader = upSel.value; state.filters.page = 1; loadPhotos(); };
  filters.appendChild(upSel);

  const tagSel = el('select', 'select');
  tagSel.appendChild(new Option('全部标签', ''));
  state.tags.forEach((t) => tagSel.appendChild(new Option(`${t.name} (${t.photos})`, t.tag_id)));
  tagSel.value = state.filters.tag_id;
  tagSel.onchange = () => { state.filters.tag_id = tagSel.value; state.filters.page = 1; loadPhotos(); };
  filters.appendChild(tagSel);

  const cmt = el('button', 'btn tiny' + (state.filters.has_comment === '1' ? ' primary' : ''), '有评论');
  cmt.onclick = () => { state.filters.has_comment = state.filters.has_comment === '1' ? '' : '1'; state.filters.page = 1; render(); };
  const like = el('button', 'btn tiny' + (state.filters.has_like === '1' ? ' primary' : ''), '有点赞');
  like.onclick = () => { state.filters.has_like = state.filters.has_like === '1' ? '' : '1'; state.filters.page = 1; render(); };
  const dl = el('button', 'btn tiny' + (state.filters.downloaded === '0' ? ' primary' : ''), '未下载');
  dl.onclick = () => { state.filters.downloaded = state.filters.downloaded === '0' ? '' : '0';
                       state.filters.thumb_only = ''; state.filters.page = 1; render(); };
  const full = el('button', 'btn tiny' + (state.filters.downloaded === 'full' ? ' primary' : ''), '已存原图');
  full.onclick = () => { state.filters.downloaded = state.filters.downloaded === 'full' ? '' : 'full';
                         state.filters.thumb_only = ''; state.filters.page = 1; render(); };
  const stub = el('button', 'btn tiny' + (state.filters.thumb_only === '1' ? ' primary' : ''), '仅缩略图');
  stub.onclick = () => { state.filters.thumb_only = state.filters.thumb_only === '1' ? '' : '1';
                         state.filters.downloaded = ''; state.filters.page = 1; render(); };
  filters.appendChild(cmt); filters.appendChild(like);
  filters.appendChild(dl); filters.appendChild(full); filters.appendChild(stub);
  bar.appendChild(filters);
  container.appendChild(bar);

  const grid = el('div', 'photo-grid');
  grid.id = 'photoGrid';
  container.appendChild(grid);
  const pager = el('div', 'pager');
  pager.id = 'photoPager';
  container.appendChild(pager);
}

async function loadPhotos() {
  const grid = $('#photoGrid');
  if (!grid) return;
  grid.innerHTML = '<div class="empty">加载中…</div>';
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([k, v]) => { if (v !== '' && v !== null && v !== undefined) params.set(k, v); });
  const data = await api('/api/photos?' + params.toString());
  state.photos = data;
  grid.innerHTML = '';
  if (!data.items.length) {
    grid.appendChild(el('div', 'empty', '没有匹配的照片'));
    $('#photoPager').innerHTML = '';
    return;
  }
  data.items.forEach((photo, index) => {
    const card = el('div', 'photo-card');
    const badges = [];
    if (photo.file_status === 'done') badges.push('<span class="badge green">已下载</span>');
    else if (photo.file_status === 'thumb-only') badges.push('<span class="badge orange">仅缩略图</span>');
    else if (photo.file_status === 'error') badges.push('<span class="badge orange">失败</span>');
    if (photo.comment_count > 0) badges.push(`<span class="badge blue">💬 ${photo.comment_count}</span>`);
    if (photo.like_count > 0) badges.push(`<span class="badge pink">♥ ${photo.like_count}</span>`);
    if (photo.is_animated) badges.push('<span class="badge purple">动图</span>');
    else if (photo.is_video) badges.push('<span class="badge">视频</span>');
    card.innerHTML =
      `<div class="badges">${badges.join('')}</div>
       <img class="photo-thumb" loading="lazy" src="/api/thumb?photo_id=${encodeURIComponent(photo.photo_id)}&size=320" />
       <div class="photo-foot">
         <div class="who">${esc(photo.uploader_nick || photo.uploader_uin || '未知')}</div>
         <div>${esc((photo.upload_time || '').slice(0, 16))}</div>
       </div>`;
    card.onclick = () => openDetail(photo.photo_id, data.items.map((p) => p.photo_id), index);
    grid.appendChild(card);
  });

  const pager = $('#photoPager');
  pager.innerHTML = '';
  const prev = el('button', 'btn tiny', '上一页');
  const next = el('button', 'btn tiny', '下一页');
  prev.disabled = data.page <= 1;
  next.disabled = data.page >= data.pages;
  prev.onclick = () => { state.filters.page = data.page - 1; loadPhotos(); $('#view').scrollTop = 0; };
  next.onclick = () => { state.filters.page = data.page + 1; loadPhotos(); $('#view').scrollTop = 0; };
  pager.append(prev, el('span', '', `第 ${data.page} / ${data.pages} 页 · 共 ${fmtNum(data.total)} 张`), next);
}

function renderUploaders(container) {
  const head = el('div', 'section-head', '<h2>上传者排行</h2>');
  container.appendChild(head);
  const table = el('table', 'data');
  table.innerHTML = '<thead><tr><th>#</th><th>上传者</th><th>QQ</th><th>照片</th><th>评论</th><th>点赞</th></tr></thead>';
  const tbody = el('tbody');
  state.topUploaders.forEach((u, i) => {
    const tr = el('tr');
    tr.innerHTML = `<td>${i + 1}</td>
      <td><span class="avatar">${esc((u.nick || '?').slice(0, 1))}</span>${esc(u.nick || '未知')}</td>
      <td class="muted">${esc(u.uin || '')}</td>
      <td>${fmtNum(u.photos)}</td><td>${fmtNum(u.comments)}</td><td>${fmtNum(u.likes)}</td>`;
    tr.onclick = () => {
      state.filters.uploader = u.uin; state.filters.page = 1; state.view = 'photos'; render();
    };
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.appendChild(table);
}

function renderCommentRow(comment) {
  const node = el('div', 'comment' + (comment.is_reply ? ' reply' : ''));
  node.innerHTML =
    `<div class="head"><span>${esc(comment.author_nick || comment.author_uin || '匿名')}</span>
       <span>${esc((comment.comment_time || '').slice(0, 16))}</span></div>
     <div>${esc(comment.content || '')}</div>`;
  return node;
}

async function renderComments(container) {
  const head = el('div', 'section-head', '<h2>评论流（最新 200 条）</h2>');
  container.appendChild(head);
  const data = await api('/api/comments?limit=200');
  if (!data.items.length) {
    container.appendChild(el('div', 'empty', '还没有评论数据，先抓取「评论 / 点赞 / 动态」。'));
    return;
  }
  const list = el('div', 'comment-list');
  data.items.forEach((c) => {
    const node = renderCommentRow(c);
    if (c.photo_id) {
      const link = el('a', '', '查看照片');
      link.href = 'javascript:void(0)';
      link.onclick = () => openDetail(c.photo_id, [], 0);
      node.appendChild(link);
    }
    list.appendChild(node);
  });
  container.appendChild(list);
}

async function renderGallery(container) {
  const head = el('div', 'section-head', '<h2>随机精选</h2>');
  const again = el('button', 'btn tiny', '换一批');
  again.onclick = () => render();
  head.appendChild(again);
  container.appendChild(head);
  const data = await api('/api/gallery?limit=60');
  const grid = el('div', 'photo-grid');
  data.items.forEach((photo, i) => {
    const card = el('div', 'photo-card');
    card.innerHTML = `<img class="photo-thumb" loading="lazy"
        src="/api/thumb?photo_id=${encodeURIComponent(photo.photo_id)}&size=320" />
      <div class="photo-foot"><div class="who">${esc(photo.uploader_nick || '')}</div>
      <div>${esc((photo.album_id || '').slice(0, 12))}</div></div>`;
    card.onclick = () => openDetail(photo.photo_id, data.items.map((p) => p.photo_id), i);
    grid.appendChild(card);
  });
  if (!data.items.length) {
    container.appendChild(el('div', 'empty', '还没有已下载的图片。先执行「下载原图」。'));
  }
  container.appendChild(grid);
}

async function renderTimeline(container) {
  container.appendChild(el('div', 'section-head', '<h2>上传时间线</h2>'));
  const data = await api('/api/timeline');
  if (!data.items.length) {
    container.appendChild(el('div', 'empty', '暂无时间数据'));
    return;
  }
  const max = Math.max(...data.items.map((i) => i.photos));
  const bars = el('div', 'bars');
  data.items.forEach((item) => {
    const row = el('div', 'bar-row');
    row.innerHTML = `<span class="muted">${esc(item.month || '未知')}</span>
      <div class="bar" style="width:${(item.photos / max * 100).toFixed(1)}%"></div>
      <span>${fmtNum(item.photos)}</span>`;
    bars.appendChild(row);
  });
  container.appendChild(bars);
}

/* ------------------------------------------------------------------ detail */
async function openDetail(photoId, stack, index) {
  state.detailStack = stack && stack.length ? stack : [photoId];
  state.detailIndex = index || 0;
  const data = await api('/api/photo?photo_id=' + encodeURIComponent(photoId));
  if (data.error) { toast('找不到该照片'); return; }
  state.detail = data;
  const photo = data.photo;
  $('#modal').classList.remove('hidden');
  $('#modalImg').src = `/api/image?photo_id=${encodeURIComponent(photoId)}`;
  $('#modalTitle').textContent = photo.description || photo.name || '照片详情';
  $('#modalSub').textContent = `${photo.upload_time || ''} · ${photo.album_title || photo.album_id || ''}`;
  $('#modalCounter').textContent = `${state.detailIndex + 1} / ${state.detailStack.length}`;

  const meta = [
    ['上传者', (photo.uploader_nick || '未知') + (photo.uploader_uin ? `（${photo.uploader_uin}）` : '')],
    ['上传时间', photo.upload_time || '—'],
    ['批次', photo.batch_id || '—'],
    ['尺寸', `${photo.width || '?'}×${photo.height || '?'}` +
      (photo.original_width ? `（原图 ${photo.original_width}×${photo.original_height}）` : '')],
    ['评论 / 点赞', `${photo.comment_count || 0} / ${photo.like_count || 0}`],
    ['本地文件', photo.file_path ? `${photo.file_status} · ${fmtBytes(photo.file_size)}` : '未下载'],
    ['照片ID', photo.photo_id],
  ];
  $('#modalMeta').innerHTML = meta.map(([k, v]) =>
    `<div class="k">${k}</div><div class="v">${esc(v)}</div>`).join('');

  // BLIP caption
  const capWrap = $('#modalCaptionWrap');
  if (data.caption) {
    capWrap.style.display = '';
    $('#modalCaption').textContent = data.caption;
  } else {
    capWrap.style.display = 'none';
  }

  const tags = el('div', 'tag-row');
  data.tags.forEach((t) => {
    const chip = el('span', 'tag', esc(t.name) + ' ✕');
    chip.style.borderColor = t.color;
    chip.style.color = t.color;
    chip.onclick = async () => {
      await api('/api/tags/assign', { method: 'POST',
        body: JSON.stringify({ tag_id: t.tag_id, photo_ids: [photoId], remove: true }) });
      openDetail(photoId, state.detailStack, state.detailIndex);
    };
    tags.appendChild(chip);
  });
  state.tags.forEach((t) => {
    if (data.tags.some((x) => x.tag_id === t.tag_id)) return;
    const chip = el('span', 'tag', '+ ' + esc(t.name));
    chip.style.color = t.color;
    chip.onclick = async () => {
      await api('/api/tags/assign', { method: 'POST',
        body: JSON.stringify({ tag_id: t.tag_id, photo_ids: [photoId] }) });
      openDetail(photoId, state.detailStack, state.detailIndex);
    };
    tags.appendChild(chip);
  });
  $('#modalTags').innerHTML = '';
  $('#modalTags').appendChild(tags);

  // AI tags (SigLIP2): click to search that tag, red when the photo is not indexed yet
  const aiBox = $('#modalAiTags');
  aiBox.innerHTML = '';
  const aiTags = data.ai_tags || [];
  if (!aiTags.length) {
    aiBox.appendChild(el('span', 'ai-hint',
      data.has_embedding ? '该图没有明显标签' : '该图尚未建立 AI 索引'));
  }
  aiTags.forEach((tag) => {
    const chip = el('span', 'tag', `${esc(tag.name)} <span style="opacity:.6">` +
      `${Math.round((tag.confidence || 0) * 100)}%</span>`);
    chip.style.borderColor = tag.color;
    chip.style.color = tag.color;
    chip.title = `${tag.name_en || ''}\n余弦相似度 ${(tag.score || 0).toFixed(4)}\n点击查看同类图片`;
    chip.onclick = async () => {
      closeModal();
      aiState.tags = [tag.tag_id];
      aiState.query = '';
      state.view = 'ai';
      render();
      runAiBrowse();
    };
    aiBox.appendChild(chip);
  });

  $('#commentCount').textContent = `(${data.comments.length}/${data.comment_total || 0})`;
  $('#likeCount').textContent = `(${data.likes.length}/${data.like_total || 0})`;
  const commentBox = $('#modalComments');
  commentBox.innerHTML = '';
  if (!data.comments.length) commentBox.appendChild(el('div', 'muted', '暂无评论'));
  data.comments.forEach((c) => commentBox.appendChild(renderCommentRow(c)));
  const likeBox = $('#modalLikes');
  likeBox.innerHTML = '';
  if (!data.likes.length) likeBox.appendChild(el('div', 'muted', '暂无点赞'));
  data.likes.forEach((l) => likeBox.appendChild(el('span', 'like-chip', esc(l.nick || l.uin))));

  $('#modalDownload').href = photo.file_status === 'done'
    ? `/api/file?photo_id=${encodeURIComponent(photoId)}`
    : (photo.raw_url || '#');
  $('#modalRaw').href = photo.raw_url || photo.big_url || '#';
}

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modalImg').src = '';
}

function stepPhoto(delta) {
  if (!state.detailStack.length) return;
  state.detailIndex = (state.detailIndex + delta + state.detailStack.length) % state.detailStack.length;
  openDetail(state.detailStack[state.detailIndex], state.detailStack, state.detailIndex);
}

/* ------------------------------------------------------------------- wiring */
async function loadAux() {
  const [up, tg, top] = await Promise.all([
    api('/api/uploaders'), api('/api/tags'), api('/api/top_uploaders'),
  ]);
  state.uploaders = up.items || [];
  state.tags = tg.items || [];
  state.topUploaders = top.items || [];
  try {
    const ai = await api('/api/ai/tags');
    state.aiTags = ai.items || [];
  } catch (e) {
    state.aiTags = [];
  }
}

async function pollAi() {
  const status = await loadAiStatus();
  const st = (status && status.state) || {};
  const cap = (status && status.caption_state) || {};
  if (st.running || cap.running) {
    if (!state.aiPolling) state.aiPolling = setInterval(pollAi, 3000);
  } else if (state.aiPolling) {
    clearInterval(state.aiPolling);
    state.aiPolling = null;
    await loadAiStatus();
    await loadAiTags();
    if (state.view === 'ai') render();
    const err = st.error || cap.error;
    toast(err ? ('AI 任务出错：' + err) : 'AI 任务结束');
  }
}

async function boot() {
  await loadOverview();
  await loadAux();
  await loadAiStatus();
  render();
  pollSync();
  pollAi();

  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.onclick = () => { state.view = btn.dataset.view; render(); };
  });

  let searchTimer = null;
  $('#searchInput').oninput = (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(() => {
      state.filters.q = value.trim();
      state.filters.page = 1;
      if (state.view !== 'photos') { state.view = 'photos'; render(); } else loadPhotos();
    }, 320);
  };
  $('#orderSelect').onchange = (event) => {
    state.filters.order = event.target.value;
    state.filters.page = 1;
    if (state.view !== 'photos') { state.view = 'photos'; render(); } else loadPhotos();
  };
  $('#btnRefresh').onclick = async () => {
    await loadOverview(); await loadAux();
    state.detailStack = [];
    render();
    toast('已刷新');
  };
  $('#btnSync').onclick = async () => {
    const payload = {
      albums: $('#optAlbums').checked, photos: $('#optPhotos').checked,
      feeds: $('#optFeeds').checked, members: $('#optMembers').checked,
      incremental: $('#optIncremental').checked,
      videos: $('#optVideos') ? $('#optVideos').checked : false,
    };
    const res = await api('/api/sync', { method: 'POST', body: JSON.stringify(payload) });
    toast(res.started ? '已开始抓取' : res.reason);
    pollSync();
  };
  $('#btnDownload').onclick = async () => {
    const res = await api('/api/download', { method: 'POST',
      body: JSON.stringify({ album_id: state.filters.album_id || null, retry_failed: true }) });
    toast(res.started ? '已开始下载原图' : res.reason);
    pollSync();
  };
  $('#btnVideoDownload').onclick = async () => {
    setStatus('检查视频引擎…', 'busy');
    try {
      const info = await api('/api/videos/status');
      if (!info.engine_ok) {
        toast('视频引擎不可用：' + (info.error || '未知原因'));
        setStatus('就绪');
        return;
      }
    } catch (e) {
      toast('检查视频引擎失败：' + e.message);
      setStatus('就绪');
      return;
    }
    const res = await api('/api/download-videos', { method: 'POST', body: '{}' });
    toast(res.started ? '已开始下载视频' : res.reason);
    pollSync();
  };
  $('#btnConnect').onclick = async () => {
    setStatus('连接中…', 'busy');
    try {
      await api('/api/connect', { method: 'POST', body: '{}' });
      toast('浏览器已连接');
    } catch (e) { toast('连接失败：' + e.message); }
    pollSync();
  };
  $('#btnCancel').onclick = async () => {
    await api('/api/cancel', { method: 'POST', body: '{}' });
    toast('已请求取消');
  };
  $('#btnAddTag').onclick = async () => {
    const name = $('#newTagInput').value.trim();
    if (!name) return;
    await api('/api/tags', { method: 'POST', body: JSON.stringify({ name }) });
    $('#newTagInput').value = '';
    await loadAux();
    if (state.detail) openDetail(state.detail.photo.photo_id, state.detailStack, state.detailIndex);
    toast('标签已创建');
  };
  const similarBtn = $('#btnSimilar');
  if (similarBtn) {
    similarBtn.onclick = () => {
      if (!state.detail) return;
      const photo = state.detail.photo;
      closeModal();
      runAiSimilar(photo.photo_id, photo.description || photo.name || photo.photo_id);
    };
  }

  // AI index controls
  $('#btnAiIndex').onclick = async () => {
    const res = await api('/api/ai/index', { method: 'POST', body: JSON.stringify({}) });
    toast(res.started ? '已开始建立 AI 索引（可继续浏览，速度取决于 CPU）' : res.reason);
    pollAi();
  };
  $('#btnAiRetag').onclick = async () => {
    const res = await api('/api/ai/index', { method: 'POST',
      body: JSON.stringify({ retag: true }) });
    toast(res.started ? '已开始重新打标（会覆盖已有 AI 标签）' : res.reason);
    pollAi();
  };
  $('#btnAiCancel').onclick = async () => {
    await api('/api/ai/cancel', { method: 'POST', body: '{}' });
    toast('已请求停止 AI 任务');
  };
  $('#btnCaption').onclick = async () => {
    const res = await api('/api/ai/caption', { method: 'POST', body: JSON.stringify({}) });
    toast(res.started ? '已开始生成图片描述（可继续浏览）' : res.reason);
    pollAi();
  };
  $('#btnRecaption').onclick = async () => {
    const res = await api('/api/ai/caption', { method: 'POST',
      body: JSON.stringify({ recaption: true }) });
    toast(res.started ? '已开始重新生成描述' : res.reason);
    pollAi();
  };

  // paste an image anywhere to run image search
  document.addEventListener('paste', (event) => {
    const items = (event.clipboardData && event.clipboardData.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) {
          state.view = 'ai';
          render();
          runAiImage(file);
          event.preventDefault();
        }
        return;
      }
    }
  });

  document.querySelectorAll('[data-close]').forEach((node) => { node.onclick = closeModal; });
  $('#prevPhoto').onclick = () => stepPhoto(-1);
  $('#nextPhoto').onclick = () => stepPhoto(1);
  document.addEventListener('keydown', (event) => {
    if ($('#modal').classList.contains('hidden')) return;
    if (event.key === 'Escape') closeModal();
    if (event.key === 'ArrowLeft') stepPhoto(-1);
    if (event.key === 'ArrowRight') stepPhoto(1);
  });
}

boot().catch((err) => {
  setStatus('加载失败', 'err');
  toast('初始化失败：' + err.message);
});
