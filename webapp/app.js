'use strict';
/* VieNeu Studio — client of the local gateway (127.0.0.1:8001). Same API the MCP server,
   the `speak` CLI and future clients (e.g. a YouTube narration extension) use. */
const TOKEN = document.querySelector('meta[name="vieneu-token"]').content;
const MODEL = 'vieneu-v3-turbo';
const PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/';
const LAMEJS = 'https://cdn.jsdelivr.net/npm/lamejs@1.2.1/lame.min.js';
const ADV_DEFAULTS = {temperature: 0.8, top_k: 25, top_p: 0.95, repetition_penalty: 1.2, max_chars: 256};
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const store = {get(k, d) { try { const v = localStorage.getItem('vieneu.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
               set(k, v) { try { localStorage.setItem('vieneu.' + k, JSON.stringify(v)); } catch {} }};
let VOICES = [];
let MAX_STREAMS = 4;          // from /health; parallel requests = streams - 1 (leave room for Claude/CLI)
let ENGINE_LOADING = false;
const concurrency = () => Math.max(1, Math.min(3, MAX_STREAMS - 1));

/* ---------- helpers ---------- */
function toast(msg, bad = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, bad ? 8000 : 3500);
}
class ApiError extends Error {}
async function api(path, opts = {}) {
  const headers = Object.assign({Authorization: 'Bearer ' + TOKEN}, opts.headers || {});
  let r;
  try { r = await fetch(path, Object.assign({}, opts, {headers})); }
  catch (e) { if (e.name === 'AbortError') throw e; throw new ApiError('Không kết nối được gateway. Chạy start-all.bat rồi tải lại trang.'); }
  if (!r.ok) {
    let detail = '';
    try { const j = await r.clone().json(); detail = j.detail ?? j.error?.message ?? j.error ?? j.message ?? ''; if (typeof detail !== 'string') detail = JSON.stringify(detail); }
    catch { try { detail = await r.text(); } catch {} }
    const map = {401: 'Sai token. Tải lại trang (token lấy từ file .env).', 409: detail || 'Trùng tên.', 413: 'File quá lớn.',
                 429: 'Server đang bận, thử lại sau vài giây.',
                 503: ENGINE_LOADING ? 'Đang đổi model, đợi tab Model báo xong.' : 'Model chưa chạy hoặc vừa lỗi. Chạy start-all.bat.'};
    throw new ApiError(map[r.status] || `Lỗi ${r.status}: ${detail}`.slice(0, 400));
  }
  return r;
}
function loadScript(src) {
  return new Promise((res, rej) => { const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error('Không tải được thư viện (cần internet): ' + src)); document.head.append(s); });
}
const fmtDur = s => { const m = Math.floor(s / 60); return (m ? m + ' phút ' : '') + (s - m * 60).toFixed(1) + ' giây'; };
const silence = (sec, rate) => new Int16Array(Math.max(0, Math.round(sec * rate)));
function concat(parts) {
  const out = new Int16Array(parts.reduce((n, p) => n + p.length, 0)); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; } return out;
}
function encodeWav(pcm, rate) {
  const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data');
  v.setUint32(40, pcm.length * 2, true); new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], {type: 'audio/wav'});
}
/* Smoothness: tiny fades on every segment edge (no clicks at joins) and peak normalisation. */
function fadeEdges(pcm, rate, ms = 8) {
  const n = Math.min(Math.round(rate * ms / 1000), pcm.length >> 1);
  for (let i = 0; i < n; i++) { const g = i / n; pcm[i] = pcm[i] * g; pcm[pcm.length - 1 - i] = pcm[pcm.length - 1 - i] * g; }
  return pcm;
}
function normalizePeak(pcm, targetDb = -1, maxGain = 4) {
  let peak = 1; for (let i = 0; i < pcm.length; i++) { const a = Math.abs(pcm[i]); if (a > peak) peak = a; }
  const gain = Math.min(maxGain, (32767 * Math.pow(10, targetDb / 20)) / peak);
  if (Math.abs(gain - 1) < 0.02) return pcm;
  for (let i = 0; i < pcm.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(pcm[i] * gain)));
  return pcm;
}
async function toMp3(pcm, rate, onProgress) {
  if (!window.lamejs) await loadScript(LAMEJS);
  const enc = new lamejs.Mp3Encoder(1, rate, 160), out = [], step = 1152 * 64;
  for (let i = 0; i < pcm.length; i += 1152) {
    const b = enc.encodeBuffer(pcm.subarray(i, i + 1152)); if (b.length) out.push(b);
    if (i % step === 0) { onProgress?.(i / pcm.length); await new Promise(r => setTimeout(r)); }
  }
  const e = enc.flush(); if (e.length) out.push(e);
  return new Blob(out, {type: 'audio/mpeg'});
}
async function runPool(items, worker, conc, onProgress, signal) {
  const results = new Array(items.length); let next = 0, done = 0;
  async function lane() {
    while (next < items.length) {
      if (signal?.aborted) throw new DOMException('Đã dừng', 'AbortError');
      const i = next++; results[i] = await worker(items[i], i); onProgress?.(++done, items.length);
    }
  }
  await Promise.all(Array.from({length: Math.min(conc, items.length)}, lane));
  return results;
}
/* Plays PCM pieces back-to-back while the rest is still being generated. */
class LivePlayer {
  constructor(rate) { this.rate = rate; this.ctx = new (window.AudioContext || window.webkitAudioContext)(); this.t = 0; this.srcs = []; }
  push(pcm) {
    if (!pcm.length || this.ctx.state === 'closed') return;
    const buf = this.ctx.createBuffer(1, pcm.length, this.rate), ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = this.ctx.createBufferSource(); src.buffer = buf; src.connect(this.ctx.destination);
    const at = Math.max(this.ctx.currentTime + 0.08, this.t); src.start(at); this.t = at + buf.duration; this.srcs.push(src);
  }
  stop() { for (const s of this.srcs) { try { s.stop(); } catch {} } if (this.ctx.state !== 'closed') this.ctx.close(); }
}
let CURRENT_LIVE = null;
const stopLive = () => { CURRENT_LIVE?.stop(); CURRENT_LIVE = null; };

/* ---------- synthesis (one call = POST /v1/audio/speech, raw PCM) ---------- */
function advParams() {
  const p = {};
  for (const k of Object.keys(ADV_DEFAULTS)) {
    let v = Number($('#adv-' + k).value); if (k === 'top_k' || k === 'max_chars') v = Math.round(v);
    if (v !== ADV_DEFAULTS[k]) p[k] = v;
  }
  return p;
}
function pcmFrom(buf) {
  const n = buf.byteLength >> 1, dv = new DataView(buf), pcm = new Int16Array(n);
  for (let i = 0; i < n; i++) pcm[i] = dv.getInt16(i * 2, true);
  return pcm;
}
async function synth(text, voice, signal) {
  const rate = Number($('#adv-rate').value);
  const body = Object.assign({model: MODEL, input: text, voice, response_format: 'pcm', sample_rate: rate}, advParams());
  const r = await api('/v1/audio/speech', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal});
  const sr = Number(r.headers.get('x-sample-rate')) || rate, pcm = pcmFrom(await r.arrayBuffer());
  if (!pcm.length) throw new ApiError('Server trả về audio rỗng.');
  return {pcm, rate: sr};
}
/* Text → paragraphs. Cleaning gives every line a closing punctuation mark so the model pauses
   naturally, and fixes typographic quotes/spaces that sometimes trip the phonemizer. */
function cleanLine(l) {
  l = l.replace(/[“”„]/g, '"').replace(/[‘’]/g, "'").replace(/\s+/g, ' ').replace(/\s+([,.!?;:…])/g, '$1').trim();
  return !l || /[.!?…:;,"')\]]$/.test(l) ? l : l + '.';
}
function splitLong(text, max = 15000) {
  if (text.length <= max) return [text];
  const parts = []; let cur = '';
  const push = () => { if (cur.trim()) parts.push(cur); cur = ''; };
  for (const sent of text.split(/(?<=[.!?…\n])\s+/)) { if (cur && (cur.length + sent.length + 1) > max) push(); cur += (cur ? ' ' : '') + sent; }
  push(); return parts;
}
function paragraphs(text, clean) {
  const blocks = clean ? text.split(/\r?\n/).map(cleanLine).filter(Boolean) : text.split(/\r?\n\s*\r?\n/).map(s => s.trim()).filter(Boolean);
  return blocks.flatMap(b => splitLong(b));
}

/* ---------- shared producer: parallel synthesis, ordered assembly, optional live playback ---------- */
async function produce({box, items, synthOne, piecesFor, label, voice, extra, signal}) {
  const live = $('#opt-live').checked, t0 = performance.now(), parts = [], ready = [];
  let rate = null, player = null, next = 0;
  stopLive();
  const flush = () => {
    while (next < items.length && ready[next] !== undefined) {
      for (const p of piecesFor(next, ready[next], rate)) { parts.push(p); player?.push(p); }
      ready[next++] = null;
    }
  };
  showProgress(box, `0/${items.length} đoạn`, 0);
  try {
    await runPool(items, async (it, i) => {
      const r = await synthOne(it, signal);
      if (rate === null) { rate = r.rate; if (live) CURRENT_LIVE = player = new LivePlayer(rate); }
      ready[i] = fadeEdges(r.pcm, r.rate); flush();
    }, concurrency(), (d, n) => showProgress(box, `${d}/${n} đoạn${player ? ' · đang phát' : ''}`, d / n), signal);
  } catch (e) { player?.stop(); throw e; }
  const pcm = concat(parts);
  if ($('#opt-norm').checked) normalizePeak(pcm);
  await showResult(box, pcm, rate, {voice, label, extra: typeof extra === 'function' ? extra() : extra, secs: (performance.now() - t0) / 1000, live: !!player});
}

/* ---------- result card ---------- */
function showProgress(box, text, frac) {
  box.innerHTML = `<h3>Đang tạo…</h3><div class="progress"><div style="width:${Math.round((frac || 0) * 100)}%"></div></div><p class="muted small">${esc(text)}</p>`;
}
async function showResult(box, pcm, rate, info) {
  if (box._url) URL.revokeObjectURL(box._url);
  const blob = encodeWav(pcm, rate), url = box._url = URL.createObjectURL(blob);
  const base = `vieneu-${info.label}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`;
  box.innerHTML = `<h3>Kết quả</h3><audio controls src="${url}"></audio>
    <div class="meta"><span>⏱ ${fmtDur(pcm.length / rate)}</span><span>🎙 ${esc(info.voice)}</span><span>${rate / 1000} kHz</span><span>⚡ ${info.secs.toFixed(1)} s</span>${info.extra ? `<span>${esc(info.extra)}</span>` : ''}</div>
    <div class="row gap wrap"><a class="btn primary" download="${esc(base)}.wav" href="${url}">⬇️ Tải WAV</a><button class="btn mp3">⬇️ Tải MP3</button><span class="muted small save-note">Đang lưu vào outputs…</span></div>`;
  const audio = $('audio', box);
  audio.addEventListener('play', stopLive);
  if (!info.live) audio.play().catch(() => {});
  $('.mp3', box).onclick = async e => {
    const b = e.currentTarget, old = b.textContent; b.disabled = true;
    try {
      const mp3 = await toMp3(pcm, rate, f => { b.textContent = `⏳ ${Math.round(f * 100)}%`; });
      const a = document.createElement('a'); a.href = URL.createObjectURL(mp3); a.download = base + '.mp3'; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    } catch (err) { toast(err.message, true); }
    finally { b.disabled = false; b.textContent = old; }
  };
  const note = $('.save-note', box);
  try {
    const r = await api('/app/api/outputs?label=' + encodeURIComponent(info.label), {method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: blob});
    note.textContent = 'Đã lưu: outputs\\' + (await r.json()).name;
  } catch (e) { note.textContent = 'Chưa lưu vào outputs: ' + e.message; }
}
function showError(box, e) {
  box.innerHTML = `<h3>Kết quả</h3><div class="empty">${e.name === 'AbortError' ? '⏹ Đã dừng.' : '⚠️ ' + esc(e.message)}</div>`;
  if (e.name !== 'AbortError') toast(e.message, true);
}
function job(goBtn, stopBtn, box, fn) {
  return async () => {
    const ctl = new AbortController(); goBtn.disabled = true; stopBtn.hidden = false;
    stopBtn.onclick = () => { ctl.abort(); stopLive(); };
    try { await fn(ctl.signal); } catch (e) { showError(box, e); }
    finally { goBtn.disabled = false; stopBtn.hidden = true; }
  };
}

/* ---------- voices ---------- */
const parseDesc = v => { const [gender = '', region = '', style = ''] = (v.description || '').split('·').map(s => s.trim()); return {gender, region, style}; };
function voiceLabel(v) {
  if (v.kind === 'cloned') return `${v.name}${v.description ? ' — ' + v.description : ''}`;
  const d = parseDesc(v); return `${v.name} — ${[d.gender, d.region && 'miền ' + d.region, d.style].filter(Boolean).join(', ')}`;
}
function fillVoiceSelect(sel) {
  const want = sel.value || store.get('voice.' + sel.id, 'Mai Anh');
  const presets = VOICES.filter(v => v.kind === 'preset');
  const groups = [['💾 Giọng của bạn', VOICES.filter(v => v.kind === 'cloned')],
    ['⭐ Nổi bật', presets.filter(v => v.featured).sort((a, b) => a.featured - b.featured)],
    ...['Bắc', 'Trung', 'Nam'].map(r => ['Miền ' + r, presets.filter(v => !v.featured && parseDesc(v).region === r)])];
  sel.innerHTML = groups.filter(g => g[1].length).map(([label, list]) =>
    `<optgroup label="${esc(label)}">${list.map(v => `<option value="${esc(v.id)}">${esc(voiceLabel(v))}</option>`).join('')}</optgroup>`).join('');
  sel.value = VOICES.some(v => v.id === want) ? want : (VOICES.find(v => v.id === 'Mai Anh') || VOICES[0] || {}).id;
}
async function loadVoices() {
  try { VOICES = (await (await api('/v1/voices')).json()).data; }
  catch (e) { toast(e.message, true); return; }
  $$('select.voice-select').forEach(fillVoiceSelect);
  $$('.spk select').forEach(fillVoiceSelect);
  renderVoices();
}
const previewCache = new Map(); let previewAudio = null;
async function previewVoice(name, btn) {
  const key = name + '|' + $('#adv-rate').value;
  const old = btn?.textContent; if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    if (!previewCache.has(key)) {
      const {pcm, rate} = await synth(`Xin chào, mình là giọng ${name}. Rất vui được đọc cho bạn nghe.`, name);
      previewCache.set(key, URL.createObjectURL(encodeWav(pcm, rate)));
    }
    previewAudio?.pause(); previewAudio = new Audio(previewCache.get(key)); await previewAudio.play();
  } catch (e) { toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}
function renderVoices() {
  const q = $('#voice-filter').value.trim().toLowerCase();
  const rows = VOICES.filter(v => !q || `${v.name} ${v.description} ${v.kind} ${(v.aliases || []).join(' ')}`.toLowerCase().includes(q));
  $('#voice-rows').innerHTML = rows.map(v => `<tr><td><b>${esc(v.name)}</b>${v.featured ? ' ⭐' : ''}${v.aliases?.length ? `<div class="muted small">tên khác: ${esc(v.aliases.join(', '))}</div>` : ''}</td>
    <td>${esc(v.description || '')}</td><td><span class="badge ${v.kind === 'cloned' ? 'cloned' : ''}">${v.kind === 'cloned' ? 'đã clone' : 'có sẵn'}</span></td>
    <td><button class="btn small" data-preview="${esc(v.id)}">🔈 Nghe thử</button> <button class="btn small" data-use="${esc(v.id)}">Dùng</button>
    ${v.kind === 'cloned' ? ` <button class="btn small danger" data-del="${esc(v.id)}">🗑 Xoá</button>` : ''}</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">Không có giọng nào khớp.</td></tr>';
}
$('#voice-rows').addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.preview) return previewVoice(b.dataset.preview, b);
  if (b.dataset.use) { $('#read-voice').value = b.dataset.use; store.set('voice.read-voice', b.dataset.use); switchTab('read'); return; }
  if (b.dataset.del) {
    if (!confirm(`Xoá giọng "${b.dataset.del}" khỏi kho? (clip gốc trong voices/ sẽ bị xoá)`)) return;
    try { await api('/v1/voices/' + encodeURIComponent(b.dataset.del), {method: 'DELETE'}); toast('Đã xoá giọng ' + b.dataset.del); await loadVoices(); }
    catch (err) { toast(err.message, true); }
  }
});
$('#voice-filter').addEventListener('input', renderVoices);
$('#voice-refresh').addEventListener('click', loadVoices);
document.addEventListener('change', e => { if (e.target.matches('select.voice-select')) store.set('voice.' + e.target.id, e.target.value); });

/* ---------- tab: read ---------- */
const readText = $('#read-text');
const updateCount = () => { $('#read-count').textContent = readText.value.length.toLocaleString('vi-VN') + ' ký tự'; store.set('read.text', readText.value); };
readText.value = store.get('read.text', ''); updateCount();
readText.addEventListener('input', updateCount);
readText.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('#read-go').click(); });
$$('.chip[data-tag]').forEach(c => c.addEventListener('click', () => {
  const s = readText.selectionStart, t = c.dataset.tag + ' ';
  readText.setRangeText(t, s, readText.selectionEnd, 'end'); readText.focus(); updateCount();
}));
for (const [id, def] of [['opt-clean', true], ['opt-live', true], ['opt-norm', true]]) {
  const el = $('#' + id); el.checked = store.get(id, def); el.addEventListener('change', () => store.set(id, el.checked));
}
const gapEl = $('#read-gap'); gapEl.value = store.get('read.gap', 0.5); $('#v-read-gap').textContent = gapEl.value;
gapEl.addEventListener('input', () => { $('#v-read-gap').textContent = gapEl.value; store.set('read.gap', Number(gapEl.value)); });
$('#read-file').addEventListener('change', async e => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  try {
    let text;
    if (/\.pdf$/i.test(f.name)) {
      toast('Đang đọc PDF…');
      if (!window.pdfjsLib) { await loadScript(PDFJS + 'pdf.min.js'); window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + 'pdf.worker.min.js'; }
      const doc = await pdfjsLib.getDocument({data: await f.arrayBuffer()}).promise; const pages = [];
      for (let i = 1; i <= doc.numPages; i++) { const c = await (await doc.getPage(i)).getTextContent(); pages.push(c.items.map(it => it.str + (it.hasEOL ? '\n' : ' ')).join('')); }
      text = pages.join('\n\n').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    } else text = await f.text();
    if (!text.trim()) throw new Error('File không có chữ (PDF scan ảnh thì không đọc được).');
    readText.value = text; updateCount(); toast(`Đã nạp ${f.name}`);
  } catch (err) { toast(err.message, true); }
});
$('#read-preview').addEventListener('click', e => previewVoice($('#read-voice').value, e.currentTarget));
$('#read-go').addEventListener('click', job($('#read-go'), $('#read-stop'), $('#read-result'), async signal => {
  const voice = $('#read-voice').value, gap = Number(gapEl.value);
  const items = paragraphs(readText.value, $('#opt-clean').checked);
  if (!items.length) { toast('Chưa có văn bản.', true); return; }
  await produce({box: $('#read-result'), items, label: 'doc', voice, signal, extra: `${items.length} đoạn`,
    synthOne: (t, sig) => synth(t, voice, sig),
    piecesFor: (i, pcm, rate) => i < items.length - 1 ? [pcm, silence(gap, rate)] : [pcm]});
}));

/* ---------- tab: conversation ---------- */
const convMap = store.get('conv.map', {});
const convText = $('#conv-text');
convText.value = store.get('conv.text', ''); convText.addEventListener('input', () => store.set('conv.text', convText.value));
$('#conv-gap').addEventListener('input', e => { $('#v-conv-gap').textContent = e.target.value; });
function parseScript(text) {
  const lines = []; let last = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim(); if (!line) continue;
    const m = line.match(/^([^:：]{1,30}?)\s*[:：](?!\d)\s*(.+)$/);
    if (m) { last = m[1].trim(); lines.push({speaker: last, text: cleanLine(m[2])}); }
    else lines.push({speaker: last || 'Người đọc', text: cleanLine(line)});
  }
  return lines;
}
function scanSpeakers() {
  const speakers = [...new Set(parseScript(convText.value).map(l => l.speaker))], box = $('#conv-speakers');
  if (!speakers.length) { box.innerHTML = '<p class="muted small">Chưa có lời thoại dạng “Tên: lời”.</p>'; return speakers; }
  const featured = VOICES.filter(v => v.kind === 'preset' && v.featured).sort((a, b) => a.featured - b.featured).map(v => v.id);
  box.innerHTML = speakers.map(s => `<div class="spk"><b>👤 ${esc(s)}</b><select data-speaker="${esc(s)}"></select></div>`).join('');
  $$('select', box).forEach((sel, i) => {
    fillVoiceSelect(sel); const want = convMap[speakers[i]] || featured[i % Math.max(1, featured.length)];
    if (want && VOICES.some(v => v.id === want)) sel.value = want;
    convMap[speakers[i]] = sel.value;
    sel.addEventListener('change', () => { convMap[sel.dataset.speaker] = sel.value; store.set('conv.map', convMap); });
  });
  store.set('conv.map', convMap); return speakers;
}
$('#conv-scan').addEventListener('click', scanSpeakers);
$('#conv-go').addEventListener('click', job($('#conv-go'), $('#conv-stop'), $('#conv-result'), async signal => {
  const lines = parseScript(convText.value);
  if (!lines.length) { toast('Kịch bản trống.', true); return; }
  const shown = $$('#conv-speakers select').map(s => s.dataset.speaker);
  if (lines.some(l => !shown.includes(l.speaker))) scanSpeakers();
  const gap = Number($('#conv-gap').value);
  const who = [...new Set(lines.map(l => `${l.speaker}: ${convMap[l.speaker]}`))].join(' · ');
  await produce({box: $('#conv-result'), items: lines, label: 'hoi-thoai', voice: who, signal, extra: `${lines.length} câu`,
    synthOne: (l, sig) => synth(l.text, convMap[l.speaker] || 'Mai Anh', sig),
    piecesFor: (i, pcm, rate) => i < lines.length - 1 ? [pcm, silence(gap, rate)] : [pcm]});
}));

/* ---------- tab: SRT ---------- */
let srtCues = [], srtName = '';
const toSec = t => { const m = t.trim().replace('.', ',').match(/(?:(\d+):)?(\d+):(\d+),(\d+)/); return m ? (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) + (+m[4].padEnd(3, '0').slice(0, 3)) / 1000 : NaN; };
function parseSrt(text) {
  const cues = [];
  for (const block of text.replace(/^\uFEFF/, '').replace(/\r/g, '').split(/\n\s*\n/)) {
    const lines = block.split('\n'); const ti = lines.findIndex(l => l.includes('-->')); if (ti < 0) continue;
    const [a, b] = lines[ti].split('-->'); const start = toSec(a), end = toSec(b);
    const t = lines.slice(ti + 1).join(' ').replace(/<[^>]+>/g, '').replace(/\{[^}]*\}/g, '').replace(/\s+/g, ' ').trim();
    if (t && !isNaN(start)) cues.push({start, end: isNaN(end) ? start : end, text: t});
  }
  return cues.sort((x, y) => x.start - y.start);
}
async function loadSrt(f) {
  if (!f) return;
  srtCues = parseSrt(await f.text()); srtName = f.name.replace(/\.srt$/i, '');
  $('#srt-info').textContent = srtCues.length ? `${f.name}: ${srtCues.length} câu, dài ${fmtDur(srtCues.at(-1).end)}` : 'Không đọc được câu nào trong file này.';
  $('#srt-go').disabled = !srtCues.length;
}
const drop = $('#srt-drop');
$('#srt-file').addEventListener('change', e => loadSrt(e.target.files[0]));
['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, () => drop.classList.remove('over')));
drop.addEventListener('drop', e => { e.preventDefault(); loadSrt(e.dataTransfer.files[0]); });
$('#srt-go').addEventListener('click', job($('#srt-go'), $('#srt-stop'), $('#srt-result'), async signal => {
  const voice = $('#srt-voice').value, keep = $('#srt-timing').checked, cues = srtCues;
  let cursor = 0, late = 0;
  await produce({box: $('#srt-result'), items: cues, label: 'srt-' + (srtName || 'phu-de'), voice, signal,
    extra: () => keep ? (late ? `${late} câu đọc dài hơn khung, đã lùi` : 'khớp mốc thời gian') : 'nối liền',
    synthOne: (c, sig) => synth(cleanLine(c.text), voice, sig),
    piecesFor: (i, pcm, rate) => {
      const out = [];
      if (keep) {
        const at = Math.round(cues[i].start * rate);
        if (at > cursor) { out.push(new Int16Array(at - cursor)); cursor = at; }
        if (pcm.length / rate > (cues[i].end - cues[i].start) + 0.05) late++;
      } else if (i) { const g = silence(0.15, rate); out.push(g); cursor += g.length; }
      out.push(pcm); cursor += pcm.length; return out;
    }});
}));

/* ---------- tab: clone ---------- */
let cloneWav = null, cloneIsSample = false, recorder = null, recTimer = null, tryAudio = null;
async function prepareClip(blob, isSample = false) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try { decoded = await ctx.decodeAudioData(await blob.arrayBuffer()); }
  catch { throw new Error('Trình duyệt không giải mã được file này. Thử WAV hoặc MP3.'); }
  finally { ctx.close(); }
  const RATE = 48000, secs = Math.min(decoded.duration, 30);
  const off = new OfflineAudioContext(1, Math.ceil(secs * RATE), RATE), src = off.createBufferSource();
  src.buffer = decoded; src.connect(off.destination); src.start(0);
  const f32 = (await off.startRendering()).getChannelData(0), pcm = new Int16Array(f32.length);
  let peak = 0; for (let i = 0; i < f32.length; i++) { const s = Math.max(-1, Math.min(1, f32[i])); pcm[i] = s * 0x7fff; peak = Math.max(peak, Math.abs(s)); }
  cloneWav = encodeWav(pcm, RATE); cloneIsSample = isSample;
  const warn = secs < 3 ? ' ⚠️ Quá ngắn: giọng clone sẽ kém, nên 6–8 giây.' : secs < 5 ? ' Hơi ngắn, 6–8 giây sẽ giống hơn.' : decoded.duration > 8 ? ' Dài hơn 8 giây: server tự cắt còn 8 giây.' : ' ✅ Độ dài tốt.';
  const quiet = peak < 0.05 ? ' ⚠️ Rất nhỏ tiếng, kiểm tra mic.' : peak > 0.99 ? ' ⚠️ Bị rè (quá to), lùi xa mic một chút.' : '';
  const prev = $('#clone-preview'); if (prev._url) URL.revokeObjectURL(prev._url);
  prev._url = URL.createObjectURL(cloneWav);
  prev.innerHTML = `<audio controls src="${prev._url}"></audio><div>${isSample ? '🎧 Clip mẫu của repo (chỉ để nghe thử). ' : ''}Giọng mẫu ${decoded.duration.toFixed(1)} giây.${warn}${quiet}</div>`;
  $('#clone-try').disabled = false; $('#clone-try-note').textContent = '';
  updateCloneBtn();
}
function updateCloneBtn() {
  const missing = [!cloneWav && 'ghi âm hoặc chọn file giọng mẫu', cloneIsSample && 'clip mẫu chỉ để nghe thử, hãy ghi âm/chọn giọng của bạn để lưu',
    !$('#clone-name').value.trim() && 'gõ tên giọng', !$('#clone-consent').checked && 'tick ô xác nhận đồng ý',
    !$('#clone-note').value.trim() && 'ghi chú đồng ý'].filter(Boolean);
  $('#clone-go').disabled = missing.length > 0;
  $('#clone-missing').textContent = missing.length ? 'Còn thiếu: ' + missing.join(', ') + '.' : '';
}
['input', 'change'].forEach(ev => ['#clone-name', '#clone-consent', '#clone-note'].forEach(s => $(s).addEventListener(ev, updateCloneBtn)));
updateCloneBtn();
$('#clone-file').addEventListener('change', async e => { const f = e.target.files[0]; e.target.value = ''; if (f) try { await prepareClip(f); } catch (err) { toast(err.message, true); } });
async function loadSamples() {
  if (loadSamples.done) return; loadSamples.done = true;
  try {
    const items = (await (await api('/app/api/samples')).json()).data;
    $('#clone-samples').innerHTML = items.length ? '<span class="muted small">Thử nhanh với clip mẫu:</span>' +
      items.map(s => `<button class="chip sample" data-sample="${esc(s.name)}" title="${esc(s.text)}">🎧 ${esc(s.name.replace(/\.wav$/, ''))}</button>`).join('') : '';
  } catch { loadSamples.done = false; }
}
$('#clone-samples').addEventListener('click', async e => {
  const b = e.target.closest('[data-sample]'); if (!b) return;
  try { await prepareClip(await (await api('/app/api/samples/' + encodeURIComponent(b.dataset.sample))).blob(), true); }
  catch (err) { toast(err.message, true); }
});
$('#rec-btn').addEventListener('click', async () => {
  const btn = $('#rec-btn');
  if (recorder) { recorder.stop(); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: false, autoGainControl: true}}); }
  catch { toast('Không mở được micro. Cho phép trình duyệt dùng mic rồi thử lại.', true); return; }
  const chunks = []; recorder = new MediaRecorder(stream); const t0 = performance.now();
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = async () => {
    clearInterval(recTimer); stream.getTracks().forEach(t => t.stop()); recorder = null;
    btn.textContent = '🎙️ Ghi âm lại'; btn.classList.remove('rec-on');
    try { await prepareClip(new Blob(chunks, {type: chunks[0]?.type || 'audio/webm'})); } catch (err) { toast(err.message, true); }
  };
  recorder.start(); btn.textContent = '⏹ Dừng ghi'; btn.classList.add('rec-on');
  recTimer = setInterval(() => { const s = (performance.now() - t0) / 1000; $('#rec-time').textContent = s.toFixed(1) + ' s'; if (s >= 15 && recorder?.state === 'recording') recorder.stop(); }, 100);
});
$('#clone-try').addEventListener('click', async e => {
  const b = e.currentTarget, text = $('#clone-test').value.trim(), note = $('#clone-try-note');
  if (!cloneWav || !text) return;
  const fd = new FormData();
  fd.append('file', cloneWav, 'reference.wav'); fd.append('text', text); fd.append('denoise', $('#clone-denoise').checked ? 'true' : 'false');
  b.disabled = true; note.textContent = '⏳ Đang nhân bản thử…';
  try {
    const r = await api('/app/api/clone-preview', {method: 'POST', body: fd});
    const rate = Number(r.headers.get('x-sample-rate')) || 48000, pcm = normalizePeak(pcmFrom(await r.arrayBuffer()));
    tryAudio?.pause(); tryAudio = new Audio(URL.createObjectURL(encodeWav(pcm, rate))); await tryAudio.play();
    note.textContent = 'Chưa lưu. Ưng thì đặt tên và bấm Lưu; chưa ưng thì ghi lại hoặc thử tắt/bật lọc tạp âm.';
  } catch (err) { note.textContent = ''; toast(err.message, true); }
  finally { b.disabled = false; }
});
$('#clone-go').addEventListener('click', async () => {
  const name = $('#clone-name').value.trim(), btn = $('#clone-go');
  if (!/^[\p{L}\p{N}][\p{L}\p{M}\p{N}_ .\-]{0,63}$/u.test(name)) { toast('Tên chỉ gồm chữ, số, khoảng trắng, dấu chấm, gạch ngang; tối đa 64 ký tự.', true); return; }
  const fd = new FormData();
  fd.append('name', name); fd.append('file', cloneWav, 'reference.wav'); fd.append('denoise', $('#clone-denoise').checked ? 'true' : 'false');
  fd.append('description', $('#clone-desc').value.trim()); fd.append('consent_confirmed', 'true'); fd.append('consent_note', $('#clone-note').value.trim());
  btn.disabled = true; btn.textContent = '⏳ Đang lưu…';
  try {
    await api('/v1/voices', {method: 'POST', body: fd});
    toast(`Đã lưu giọng "${name}". Claude cũng dùng được giọng này.`);
    await loadVoices(); $('#read-voice').value = name; store.set('voice.read-voice', name);
    $('#clone-out').innerHTML = `<div class="row gap wrap"><b>✅ ${esc(name)}</b><button class="btn" id="clone-hear">🔈 Nghe giọng đã lưu</button><button class="btn primary" id="clone-use">Dùng để đọc văn bản</button></div>`;
    $('#clone-hear').onclick = e => previewVoice(name, e.currentTarget);
    $('#clone-use').onclick = () => switchTab('read');
  } catch (e) { toast(e.message, true); }
  finally { btn.textContent = '🧬 Lưu giọng vào kho'; updateCloneBtn(); }
});

/* ---------- tab: history ---------- */
const histUrls = new Map();
async function histBlob(name) {
  if (!histUrls.has(name)) histUrls.set(name, URL.createObjectURL(await (await api('/app/api/outputs/' + encodeURIComponent(name))).blob()));
  return histUrls.get(name);
}
async function loadHistory() {
  try {
    const items = (await (await api('/app/api/outputs')).json()).data;
    $('#hist-rows').innerHTML = items.map(it => `<tr><td><code>${esc(it.name)}</code></td><td>${it.duration_seconds != null ? fmtDur(it.duration_seconds) : '—'}</td>
      <td>${it.source === 'web' ? 'Web' : 'Claude / speak'}</td><td>${esc(new Date(it.modified).toLocaleString('vi-VN'))}</td>
      <td><button class="btn small" data-play="${esc(it.name)}">▶️ Nghe</button> <button class="btn small" data-dl="${esc(it.name)}">⬇️ Tải</button></td></tr>`).join('')
      || '<tr><td colspan="5" class="muted">Chưa có file nào.</td></tr>';
  } catch (e) { toast(e.message, true); }
}
$('#hist-rows').addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.play) { $('#hist-player').innerHTML = `<p class="small"><b>${esc(b.dataset.play)}</b></p><audio controls autoplay src="${await histBlob(b.dataset.play)}"></audio>`; }
    if (b.dataset.dl) { const a = document.createElement('a'); a.href = await histBlob(b.dataset.dl); a.download = b.dataset.dl; a.click(); }
  } catch (err) { toast(err.message, true); }
});
$('#hist-refresh').addEventListener('click', loadHistory);

/* ---------- advanced settings ---------- */
const advSaved = store.get('adv', {});
for (const k of Object.keys(ADV_DEFAULTS)) {
  const el = $('#adv-' + k); if (advSaved[k] != null) el.value = advSaved[k];
  const show = () => { $('#v-' + k).textContent = el.value; };
  show(); el.addEventListener('input', () => { show(); advSaved[k] = Number(el.value); store.set('adv', advSaved); });
}
$('#adv-rate').value = store.get('rate', '48000');
$('#adv-rate').addEventListener('change', e => store.set('rate', e.target.value));
$('#adv-reset').addEventListener('click', () => {
  for (const [k, v] of Object.entries(ADV_DEFAULTS)) { $('#adv-' + k).value = v; $('#v-' + k).textContent = v; delete advSaved[k]; }
  $('#adv-rate').value = '48000'; store.set('adv', {}); store.set('rate', '48000'); toast('Đã khôi phục mặc định.');
});

/* ---------- tab: model ---------- */
let modelTimer = null, modelRendered = false;
function renderEngine(j) {
  const cur = j.current;
  if (!modelRendered) {
    $('#model-options').innerHTML = j.options.map(o => `<label class="mopt"><input type="radio" name="model" value="${esc(o.id)}">
      <span><b>${esc(o.label)}</b><span class="muted small">${esc(o.note)}</span></span></label>`).join('');
    $$('input[name=model]').forEach(r => r.addEventListener('change', () => { $('#model-lora-box').hidden = r.value !== 'lora' || !r.checked; }));
    const sel = $(`input[name=model][value="${cur.preset}"]`); if (sel) sel.checked = true;
    $('#model-lora').value = cur.lora_path || ''; $('#model-lora-box').hidden = cur.preset !== 'lora';
    modelRendered = true;
  }
  const st = {ready: '🟢 Đang chạy', loading: '🟡 Đang tải…', error: '🔴 Lỗi'}[cur.state] || cur.state;
  $('#model-status').innerHTML = `<p><b>${esc(cur.label)}</b> — ${st}</p>${cur.message ? `<p>${esc(cur.message)}</p>` : ''}` +
    (cur.warnings || []).map(w => `<p class="small">⚠️ ${esc(w)}</p>`).join('') + (cur.lora_path ? `<p class="small muted">${esc(cur.lora_path)}</p>` : '');
  $('#model-log').hidden = !j.log; $('#model-log').textContent = j.log || '';
  $('#model-go').disabled = cur.state === 'loading';
}
async function loadEngine() {
  clearTimeout(modelTimer);
  let j;
  try { j = await (await api('/app/api/engine')).json(); } catch (e) { $('#model-status').textContent = e.message; return; }
  renderEngine(j);
  if (j.current.state === 'loading') { ENGINE_LOADING = true; modelTimer = setTimeout(loadEngine, 2000); return; }
  if (ENGINE_LOADING) {
    ENGINE_LOADING = false; previewCache.clear(); pollHealth();
    if (j.current.state === 'ready') { toast('Đã tải xong ' + j.current.label); loadVoices(); } else toast(j.current.message, true);
  }
}
$('#model-go').addEventListener('click', async () => {
  const preset = $('input[name=model]:checked')?.value; if (!preset) return;
  const body = {preset, lora_path: $('#model-lora').value.trim()};
  if (!confirm('Tải model "' + $('input[name=model]:checked').closest('label').querySelector('b').textContent + '"? Model hiện tại sẽ tắt trong lúc tải.')) return;
  try { await api('/app/api/engine', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); ENGINE_LOADING = true; loadEngine(); }
  catch (e) { toast(e.message, true); }
});

/* ---------- tabs, status, boot ---------- */
function switchTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  history.replaceState(null, '', '#' + name);
  if (name === 'history') loadHistory();
  if (name === 'clone') loadSamples();
  if (name === 'model') loadEngine();
  if (name === 'conv' && !$('#conv-speakers select') && convText.value.trim()) scanSpeakers();
}
$$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
async function pollHealth() {
  const el = $('#status');
  try {
    const r = await fetch('/health', {cache: 'no-store'}), j = await r.json();
    if (!r.ok || j.status !== 'ok') throw new Error();
    MAX_STREAMS = Number(j.max_streams) || MAX_STREAMS;
    el.className = 'status ok'; $('.txt', el).textContent = 'Sẵn sàng · ' + (j.engine?.label || (j.backend === 'pytorch' ? 'GPU' : 'CPU'));
  } catch {
    el.className = 'status bad'; $('.txt', el).textContent = ENGINE_LOADING ? 'Đang đổi model…' : 'Mất kết nối — chạy start-all.bat';
  }
}
pollHealth(); setInterval(pollHealth, 5000);
loadEngine();
loadVoices().then(() => { const h = location.hash.slice(1); if (h && $('#tab-' + h)) switchTab(h); });
