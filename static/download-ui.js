(function () {
  'use strict';

  const icons = {
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>',
    send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"></path><path d="M22 2 11 13"></path></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg>',
  };

  function escapeHtml(value) {
    const element = document.createElement('div');
    element.textContent = String(value || '');
    return element.innerHTML;
  }

  function cleanFilename(title, extension) {
    const base = String(title || 'book')
      .normalize('NFKC')
      .replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 90) || 'book';
    const ext = String(extension || 'epub').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'epub';
    return base + '.' + ext;
  }

  function shorten(value, length) {
    const text = String(value || '').trim();
    return text.length > length ? text.slice(0, length - 1).trimEnd() + '…' : text;
  }

  function metaItem(value) {
    return value ? '<span class="edition-meta-item">' + escapeHtml(value) + '</span>' : '';
  }

  function renderEdition(book, index) {
    const title = shorten(book.title || 'Untitled', 150);
    const author = shorten(book.author || '', 90);
    const publisher = shorten(book.publisher || '', 90);
    const extension = String(book.ext || '').toLowerCase();
    const format = extension || 'file';
    const filename = cleanFilename(book.title, format);
    const downloadHref = book.md5
      ? '/download/' + encodeURIComponent(book.md5) + '?filename=' + encodeURIComponent(filename)
      : '';
    const cover = book.cover_url
      ? '<img class="edition-cover" src="' + escapeHtml(book.cover_url) + '" alt="" loading="lazy" decoding="async" onload="this.classList.add(\'loaded\')" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="edition-cover-placeholder" hidden aria-hidden="true">' + escapeHtml((title[0] || '?').toUpperCase()) + '</div>'
      : '<div class="edition-cover-placeholder" aria-hidden="true">' + escapeHtml((title[0] || '?').toUpperCase()) + '</div>';
    const metadata = [
      '<span class="edition-format ' + escapeHtml(format) + '">' + escapeHtml(format) + '</span>',
      metaItem(book.year),
      metaItem(book.size),
      metaItem(book.pages ? book.pages + ' pages' : ''),
      metaItem(shorten(book.language, 18)),
    ].join('');
    const actions = book.md5
      ? '<div class="edition-actions">' +
          '<a class="edition-action edition-download" href="' + downloadHref + '" data-format="' + escapeHtml(format) + '" aria-label="Download ' + escapeHtml(title) + ' as ' + escapeHtml(format.toUpperCase()) + '">' + icons.download + '<span>' + escapeHtml(format.toUpperCase()) + '</span></a>' +
          '<button class="edition-action edition-kindle" type="button" data-md5="' + escapeHtml(book.md5) + '" data-title="' + escapeHtml(book.title || '') + '" data-format="' + escapeHtml(format) + '" aria-label="Send ' + escapeHtml(title) + ' to Kindle">' + icons.send + '<span>Kindle</span></button>' +
        '</div>'
      : '<div class="edition-actions"><span class="edition-action edition-kindle" aria-disabled="true">Unavailable</span></div>';

    return '<article class="edition-row' + (index === 0 ? ' recommended' : '') + '">' +
      '<div>' + cover + '</div>' +
      '<div class="edition-copy">' +
        '<div class="edition-title-line"><h3 class="edition-title" title="' + escapeHtml(book.title || '') + '">' + escapeHtml(title) + '</h3>' + (index === 0 ? '<span class="edition-recommended">Best match</span>' : '') + '</div>' +
        (author ? '<div class="edition-byline">' + escapeHtml(author) + '</div>' : '') +
        (publisher ? '<div class="edition-publisher">' + escapeHtml(publisher) + '</div>' : '') +
        '<div class="edition-meta">' + metadata + '</div>' +
      '</div>' +
      actions +
    '</article>';
  }

  function renderEditions(container, books) {
    if (!container) return;
    container.innerHTML = (books || []).map(renderEdition).join('');
    container.hidden = !(books || []).length;
    wireActions(container);
  }

  const wiredContainers = new WeakSet();
  function wireActions(container) {
    if (!container || wiredContainers.has(container)) return;
    wiredContainers.add(container);
    container.addEventListener('click', event => {
      const download = event.target.closest('.edition-download');
      if (download) {
        if (!download.dataset.originalHtml) download.dataset.originalHtml = download.innerHTML;
        download.classList.add('busy');
        download.setAttribute('aria-busy', 'true');
        download.innerHTML = '<span class="download-spinner" aria-hidden="true"></span><span>Preparing</span>';
        window.LibFlixNotify?.('Preparing ' + String(download.dataset.format || 'book').toUpperCase() + ' download');
        window.setTimeout(() => {
          download.classList.remove('busy');
          download.removeAttribute('aria-busy');
          if (download.dataset.originalHtml) download.innerHTML = download.dataset.originalHtml;
        }, 5000);
        return;
      }

      const kindle = event.target.closest('.edition-kindle[data-md5]');
      if (!kindle || typeof window.sendToKindle !== 'function') return;
      window.sendToKindle(kindle.dataset.md5, kindle.dataset.title, kindle.dataset.format, kindle);
    });
  }

  function createKindleProgress(button) {
    const row = button?.closest('.edition-row');
    if (!row) return null;
    let panel = row.querySelector('.kindle-progress');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'kindle-progress';
      panel.setAttribute('role', 'status');
      panel.setAttribute('aria-live', 'polite');
      panel.innerHTML =
        '<div class="kindle-progress-head">' +
          '<span class="kindle-progress-stage">Preparing delivery</span>' +
          '<span class="kindle-progress-value">0%</span>' +
        '</div>' +
        '<div class="kindle-progress-track" role="progressbar" aria-label="Send to Kindle progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">' +
          '<span class="kindle-progress-fill"></span>' +
        '</div>' +
        '<div class="kindle-progress-detail" hidden></div>';
      row.appendChild(panel);
    }
    panel.className = 'kindle-progress visible';
    panel.setAttribute('role', 'status');
    return panel;
  }

  function updateKindleProgress(panel, event) {
    if (!panel || !event) return;
    const stage = panel.querySelector('.kindle-progress-stage');
    const value = panel.querySelector('.kindle-progress-value');
    const track = panel.querySelector('.kindle-progress-track');
    const fill = panel.querySelector('.kindle-progress-fill');
    const detail = panel.querySelector('.kindle-progress-detail');
    const progress = Number(event.progress);
    const hasProgress = event.progress !== null && event.progress !== undefined && Number.isFinite(progress);

    stage.textContent = event.stage || 'Sending to Kindle';
    detail.textContent = event.detail || '';
    detail.hidden = !event.detail;
    panel.classList.toggle('indeterminate', !hasProgress && event.type === 'progress');
    panel.classList.toggle('complete', event.type === 'complete');
    panel.classList.toggle('error', event.type === 'error');

    if (hasProgress) {
      const bounded = Math.max(0, Math.min(100, Math.round(progress)));
      fill.style.width = bounded + '%';
      value.textContent = bounded + '%';
      track.setAttribute('aria-valuenow', String(bounded));
      track.removeAttribute('aria-valuetext');
    } else {
      fill.style.width = '';
      value.textContent = event.type === 'error' ? 'Failed' : 'Working';
      track.removeAttribute('aria-valuenow');
      track.setAttribute('aria-valuetext', event.stage || 'Working');
    }
  }

  async function readKindleProgress(response, onEvent) {
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok || contentType.includes('application/json')) {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.success === false) {
        throw new Error(payload.error || 'Kindle delivery failed');
      }
      const event = { type: 'complete', success: true, stage: 'Sent to Kindle', progress: 100 };
      onEvent(event);
      return event;
    }
    if (!response.body) throw new Error('Live delivery progress is unavailable');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let completed = null;
    const consumeLine = line => {
      if (!line.trim()) return;
      const event = JSON.parse(line);
      onEvent(event);
      if (event.type === 'error' || event.success === false) {
        const error = new Error(event.error || 'Kindle delivery failed');
        error.kindleEvent = event;
        throw error;
      }
      if (event.type === 'complete') completed = event;
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let newline = buffer.indexOf('\n');
      while (newline !== -1) {
        consumeLine(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf('\n');
      }
      if (done) break;
    }
    if (buffer.trim()) consumeLine(buffer);
    if (!completed) throw new Error('Kindle delivery ended before confirmation');
    return completed;
  }

  async function deliverToKindle({ button, payload }) {
    if (!button) throw new Error('Kindle action is unavailable');
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    if (!button.dataset.originalAriaLabel) button.dataset.originalAriaLabel = button.getAttribute('aria-label') || 'Send to Kindle';

    const panel = createKindleProgress(button);
    updateKindleProgress(panel, { type: 'progress', stage: 'Preparing delivery', progress: 0 });
    button.classList.remove('sent');
    button.classList.add('sending');
    button.setAttribute('aria-busy', 'true');
    button.setAttribute('aria-label', 'Sending book to Kindle');
    button.innerHTML = icons.send + '<span>Sending</span>';

    try {
      const response = await fetch('/api/sendtokindle?stream=1', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const completed = await readKindleProgress(response, event => updateKindleProgress(panel, event));
      button.classList.add('sent');
      button.innerHTML = icons.check + '<span>Sent</span>';
      button.setAttribute('aria-label', 'Sent to Kindle');
      return completed;
    } catch (error) {
      const failure = error.kindleEvent || {};
      updateKindleProgress(panel, {
        type: 'error',
        stage: failure.stage || 'Delivery failed',
        progress: null,
        detail: error.message,
      });
      panel?.setAttribute('role', 'alert');
      button.innerHTML = button.dataset.originalHtml;
      button.setAttribute('aria-label', button.dataset.originalAriaLabel);
      throw error;
    } finally {
      button.classList.remove('sending');
      button.removeAttribute('aria-busy');
    }
  }

  function visiblePages(page, total) {
    const pages = new Set([1, total, page - 1, page, page + 1]);
    return Array.from(pages).filter(value => value >= 1 && value <= total).sort((a, b) => a - b);
  }

  function renderPagination(container, page, totalPages, onPage) {
    if (!container) return;
    const total = Number(totalPages) || 1;
    const current = Number(page) || 1;
    if (total <= 1) {
      container.innerHTML = '';
      container.hidden = true;
      return;
    }

    const pages = visiblePages(current, total);
    let last = 0;
    let html = '<button class="download-page-button' + (current <= 1 ? ' disabled' : '') + '" type="button" data-page="' + (current - 1) + '">Previous</button>';
    for (const value of pages) {
      if (last && value - last > 1) html += '<span class="download-page-button disabled" aria-hidden="true">…</span>';
      html += value === current
        ? '<span class="download-page-current" aria-current="page">' + value + '</span>'
        : '<button class="download-page-button" type="button" data-page="' + value + '">' + value + '</button>';
      last = value;
    }
    html += '<button class="download-page-button' + (current >= total ? ' disabled' : '') + '" type="button" data-page="' + (current + 1) + '">Next</button>';
    container.innerHTML = html;
    container.hidden = false;
    container.querySelectorAll('button[data-page]').forEach(button => {
      button.addEventListener('click', () => {
        const requested = Number(button.dataset.page);
        if (requested >= 1 && requested <= total && requested !== current) onPage(requested);
      });
    });
  }

  function friendlyError(message) {
    const text = String(message || '').toLowerCase();
    if (text.includes('timed out') || text.includes('timeout')) {
      return 'The download source is taking too long to respond. Try again in a moment.';
    }
    if (text.includes('connection') || text.includes('network') || text.includes('unreachable')) {
      return 'The download source is temporarily unreachable. Your book details are still available.';
    }
    return 'Downloads could not be checked right now. Try again in a moment.';
  }

  window.LibFlixDownloads = {
    checkIcon: icons.check,
    cleanFilename,
    deliverToKindle,
    escapeHtml,
    friendlyError,
    renderEditions,
    renderPagination,
  };
})();
