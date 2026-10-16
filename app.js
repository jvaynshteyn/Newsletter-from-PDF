'use strict';

const $ = (id) => document.getElementById(id);
const state = {document: null, pdf: null, filename: '', originalHtml: '', originalSubject: '', dirty: false, currentPage: 1, originalUrl: null, savedRange: null, activeLink: null, busy: false, imageSetting: true, layout: 'faithful', selectedImage: null, selectedUnit: null, draggedUnit: null, history: [], historyIndex: -1};
const safeUrl = (value) => {
  try { const url = new URL(String(value).trim()); return ['https:', 'http:', 'mailto:', 'tel:'].includes(url.protocol) ? url.href : null; } catch { return null; }
};
const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const formatBytes = (bytes) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(2)} MB`;
const editor = $('email-editor');
let noticeTimer;

function notify(message, type = '', persistent = false) {
  clearTimeout(noticeTimer);
  $('notice').textContent = message;
  $('notice').className = `notice ${type}`;
  $('notice').hidden = false;
  if (!persistent) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 11000);
}

function setBusy(busy, message) {
  state.busy = busy;
  $('busy-overlay').hidden = !busy;
  if (message) $('busy-message').textContent = message;
  $('include-images').disabled = busy || $('conversion-layout').value === 'faithful';
  $('conversion-layout').disabled = busy;
  $('add-picture').disabled = busy || !state.document;
  $('sample-button').disabled = busy;
  $('copy-button').disabled = busy || !state.document;
  $('download-button').disabled = busy || !state.document;
}

// A pasted email must never import executable content, local files, or tracking images.
function cleanHtml(input, {internal = false} = {}) {
  const parsed = new DOMParser().parseFromString(input, 'text/html');
  const allowed = new Set(['A','P','DIV','SPAN','BR','H1','H2','H3','H4','H5','H6','B','STRONG','I','EM','U','S','UL','OL','LI','BLOCKQUOTE','HR','TABLE','TBODY','THEAD','TFOOT','TR','TD','TH','IMG','SUP','SUB']);
  const blocked = new Set(['SCRIPT','STYLE','IFRAME','OBJECT','EMBED','SVG','MATH','FORM','INPUT','BUTTON','TEXTAREA','SELECT','LINK','META','BASE']);
  const styles = new Set(['color','background-color','font-family','font-size','font-weight','font-style','text-align','text-decoration','line-height','margin','margin-top','margin-right','margin-bottom','margin-left','padding','padding-top','padding-right','padding-bottom','padding-left','border','border-top','border-bottom','border-left','border-right','border-collapse','border-spacing','border-radius','width','min-width','max-width','height','min-height','max-height','vertical-align','display','list-style-type','letter-spacing','overflow-wrap','table-layout','mso-table-lspace','mso-table-rspace']);
  for (const el of [...parsed.body.querySelectorAll('*')]) {
    if (blocked.has(el.tagName)) { el.remove(); continue; }
    if (!allowed.has(el.tagName)) { el.replaceWith(...el.childNodes); continue; }
    const originalStyle = el.style;
    const safeStyle = [];
    // Outlook's Word renderer needs these zero table gutters. Chromium does
    // not expose MSO properties through CSSStyleDeclaration, so retain the
    // explicitly safe zero-valued declarations from the original attribute.
    const rawStyle = el.getAttribute('style') || '';
    for (const property of ['mso-table-lspace', 'mso-table-rspace']) {
      if (new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*0(?:pt|px)?\\s*(?:;|$)`, 'i').test(rawStyle)) safeStyle.push(`${property}:0pt`);
    }
    for (const property of [...originalStyle]) {
      const value = originalStyle.getPropertyValue(property);
      if (styles.has(property) && !/url\s*\(|expression\s*\(|javascript|@import|behavior|position|[<>]/i.test(value)) safeStyle.push(`${property}:${value}`);
    }
    const href = el.tagName === 'A' ? safeUrl(el.getAttribute('href')) : null;
    const src = el.tagName === 'IMG' ? el.getAttribute('src') || '' : '';
    const alt = el.getAttribute('alt') || '';
    const imageWidth = el.tagName === 'IMG' ? el.getAttribute('width') : null;
    const colspan = el.getAttribute('colspan');
    const rowspan = el.getAttribute('rowspan');
    const attributes = Object.fromEntries(['width','height','cellpadding','cellspacing','border','align','valign','role','bgcolor'].map((name) => [name, el.getAttribute(name)]));
    const pdfPage = el.tagName === 'TABLE' && el.getAttribute('data-pdf-page');
    for (const attr of [...el.attributes]) el.removeAttribute(attr.name);
    if (safeStyle.length) el.setAttribute('style', safeStyle.join(';'));
    if (el.tagName === 'A') {
      if (href) { el.setAttribute('href', href); el.setAttribute('target', '_blank'); el.setAttribute('rel', 'noopener noreferrer'); }
      else { el.replaceWith(...el.childNodes); continue; }
    }
    if (el.tagName === 'IMG') {
      if (!/^data:image\/(jpeg|png|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src)) { el.replaceWith(parsed.createTextNode(alt ? `[${alt}]` : '')); continue; }
      el.setAttribute('src', src); el.setAttribute('alt', alt); el.style.maxWidth = '100%'; el.style.height = 'auto';
      if (/^(?:\d{1,4}|(?:100|[1-9]?\d)%)$/.test(imageWidth || '')) el.setAttribute('width', imageWidth);
    }
    if (/^(TD|TH)$/.test(el.tagName)) {
      if (/^[1-9]\d?$/.test(colspan || '')) el.setAttribute('colspan', colspan);
      if (/^[1-9]\d?$/.test(rowspan || '')) el.setAttribute('rowspan', rowspan);
    }
    if (/^(TABLE|TD|TH|TR|IMG)$/.test(el.tagName)) {
      for (const name of ['width','height']) if (/^(?:\d{1,4}|(?:100|[1-9]?\d)(?:\.\d+)?%)$/.test(attributes[name] || '')) el.setAttribute(name, attributes[name]);
      for (const name of ['cellpadding','cellspacing','border']) if (/^\d{1,2}$/.test(attributes[name] || '')) el.setAttribute(name, attributes[name]);
      if (/^(left|center|right)$/i.test(attributes.align || '')) el.setAttribute('align', attributes.align);
      if (/^(top|middle|bottom|baseline)$/i.test(attributes.valign || '')) el.setAttribute('valign', attributes.valign);
      if (attributes.role === 'presentation') el.setAttribute('role', 'presentation');
      if (/^#[0-9a-f]{3,8}$/i.test(attributes.bgcolor || '')) el.setAttribute('bgcolor', attributes.bgcolor);
    }
    if (internal && pdfPage && /^\d+$/.test(pdfPage)) el.setAttribute('data-pdf-page', pdfPage);
  }
  return parsed.body.innerHTML;
}

function emailBody() {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = cleanHtml(editor.innerHTML);
  for (const el of wrapper.querySelectorAll('p')) { if (!el.closest('table') && !el.style.margin) el.style.margin = '0 0 14px'; }
  for (const el of wrapper.querySelectorAll('h1,h2,h3,h4')) {
    if (el.closest('table')) continue;
    el.style.fontFamily = 'Arial, sans-serif';
    if (!el.style.color) el.style.color = '#19574a';
    if (!el.style.lineHeight) el.style.lineHeight = '1.3';
  }
  for (const el of wrapper.querySelectorAll('a')) { if (!el.querySelector('img')) { el.style.color = '#19574a'; el.style.textDecoration = 'underline'; } }
  return `<div style="max-width:640px;margin:0 auto;font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#303b34;overflow-wrap:anywhere">${wrapper.innerHTML}</div>`;
}

function fullEmail() {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml($('email-subject').value || 'Newsletter')}</title></head><body style="margin:0;padding:${state.layout === 'faithful' ? '0' : '24px 18px'};background:#fff">${emailBody()}</body></html>`;
}

function plainText() {
  const holder = document.createElement('div');
  holder.innerHTML = cleanHtml(editor.innerHTML, {internal: true});
  for (const page of holder.querySelectorAll('table[data-pdf-page]')) {
    const text = document.createElement('div'); text.textContent = page.querySelector('img[alt]')?.alt || `Newsletter page ${page.dataset.pdfPage}`;
    const seen = new Set();
    for (const link of page.querySelectorAll('a[href]')) {
      if (seen.has(link.href)) continue; seen.add(link.href);
      const anchor = document.createElement('a'); anchor.href = link.href; anchor.textContent = link.querySelector('img')?.alt || 'Link'; text.append(document.createTextNode('\n'), anchor);
    }
    page.replaceWith(text);
  }
  for (const link of holder.querySelectorAll('a[href]')) {
    const url = link.getAttribute('href');
    const label = link.textContent.trim();
    if (!label.includes(url)) link.append(document.createTextNode(` (${url})`));
  }
  for (const image of holder.querySelectorAll('img')) image.replaceWith(document.createTextNode(`[${image.alt || 'Newsletter image'}]`));
  for (const block of holder.querySelectorAll('p,div,h1,h2,h3,li,tr')) block.append(document.createTextNode('\n'));
  for (const br of holder.querySelectorAll('br')) br.replaceWith(document.createTextNode('\n'));
  return holder.textContent.replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

function currentLinks() {
  const found = new Map();
  for (const anchor of editor.querySelectorAll('a[href]')) {
    const url = safeUrl(anchor.getAttribute('href'));
    if (url && !found.has(url)) found.set(url, {url, label: anchor.textContent.trim() || anchor.querySelector('img')?.alt || 'Linked picture'});
  }
  return [...found.values()];
}

function updateMetrics() {
  if (!state.document) return;
  const bytes = new Blob([emailBody()]).size;
  const links = currentLinks();
  const words = editor.innerText.trim().split(/\s+/).filter(Boolean).length;
  const reduction = state.pdf.size > bytes ? ` · ${Math.round((1 - bytes / state.pdf.size) * 100)}% smaller than PDF` : '';
  $('email-size').textContent = `${formatBytes(bytes)} email${reduction}`;
  $('email-stats').textContent = `${words.toLocaleString()} words · ${links.length} unique links · ${editor.querySelectorAll('img').length} pictures`;
  $('link-count').textContent = links.length;
  $('draft-state').textContent = state.dirty ? 'Edited' : 'Converted';
}

function snapshot() {
  const clone = editor.cloneNode(true);
  for (const node of clone.querySelectorAll('[data-selected]')) node.removeAttribute('data-selected');
  return {html: clone.innerHTML, subject: $('email-subject').value};
}

function recordHistory(reset = false) {
  if (reset) { state.history = []; state.historyIndex = -1; }
  const entry = snapshot();
  const last = state.history[state.historyIndex];
  if (last && last.html === entry.html && last.subject === entry.subject) return;
  state.history.splice(state.historyIndex + 1);
  state.history.push(entry);
  if (state.history.length > 40) state.history.shift();
  state.historyIndex = state.history.length - 1;
}

function undoEdit(direction) {
  const next = state.historyIndex + direction;
  if (next < 0 || next >= state.history.length) return;
  clearPictureSelection();
  state.historyIndex = next;
  const entry = state.history[next];
  editor.innerHTML = entry.html; $('email-subject').value = entry.subject;
  state.savedRange = null; decorateEditor();
  state.dirty = editor.innerHTML !== state.originalHtml || $('email-subject').value !== state.originalSubject;
  updateMetrics(); editor.focus();
}

function markEdited() {
  if (!state.document) return;
  decorateEditor();
  if (state.selectedImage && editor.contains(state.selectedImage)) {
    state.selectedUnit?.removeAttribute('data-selected');
    state.selectedUnit = pictureUnit(state.selectedImage); state.selectedUnit.dataset.selected = 'true';
  }
  recordHistory(); state.dirty = true; updateMetrics();
}

function decorateEditor() {
  for (const table of editor.querySelectorAll('table[data-pdf-page]')) { table.contentEditable = 'false'; table.draggable = true; table.setAttribute('aria-label', `PDF page ${table.dataset.pdfPage}; select to move the whole page`); }
  for (const img of editor.querySelectorAll('img')) { img.draggable = true; if (!img.alt && !img.closest('table[data-pdf-page]')) img.alt = 'Newsletter picture'; }
  editor.classList.toggle('faithful-layout', state.layout === 'faithful');
}

function pictureUnit(image) {
  const page = image.closest('table[data-pdf-page]');
  if (page) return page;
  let unit = image.closest('a') || image;
  const parent = unit.parentElement;
  if (parent && parent !== editor && /^(P|DIV)$/.test(parent.tagName) && parent.querySelectorAll('img').length === 1 && !parent.textContent.trim()) unit = parent;
  return unit;
}

function clearPictureSelection() {
  for (const node of editor.querySelectorAll('[data-selected]')) node.removeAttribute('data-selected');
  state.selectedImage = null; state.selectedUnit = null; $('picture-tools').hidden = true;
}

function selectPicture(image) {
  clearPictureSelection();
  if (!image || !editor.contains(image)) return;
  state.selectedImage = image; state.selectedUnit = pictureUnit(image); state.selectedUnit.dataset.selected = 'true';
  const page = state.selectedUnit.matches('table[data-pdf-page]');
  $('picture-selection-label').textContent = page ? `PDF page ${state.selectedUnit.dataset.pdfPage}` : 'Picture';
  $('picture-image-controls').hidden = page;
  $('picture-help').textContent = page ? 'The complete page moves together to preserve its layout and links. Use Editable text to change its wording.' : 'Drag to reposition, or use Move earlier / Move later. Link wrappers stay with your picture.';
  const width = parseInt(image.style.width, 10);
  $('picture-width').value = [25,50,75,100].includes(width) && image.style.width.endsWith('%') ? width : 100;
  $('picture-tools').hidden = false;
  const range = document.createRange(); range.setStartAfter(state.selectedUnit); range.collapse(true); state.savedRange = range.cloneRange();
  const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
}

function editorRange() {
  const selection = window.getSelection();
  if (selection.rangeCount && editor.contains(selection.anchorNode) && editor.contains(selection.focusNode)) return selection.getRangeAt(0).cloneRange();
  if (state.savedRange && editor.contains(state.savedRange.commonAncestorContainer)) return state.savedRange.cloneRange();
  const range = document.createRange(); range.selectNodeContents(editor); range.collapse(false); return range;
}

function topLevel(node) {
  if (node?.nodeType === Node.TEXT_NODE) node = node.parentElement;
  if (!node || node === editor || !editor.contains(node)) return null;
  while (node.parentElement !== editor) node = node.parentElement;
  return node;
}

function rangeAtPoint(x, y) {
  let range;
  if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(x, y);
  else if (document.caretPositionFromPoint) { const caret = document.caretPositionFromPoint(x, y); if (caret) { range = document.createRange(); range.setStart(caret.offsetNode, caret.offset); range.collapse(true); } }
  if (range && editor.contains(range.commonAncestorContainer)) return range;
  return editorRange();
}

// Never insert a picture into a PDF tile: a page is one protected layout unit.
function outsidePageRange(range, pointY = null) {
  const startNode = range.startContainer.nodeType === Node.TEXT_NODE ? range.startContainer.parentElement : range.startContainer;
  const endNode = range.endContainer.nodeType === Node.TEXT_NODE ? range.endContainer.parentElement : range.endContainer;
  const page = startNode.closest?.('table[data-pdf-page]');
  const endPage = endNode.closest?.('table[data-pdf-page]');
  if (!range.collapsed) {
    if (page) range.setStartBefore(page);
    if (endPage) range.setEndAfter(endPage);
  } else if (page) {
    const rect = page.getBoundingClientRect();
    if (pointY !== null && pointY < rect.top + rect.height / 2) range.setStartBefore(page);
    else range.setStartAfter(page);
    range.collapse(true);
  }
  return range;
}

function insertNodes(nodes, range = editorRange(), pointY = null) {
  range = outsidePageRange(range, pointY);
  if (!editor.contains(range.commonAncestorContainer)) { range = document.createRange(); range.selectNodeContents(editor); range.collapse(false); }
  // A range spanning content is a normal paste replacement; page boundaries remain intact.
  range.deleteContents();
  const top = topLevel(range.startContainer);
  // Split a paragraph at the caret so a picture is never nested inside <p>.
  if (top && /^(P|H[1-6]|BLOCKQUOTE)$/.test(top.tagName) && top.contains(range.startContainer) && nodes.some((node) => /^(P|DIV|TABLE)$/.test(node.nodeName))) {
    const tailRange = document.createRange(); tailRange.selectNodeContents(top); tailRange.setStart(range.startContainer, range.startOffset);
    const tail = top.cloneNode(false); tail.append(tailRange.extractContents());
    range.setStartAfter(top); range.collapse(true);
    if (tail.textContent || tail.querySelector('img,br')) top.after(tail);
  }
  const fragment = document.createDocumentFragment(); nodes.forEach((node) => fragment.append(node));
  const last = nodes[nodes.length - 1]; range.insertNode(fragment);
  if (last) { range.setStartAfter(last); range.collapse(true); state.savedRange = range.cloneRange(); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); }
  markEdited();
}

async function readPicture(file) {
  if (!/^image\/(jpeg|png|gif|webp)$/i.test(file.type)) throw new Error('Use a JPG, PNG, GIF, or WebP picture. SVG and other file types are not supported.');
  if (file.size > 20 * 1024 * 1024) throw new Error('Choose pictures smaller than 20 MB each.');
  const source = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('The picture could not be read.')); reader.readAsDataURL(file); });
  const decoded = await new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve(image); image.onerror = () => reject(new Error('This picture could not be opened. Please try a JPG or PNG.')); image.src = source; });
  const scale = Math.min(1, 1200 / Math.max(decoded.naturalWidth, decoded.naturalHeight));
  if (!decoded.naturalWidth || !decoded.naturalHeight) throw new Error('The picture has no usable dimensions.');
  let dataUrl = source;
  if (file.type !== 'image/gif' || scale < 1 || file.size > 500 * 1024) {
    const canvas = document.createElement('canvas'); canvas.width = Math.max(1, Math.round(decoded.naturalWidth * scale)); canvas.height = Math.max(1, Math.round(decoded.naturalHeight * scale));
    const context = canvas.getContext('2d'); if (!context) throw new Error('This browser cannot process pictures.');
    context.drawImage(decoded, 0, 0, canvas.width, canvas.height);
    dataUrl = canvas.toDataURL(file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png', .85);
    if (dataUrl.length > 8 * 1024 * 1024) { context.globalCompositeOperation = 'destination-over'; context.fillStyle = '#fff'; context.fillRect(0,0,canvas.width,canvas.height); dataUrl = canvas.toDataURL('image/jpeg', .85); }
  }
  const image = document.createElement('img'); image.src = dataUrl; image.alt = (file.name || 'Pasted picture').replace(/\.[^.]+$/, ''); image.width = Math.min(640, Math.round(decoded.naturalWidth * scale)); image.style.cssText = 'display:block;max-width:100%;width:100%;height:auto;border:0;margin:0 auto;';
  const block = document.createElement('p'); block.style.cssText = 'margin:14px 0 22px;text-align:center;'; block.append(image); return block;
}

async function insertPictureFiles(files, range = editorRange(), pointY = null) {
  const documentAtStart = state.document; const nodes = []; const errors = [];
  for (const file of files) { try { nodes.push(await readPicture(file)); } catch (error) { errors.push(error.message); } }
  if (state.document !== documentAtStart) { notify('The newsletter changed while the picture was loading. Add the picture to the current email again.', 'warning'); return; }
  if (nodes.length) { editor.focus(); insertNodes(nodes, range, pointY); selectPicture(nodes[nodes.length - 1].querySelector('img')); }
  if (errors.length) notify([...new Set(errors)].join(' '), 'warning', true);
  else if (nodes.length) notify(`${nodes.length === 1 ? 'Picture added' : 'Pictures added'}. Select a picture to move, align, or resize it. Ctrl+Z undoes the change.`);
}

function moveSelectedPicture(direction) {
  const unit = state.selectedUnit; const image = state.selectedImage;
  if (!unit || !editor.contains(unit)) return;
  const top = topLevel(unit); const neighbor = direction < 0 ? top.previousElementSibling : top.nextElementSibling;
  if (!neighbor) return;
  if (direction < 0) editor.insertBefore(unit, neighbor);
  else editor.insertBefore(unit, neighbor.nextSibling);
  // Images that began inline receive an email-safe block after moving.
  if (/^(IMG|A)$/.test(unit.tagName)) { const block = document.createElement('p'); block.style.cssText = 'margin:14px 0 22px;'; unit.replaceWith(block); block.append(unit); }
  if (top !== unit && !top.textContent.trim() && !top.querySelector('img')) top.remove();
  markEdited(); selectPicture(image); state.selectedUnit.scrollIntoView({block: 'nearest'});
}

function renderLinkList() {
  const list = $('links-list');
  list.replaceChildren();
  const links = currentLinks();
  if (!links.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No links in this email yet. Select text in the editor and use Link to add one.'; list.append(p); return; }
  links.forEach((link, i) => {
    const card = document.createElement('div'); card.className = 'link-card';
    const number = document.createElement('span'); number.className = 'link-card-number'; number.textContent = String(i + 1).padStart(2, '0');
    const details = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = link.label; title.title = link.label;
    const anchor = document.createElement('a'); anchor.href = link.url; anchor.textContent = link.url; anchor.title = link.url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
    details.append(title, anchor);
    const open = document.createElement('a'); open.href = link.url; open.target = '_blank'; open.rel = 'noopener noreferrer'; open.textContent = '↗'; open.setAttribute('aria-label', `Open ${link.label}`);
    card.append(number, details, open); list.append(card);
  });
}

function switchView(view) {
  for (const tab of document.querySelectorAll('[data-view]')) {
    const selected = tab.dataset.view === view;
    tab.classList.toggle('active', selected); tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    $(`${tab.dataset.view}-view`).hidden = !selected;
  }
  if (view === 'preview') $('email-preview').innerHTML = state.document ? emailBody() : '<p style="font:14px Arial;color:#899681;padding:35px;text-align:center">Add a PDF to preview your email.</p>';
  $('email-preview').classList.toggle('faithful-layout', state.layout === 'faithful');
  if (view === 'links') renderLinkList();
}

async function confirmChange(title, message, button = 'Continue') {
  $('confirm-title').textContent = title; $('confirm-message').textContent = message; $('confirm-yes').textContent = button;
  const dialog = $('confirm-dialog'); dialog.returnValue = ''; dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'yes'), {once: true}));
}

function updateLayoutControls() {
  const faithful = $('conversion-layout').value === 'faithful';
  if (faithful) $('include-images').checked = true;
  $('include-images').disabled = faithful || state.busy;
  $('layout-help').textContent = faithful ? 'Preserves the complete design and clickable links. Text inside each PDF page is part of its picture; add text or pictures around it.' : 'Edit wording, pictures, and links freely. PDF fonts and placement may change.';
  $('editable-badge').textContent = faithful ? 'PDF LAYOUT' : 'EDITABLE TEXT';
}

async function convertFile(file, {replace = true} = {}) {
  if (state.busy) return false;
  if (!file || file.size > 30 * 1024 * 1024) { notify('Choose a PDF file smaller than 30 MB.', 'error', true); return false; }
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') { notify('Please choose a PDF newsletter.', 'error', true); return false; }
  if (replace && state.dirty && !await confirmChange('Replace this newsletter?', 'Your current email edits will be replaced by the new PDF conversion.', 'Replace newsletter')) return false;
  setBusy(true, 'Reading your newsletter, arranging its content, and preserving its links…');
  try {
    const header = await file.slice(0, 1024).text();
    if (!header.includes('%PDF-')) throw new Error('This file does not look like a valid PDF. Please choose the original PDF file.');
    const response = await fetch(`/api/convert?layout=${encodeURIComponent($('conversion-layout').value)}&images=${$('include-images').checked ? '1' : '0'}`, {method: 'POST', headers: {'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name)}, body: file});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The PDF could not be converted.');
    if (!data.html || !data.pages?.length) throw new Error('The PDF did not contain any usable pages.');
    state.document = data; state.pdf = file; state.filename = file.name; state.layout = data.layout || $('conversion-layout').value; state.originalHtml = cleanHtml(data.html, {internal: true}); state.dirty = false; state.currentPage = 1; state.imageSetting = $('include-images').checked;
    const converted = document.createElement('div'); converted.innerHTML = state.originalHtml;
    if (converted.children.length === 1 && converted.firstElementChild.tagName === 'DIV') state.originalHtml = converted.firstElementChild.innerHTML;
    if (replace) $('email-subject').value = file.name.replace(/\.pdf$/i, '').replace(/_/g, ' ').trim();
    state.originalSubject = $('email-subject').value;
    clearPictureSelection(); state.savedRange = null; editor.innerHTML = state.originalHtml; editor.contentEditable = 'true'; decorateEditor(); state.originalHtml = editor.innerHTML; recordHistory(true); updateLayoutControls();
    document.body.classList.add('has-document'); document.querySelector('.source-panel').classList.add('loaded');
    $('source-name').textContent = file.name; $('source-name').title = file.name;
    $('source-detail').textContent = `${data.pageCount} ${data.pageCount === 1 ? 'page' : 'pages'} · ${formatBytes(file.size)} · Original preserved`;
    for (const id of ['source-toolbar','replace-button','pdf-pages','view-original']) $(id).hidden = false;
    for (const id of ['drop-zone','sample-callout']) $(id).hidden = true;
    for (const id of ['email-subject','reset-button']) $(id).disabled = false;
    for (const control of document.querySelectorAll('.editor-toolbar button, .editor-toolbar select')) control.disabled = false;
    $('add-picture').disabled = false;
    if (state.originalUrl) URL.revokeObjectURL(state.originalUrl);
    state.originalUrl = URL.createObjectURL(file); $('view-original').href = state.originalUrl;
    renderPdf(data); updateMetrics(); switchView('edit');
    const warning = (data.warnings || []).join(' ');
    if (warning) notify(warning, 'warning', true);
    else notify('Your email is ready to edit. Review its layout, then copy it into a new message.');
    return true;
  } catch (error) { notify(error.message || 'Something went wrong while converting the PDF.', 'error', true); return false; }
  finally { setBusy(false); }
}

function renderPdf(data) {
  const pages = $('pdf-pages'); pages.replaceChildren();
  data.pages.forEach((page, index) => {
    const card = document.createElement('div'); card.className = 'pdf-page'; card.dataset.page = index + 1;
    card.style.aspectRatio = `${page.width} / ${page.height}`;
    const image = document.createElement('img'); image.src = `/api/documents/${encodeURIComponent(data.id)}/pages/${index + 1}.jpg`; image.alt = `Original PDF page ${index + 1}`; image.loading = index === 0 ? 'eager' : 'lazy'; image.width = Math.round(page.width); image.height = Math.round(page.height);
    image.addEventListener('error', () => { image.alt = 'Page preview unavailable. Use Open original below to see the PDF.'; });
    const layer = document.createElement('div'); layer.className = 'pdf-page-links';
    for (const link of page.links || []) {
      const href = safeUrl(link.url); if (!href) continue;
      const anchor = document.createElement('a'); anchor.className = 'pdf-link'; anchor.href = href; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.title = link.label || href; anchor.setAttribute('aria-label', link.label || href);
      const rect = link.rect || link;
      anchor.style.left = `${100 * rect.x / page.width}%`; anchor.style.top = `${100 * rect.y / page.height}%`; anchor.style.width = `${100 * rect.width / page.width}%`; anchor.style.height = `${100 * rect.height / page.height}%`; layer.append(anchor);
    }
    const caption = document.createElement('span'); caption.className = 'pdf-page-caption'; caption.textContent = `PAGE ${index + 1}`;
    card.append(image, layer, caption); pages.append(card);
  });
  $('pdf-viewer').scrollTop = 0; updatePageLabel(); applyZoom();
}

function updatePageLabel() {
  $('page-label').textContent = `Page ${state.currentPage} of ${state.document?.pageCount || 1}`;
  $('previous-page').disabled = state.currentPage <= 1;
  $('next-page').disabled = state.currentPage >= (state.document?.pageCount || 1);
}

function goToPage(delta) {
  const next = Math.max(1, Math.min(state.document.pageCount, state.currentPage + delta));
  const page = $ ('pdf-pages').querySelector(`[data-page="${next}"]`);
  if (page) $('pdf-viewer').scrollTo({top: page.offsetTop - $('pdf-pages').offsetTop, behavior: 'smooth'});
  state.currentPage = next; updatePageLabel();
}

function applyZoom() {
  if (!state.document) return;
  const zoom = $('zoom').value;
  for (const card of document.querySelectorAll('.pdf-page')) {
    const page = state.document.pages[Number(card.dataset.page) - 1];
    card.style.width = zoom === 'fit' ? '100%' : `${page.width * Number(zoom) / 100}px`;
    card.style.maxWidth = zoom === 'fit' ? '100%' : 'none';
  }
}

function rememberSelection() {
  const selection = window.getSelection();
  if (selection.rangeCount && editor.contains(selection.anchorNode) && editor.contains(selection.focusNode)) state.savedRange = selection.getRangeAt(0).cloneRange();
}

function restoreSelection() {
  editor.focus();
  if (state.savedRange && editor.contains(state.savedRange.commonAncestorContainer)) { const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(state.savedRange); }
}

function openLinkDialog() {
  if (!state.document) return;
  rememberSelection();
  const selection = window.getSelection();
  let node = selection.anchorNode;
  if (node?.nodeType === Node.TEXT_NODE) node = node.parentElement;
  state.activeLink = state.selectedImage?.closest('a') || node?.closest?.('a');
  if (state.activeLink && !editor.contains(state.activeLink)) state.activeLink = null;
  const imageLink = state.activeLink?.querySelector('img') || state.selectedImage;
  $('link-label').value = state.activeLink?.textContent || imageLink?.alt || (imageLink ? 'Linked picture' : selection.toString()) || '';
  $('link-label').disabled = Boolean(imageLink);
  $('link-url').value = state.activeLink?.getAttribute('href') || '';
  $('link-dialog-title').textContent = state.activeLink ? 'Edit link' : 'Add a link';
  $('remove-link').hidden = !state.activeLink; $('link-error').hidden = true;
  $('link-dialog').showModal();
  (state.activeLink || imageLink || selection.toString() ? $('link-url') : $('link-label')).focus();
}

async function copyEmail() {
  if (!state.document) return;
  const html = emailBody(); const text = plainText();
  let copied = false;
  try {
    if (window.ClipboardItem && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({'text/html': new Blob([html], {type: 'text/html'}), 'text/plain': new Blob([text], {type: 'text/plain'})})]); copied = true;
    }
  } catch { /* Older email/browser combinations use the selection-based path below. */ }
  if (!copied) {
    const temporary = document.createElement('div'); temporary.contentEditable = 'true'; temporary.style.cssText = 'position:fixed;left:-10000px;top:0;width:640px'; temporary.innerHTML = html; document.body.append(temporary);
    const selection = window.getSelection(); const previous = selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    const range = document.createRange(); range.selectNodeContents(temporary); selection.removeAllRanges(); selection.addRange(range);
    const onCopy = (event) => { event.preventDefault(); event.clipboardData.setData('text/html', html); event.clipboardData.setData('text/plain', text); };
    document.addEventListener('copy', onCopy);
    try { copied = document.execCommand('copy'); } catch { copied = false; }
    document.removeEventListener('copy', onCopy); temporary.remove(); selection.removeAllRanges(); if (previous) selection.addRange(previous);
  }
  if (copied) {
    $('copy-button').textContent = '✓ Email copied'; setTimeout(() => { $('copy-button').innerHTML = '<span aria-hidden="true">▣</span> Copy email'; }, 2800);
    notify('Email copied with formatting and links. Paste into a new message with Ctrl+V. If pictures do not paste, use Export → Outlook email draft.');
  } else {
    switchView('edit'); editor.focus(); const range = document.createRange(); range.selectNodeContents(editor); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    notify('The browser blocked clipboard access. Your email is selected: press Ctrl+C, then paste it into a new message.', 'warning', true);
  }
}

function downloadBlob(blob, extension) {
  const anchor = document.createElement('a'); const url = URL.createObjectURL(blob);
  anchor.href = url; anchor.download = `${($('email-subject').value || 'newsletter').replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').slice(0, 120)}.${extension}`; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
}

// PDF compression has its own file and result; it never rebuilds the email draft.
const compression = {file: null, blob: null, busy: false};
function setCompressionFile(file) {
  if (compression.busy) return;
  $('compression-error').hidden = true;
  if (!file || !(/\.pdf$/i.test(file.name) || file.type === 'application/pdf')) {
    $('compression-error').textContent = 'Choose a PDF file to compress.'; $('compression-error').hidden = false; return;
  }
  if (file.size > 30 * 1024 * 1024) {
    $('compression-error').textContent = 'This PDF is larger than 30 MB. Choose a smaller file.'; $('compression-error').hidden = false; return;
  }
  compression.file = file; compression.blob = null;
  $('compression-file-name').textContent = file.name;
  $('compression-file-detail').textContent = `${formatBytes(file.size)} · Original PDF`;
  $('compression-result').hidden = true; $('compression-download').hidden = true;
  $('compression-submit').hidden = false; $('compression-submit').disabled = false;
  $('compression-use-uploaded').hidden = !state.pdf || state.pdf === file;
}

function setCompressionBusy(busy) {
  compression.busy = busy;
  $('compression-dialog').setAttribute('aria-busy', String(busy));
  $('compression-progress').hidden = !busy;
  for (const id of ['compression-choose', 'compression-input', 'compression-use-uploaded', 'compression-close']) $(id).disabled = busy;
  $('compression-submit').disabled = busy || !compression.file || Boolean(compression.blob);
  $('compression-submit').textContent = busy ? 'Compressing…' : 'Compress PDF';
  $('compression-drop-zone').classList.toggle('is-busy', busy);
}

function openCompression() {
  if (!compression.file && state.pdf) setCompressionFile(state.pdf);
  $('compression-use-uploaded').hidden = !state.pdf || state.pdf === compression.file;
  $('compression-dialog').showModal();
}

async function compressPdf(event) {
  event.preventDefault();
  if (!compression.file || compression.busy) return;
  $('compression-error').hidden = true; $('compression-result').hidden = true; $('compression-download').hidden = true;
  compression.blob = null;
  setCompressionBusy(true);
  try {
    const response = await fetch('/api/compress?target=0.18', {method: 'POST', headers: {'Content-Type': 'application/pdf'}, body: compression.file});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || 'The PDF could not be compressed. Please try again.');
    }
    const blob = await response.blob();
    if (!blob.size) throw new Error('The compressed PDF was empty. Please try another file.');
    const originalBytes = Number(response.headers.get('X-Original-Bytes')) || compression.file.size;
    const compressedBytes = blob.size;
    const ratio = compressedBytes / originalBytes;
    const percentage = Math.min(100, ratio * 100);
    const targetReached = response.headers.get('X-Target-Reached') === 'true';
    const pageCount = Number(response.headers.get('X-Page-Count')) || 0;
    const linkCount = Number(response.headers.get('X-Link-Count')) || 0;
    let warnings = [];
    try { const value = JSON.parse(decodeURIComponent(response.headers.get('X-Compression-Warnings') || '%5B%5D')); if (Array.isArray(value)) warnings = value.filter((item) => typeof item === 'string'); } catch { /* Size and download remain available if optional details are missing. */ }
    compression.blob = blob;
    $('compression-original-size').textContent = formatBytes(originalBytes);
    $('compression-new-size').textContent = formatBytes(compressedBytes);
    $('compression-summary').textContent = `${percentage.toFixed(1)}% of the original · ${Math.max(0, (1 - ratio) * 100).toFixed(1)}% smaller`;
    $('compression-result-title').textContent = compressedBytes < originalBytes ? 'Your smaller PDF is ready.' : 'Your PDF is already compact.';
    $('compression-preserved').textContent = `${pageCount} page${pageCount === 1 ? '' : 's'} · ${linkCount} clickable link${linkCount === 1 ? '' : 's'} preserved`;
    $('compression-target-note').hidden = targetReached;
    $('compression-target-note').textContent = 'This PDF could not reach 18% while keeping a usable picture quality. This is the smallest version found.';
    $('compression-warnings').replaceChildren(...warnings.map((message) => { const item = document.createElement('li'); item.textContent = message; return item; }));
    $('compression-warnings').hidden = !warnings.length;
    $('compression-result').hidden = false; $('compression-download').hidden = false; $('compression-submit').hidden = true;
  } catch (error) {
    $('compression-error').textContent = error.message || 'The PDF could not be compressed. Please try again.';
    $('compression-error').hidden = false;
  } finally { setCompressionBusy(false); }
}

$('compress-pdf-button').addEventListener('click', openCompression);
$('compression-choose').addEventListener('click', () => { if (!compression.busy) $('compression-input').click(); });
$('compression-input').addEventListener('change', (event) => { const file = event.target.files[0]; event.target.value = ''; if (file) setCompressionFile(file); });
$('compression-use-uploaded').addEventListener('click', () => { if (state.pdf) setCompressionFile(state.pdf); });
$('compression-form').addEventListener('submit', compressPdf);
$('compression-dialog').addEventListener('cancel', (event) => { if (compression.busy) event.preventDefault(); });
for (const name of ['dragenter', 'dragover']) $('compression-drop-zone').addEventListener(name, (event) => { event.preventDefault(); event.stopPropagation(); if (!compression.busy) $('compression-drop-zone').classList.add('drag-over'); });
for (const name of ['dragleave', 'drop']) $('compression-drop-zone').addEventListener(name, (event) => { event.preventDefault(); event.stopPropagation(); $('compression-drop-zone').classList.remove('drag-over'); });
$('compression-drop-zone').addEventListener('drop', (event) => { if (!compression.busy && event.dataTransfer.files[0]) setCompressionFile(event.dataTransfer.files[0]); });
$('compression-download').addEventListener('click', () => {
  if (!compression.blob || !compression.file) return;
  const url = URL.createObjectURL(compression.blob); const anchor = document.createElement('a');
  const stem = compression.file.name.replace(/\.pdf$/i, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').slice(0, 120) || 'newsletter';
  anchor.href = url; anchor.download = `${stem}-compressed.pdf`; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
});

$('drop-zone').addEventListener('click', () => { if (!state.busy) $('pdf-input').click(); });
$('drop-zone').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('pdf-input').click(); } });
$('replace-button').addEventListener('click', () => $('pdf-input').click());
$('pdf-input').addEventListener('change', async (e) => { const file = e.target.files[0]; e.target.value = ''; if (file) await convertFile(file); });
for (const name of ['dragenter','dragover']) $('pdf-viewer').addEventListener(name, (e) => { e.preventDefault(); $('drop-zone').classList.add('drag-over'); });
for (const name of ['dragleave','drop']) $('pdf-viewer').addEventListener(name, (e) => { e.preventDefault(); $('drop-zone').classList.remove('drag-over'); });
$('pdf-viewer').addEventListener('drop', (e) => { const file = e.dataTransfer.files[0]; if (file) convertFile(file); });
window.addEventListener('dragover', (e) => e.preventDefault()); window.addEventListener('drop', (e) => e.preventDefault());
$('sample-button').addEventListener('click', async () => {
  if (state.busy) return;
  $('sample-button').disabled = true;
  try { const response = await fetch('/api/sample'); if (!response.ok) throw new Error('The October sample is not available here. Use Choose a PDF to select your newsletter.'); const bytes = await response.blob(); await convertFile(new File([bytes], 'PSIS 499 PTA Newsletter_ October.pdf', {type: 'application/pdf'})); }
  catch (error) { notify(error.message, 'error', true); }
  finally { $('sample-button').disabled = false; }
});

for (const tab of document.querySelectorAll('[data-view]')) {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
  tab.addEventListener('keydown', (e) => { if (['ArrowLeft','ArrowRight'].includes(e.key)) { e.preventDefault(); const tabs = [...document.querySelectorAll('[data-view]')]; const next = tabs[(tabs.indexOf(tab) + (e.key === 'ArrowRight' ? 1 : 2)) % 3]; next.focus(); switchView(next.dataset.view); } });
}
for (const button of document.querySelectorAll('[data-command]')) {
  button.disabled = true;
  button.addEventListener('mousedown', (e) => e.preventDefault());
  button.addEventListener('click', () => { if (!state.document) return; if (button.dataset.command === 'undo') { undoEdit(-1); return; } if (button.dataset.command === 'redo') { undoEdit(1); return; } restoreSelection(); document.execCommand(button.dataset.command, false); rememberSelection(); markEdited(); });
}
$('insert-link').disabled = true; $('block-format').disabled = true;
$('block-format').addEventListener('mousedown', rememberSelection);
$('block-format').addEventListener('change', () => { restoreSelection(); document.execCommand('formatBlock', false, $('block-format').value); rememberSelection(); markEdited(); });
$('insert-link').addEventListener('mousedown', (e) => e.preventDefault()); $('insert-link').addEventListener('click', openLinkDialog);
editor.addEventListener('input', () => { clearPictureSelection(); markEdited(); }); editor.addEventListener('keyup', rememberSelection); editor.addEventListener('mouseup', rememberSelection); editor.addEventListener('blur', rememberSelection);
editor.addEventListener('click', (e) => { if (e.target.closest('a')) e.preventDefault(); if (e.target.tagName === 'IMG') { e.preventDefault(); selectPicture(e.target); } else if (!e.target.closest('table[data-pdf-page]')) clearPictureSelection(); });
editor.addEventListener('dblclick', (e) => { if (e.target.closest('a')) { e.preventDefault(); openLinkDialog(); } });
for (const action of ['copy','cut']) editor.addEventListener(action, (e) => {
  const unit = state.selectedUnit; if (!unit || !editor.contains(unit) || !e.clipboardData || !window.getSelection().isCollapsed) return;
  e.preventDefault();
  e.clipboardData.setData('text/html', cleanHtml(unit.outerHTML));
  e.clipboardData.setData('text/plain', state.selectedImage?.alt || 'Newsletter picture');
  e.clipboardData.setData('application/x-newsletter-picture', cleanHtml(unit.outerHTML, {internal: true}));
  if (action === 'cut') { clearPictureSelection(); unit.remove(); state.savedRange = null; markEdited(); }
});
editor.addEventListener('paste', async (e) => {
  if (!state.document) return;
  e.preventDefault();
  const range = editorRange(); const clipboard = e.clipboardData;
  const files = [...clipboard.files];
  if (files.length) { await insertPictureFiles(files, range); return; }
  const internalPicture = clipboard.getData('application/x-newsletter-picture');
  const html = clipboard.getData('text/html'); const text = clipboard.getData('text/plain');
  const clean = internalPicture ? cleanHtml(internalPicture, {internal: true}) : html ? cleanHtml(html) : escapeHtml(text).replace(/\n/g, '<br>');
  if (!clean) return;
  const fragment = document.createElement('div'); fragment.innerHTML = clean;
  if (fragment.querySelector('img')) { const nodes = [...fragment.childNodes]; insertNodes(nodes, range); const images = nodes.flatMap((node) => node.nodeType === Node.ELEMENT_NODE ? [...(node.tagName === 'IMG' ? [node] : []), ...node.querySelectorAll('img')] : []); if (images.length) selectPicture(images[images.length - 1]); return; }
  const safeRange = outsidePageRange(range); editor.focus(); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(safeRange);
  document.execCommand('insertHTML', false, fragment.innerHTML); markEdited(); rememberSelection();
  if (html && /<img\b/i.test(html) && !fragment.querySelector('img')) notify('The copied picture uses an external address. Use Add picture, paste an image file, or drop a saved picture here to embed it in your email.', 'warning', true);
});
editor.addEventListener('keydown', (e) => {
  const modifier = e.ctrlKey || e.metaKey;
  if (modifier && e.key.toLowerCase() === 'k') { e.preventDefault(); openLinkDialog(); }
  if (modifier && ['z','y'].includes(e.key.toLowerCase())) { e.preventDefault(); undoEdit(e.key.toLowerCase() === 'y' || e.shiftKey ? 1 : -1); }
  if (!modifier && state.selectedUnit && ['Delete','Backspace'].includes(e.key)) { e.preventDefault(); const unit = state.selectedUnit; clearPictureSelection(); unit.remove(); state.savedRange = null; markEdited(); return; }
  if (!modifier && (e.key.length === 1 || ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter','Home','End'].includes(e.key))) clearPictureSelection();
  if (e.key === 'Escape') clearPictureSelection();
});
editor.addEventListener('dragstart', (e) => {
  const image = e.target.tagName === 'IMG' ? e.target : e.target.closest('table[data-pdf-page]')?.querySelector('img');
  if (!image || !editor.contains(image)) return;
  selectPicture(image); state.draggedUnit = state.selectedUnit;
  e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('application/x-newsletter-picture', 'move'); e.dataTransfer.setData('text/plain', image.alt || 'Newsletter picture');
});
editor.addEventListener('dragend', () => { state.draggedUnit = null; editor.classList.remove('picture-drop-target'); });
editor.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = state.draggedUnit ? 'move' : 'copy'; editor.classList.add('picture-drop-target'); });
editor.addEventListener('dragleave', (e) => { if (!editor.contains(e.relatedTarget)) editor.classList.remove('picture-drop-target'); });
editor.addEventListener('drop', async (e) => {
  e.preventDefault(); e.stopPropagation(); editor.classList.remove('picture-drop-target');
  if (!state.document) return;
  const range = rangeAtPoint(e.clientX, e.clientY); range.collapse(true);
  const unit = state.draggedUnit;
  if (unit && editor.contains(unit)) {
    state.draggedUnit = null;
    if (unit.contains(range.startContainer) || unit === range.startContainer) return;
    const image = state.selectedImage; insertNodes([unit], range, e.clientY); selectPicture(image); return;
  }
  const files = [...e.dataTransfer.files];
  if (files.length) await insertPictureFiles(files, range, e.clientY);
});
$('add-picture').addEventListener('mousedown', (e) => { e.preventDefault(); rememberSelection(); });
$('add-picture').addEventListener('click', () => { if (state.document) { state.savedRange = editorRange(); $('picture-input').click(); } });
$('picture-input').addEventListener('change', async (e) => { const files = [...e.target.files]; e.target.value = ''; if (files.length) await insertPictureFiles(files, editorRange()); });
for (const button of document.querySelectorAll('#picture-tools button')) button.addEventListener('mousedown', (e) => e.preventDefault());
$('picture-earlier').addEventListener('click', () => moveSelectedPicture(-1));
$('picture-later').addEventListener('click', () => moveSelectedPicture(1));
for (const button of document.querySelectorAll('[data-picture-align]')) button.addEventListener('click', () => {
  const image = state.selectedImage; const unit = state.selectedUnit; if (!image || !editor.contains(image) || unit.matches('table[data-pdf-page]')) return;
  const align = button.dataset.pictureAlign;
  image.style.display = 'block'; image.style.marginLeft = align === 'left' ? '0' : 'auto'; image.style.marginRight = align === 'right' ? '0' : 'auto';
  if (/^(P|DIV)$/.test(unit.tagName)) unit.style.textAlign = align;
  markEdited();
});
$('picture-width').addEventListener('change', () => {
  const image = state.selectedImage; if (!image || !editor.contains(image) || state.selectedUnit.matches('table[data-pdf-page]')) return;
  const width = $('picture-width').value; image.style.width = `${width}%`; image.style.maxWidth = '100%'; image.style.height = 'auto'; image.setAttribute('width', `${width}%`); markEdited();
});
$('picture-remove').addEventListener('click', () => { const unit = state.selectedUnit; if (!unit || !editor.contains(unit)) return; clearPictureSelection(); unit.remove(); state.savedRange = null; markEdited(); });
$('picture-done').addEventListener('click', () => { clearPictureSelection(); editor.focus(); });
$('email-subject').addEventListener('input', markEdited);
$('link-form').addEventListener('submit', (e) => {
  e.preventDefault(); const url = safeUrl($('link-url').value);
  if (!url) { $('link-error').textContent = 'Enter a complete http://, https://, mailto:, or tel: address.'; $('link-error').hidden = false; return; }
  const label = $('link-label').value.trim(); if (!label) return;
  $('link-dialog').close(); restoreSelection();
  if (state.activeLink && editor.contains(state.activeLink)) { state.activeLink.href = url; if (!state.activeLink.querySelector('img')) state.activeLink.textContent = label; }
  else if (state.selectedImage && editor.contains(state.selectedImage)) { const anchor = document.createElement('a'); anchor.href = url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; state.selectedImage.replaceWith(anchor); anchor.append(state.selectedImage); }
  else document.execCommand('insertHTML', false, `<a href="${escapeHtml(url)}" style="color:#19574a;text-decoration:underline" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
  markEdited(); rememberSelection();
});
$('remove-link').addEventListener('click', () => { const link = state.activeLink; $('link-dialog').close(); if (link && editor.contains(link)) { link.replaceWith(...link.childNodes); markEdited(); } });
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $(button.dataset.close).close());
$('confirm-cancel').addEventListener('click', () => $('confirm-dialog').close('no')); $('confirm-yes').addEventListener('click', () => $('confirm-dialog').close('yes'));
$('copy-button').addEventListener('click', copyEmail);
$('download-button').addEventListener('click', () => $('export-dialog').showModal());
$('export-html').addEventListener('click', () => { downloadBlob(new Blob([fullEmail()], {type: 'text/html;charset=utf-8'}), 'html'); notify('Email HTML downloaded with your edits and links.'); });
$('export-eml').addEventListener('click', async () => {
  const button = $('export-eml'); button.disabled = true;
  try { const response = await fetch('/api/export-eml', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({html: emailBody(), subject: $('email-subject').value || 'Newsletter'})}); if (!response.ok) { const data = await response.json(); throw new Error(data.error || 'Could not create the email draft.'); } downloadBlob(await response.blob(), 'eml'); $('export-dialog').close(); notify('Email draft downloaded. Open it in Outlook or another compatible email app; pictures are embedded in the draft.'); }
  catch (error) { $('export-dialog').close(); notify(error.message, 'error', true); }
  finally { button.disabled = false; }
});
$('reset-button').addEventListener('click', async () => { if (!state.document || !await confirmChange('Reset your email?', 'This restores the original conversion and replaces your email edits.', 'Reset email')) return; clearPictureSelection(); editor.innerHTML = state.originalHtml; $('email-subject').value = state.originalSubject; state.dirty = false; state.savedRange = null; decorateEditor(); recordHistory(true); updateMetrics(); switchView('edit'); notify('Email restored to its original conversion.'); });
$('conversion-layout').addEventListener('change', async () => {
  const previous = state.layout; const previousImages = state.imageSetting;
  if (!state.pdf) { state.layout = $('conversion-layout').value; updateLayoutControls(); state.imageSetting = $('include-images').checked; return; }
  if (state.dirty && !await confirmChange('Change email layout?', 'Changing the layout recreates the email from the PDF and replaces your current edits.', 'Rebuild email')) { $('conversion-layout').value = previous; updateLayoutControls(); return; }
  updateLayoutControls();
  const success = await convertFile(state.pdf, {replace: false});
  if (!success) { $('conversion-layout').value = previous; $('include-images').checked = previousImages; updateLayoutControls(); }
});
$('include-images').addEventListener('change', async () => {
  if (!state.pdf) { state.imageSetting = $('include-images').checked; return; }
  if (state.dirty && !await confirmChange('Rebuild the email?', 'Changing pictures recreates the email from the PDF and replaces your current edits.', 'Rebuild email')) { $('include-images').checked = state.imageSetting; return; }
  const success = await convertFile(state.pdf, {replace: false}); if (!success) $('include-images').checked = state.imageSetting;
});
$('previous-page').addEventListener('click', () => goToPage(-1)); $('next-page').addEventListener('click', () => goToPage(1)); $('zoom').addEventListener('change', applyZoom);
$('show-links').addEventListener('change', () => $('pdf-pages').classList.toggle('show-links', $('show-links').checked));
$('pdf-viewer').addEventListener('scroll', () => { if (!state.document) return; const view = $('pdf-viewer').getBoundingClientRect(); let closest = 1; let distance = Infinity; for (const page of document.querySelectorAll('.pdf-page')) { const rect = page.getBoundingClientRect(); const d = Math.abs(rect.top - view.top - 25); if (d < distance) { closest = Number(page.dataset.page); distance = d; } } state.currentPage = closest; updatePageLabel(); }, {passive: true});
for (const mode of ['desktop','mobile']) $(`${mode}-preview`).addEventListener('click', () => { $('email-preview').classList.toggle('mobile', mode === 'mobile'); for (const other of ['desktop','mobile']) { $(`${other}-preview`).classList.toggle('selected', mode === other); $(`${other}-preview`).setAttribute('aria-pressed', String(mode === other)); } });
window.addEventListener('beforeunload', (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
switchView('edit');
updateLayoutControls();
