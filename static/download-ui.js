(function () {
  'use strict';

  const icons = {
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>',
    send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"></path><path d="M22 2 11 13"></path></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg>',
    chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"></path></svg>',
    info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path></svg>',
  };
  const hiddenKindleFormats = new Set(['azw', 'azw3', 'mobi']);

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

  function renderEdition(book, index, options) {
    options = options || {};
    const title = shorten(book.title || 'Untitled', 150);
    const author = shorten(book.author || '', 90);
    const publisher = shorten(book.publisher || '', 90);
    const extension = String(book.ext || '').toLowerCase();
    const format = extension || 'file';
    const kindleCompatible = book.kindle_compatible === true || ['epub', 'pdf'].includes(extension);
    const recommended = book.best_match === true;
    const reasons = Array.isArray(book.recommendation_reasons)
      ? book.recommendation_reasons.filter(Boolean).slice(0, 4)
      : [];
    const recommendationId = 'edition-reasons-' + index;
    const recommendation = recommended
      ? reasons.length
        ? '<span class="edition-recommendation">' +
            '<button class="edition-recommended" type="button" aria-describedby="' + recommendationId + '">Best for Kindle' + icons.info + '</button>' +
            '<span class="edition-reasons-tooltip" id="' + recommendationId + '" role="tooltip">' +
              '<span class="edition-tooltip-title">Why this edition</span>' +
              '<span class="edition-tooltip-list">' + reasons.map(reason => '<span class="edition-tooltip-reason">' + icons.check + '<span>' + escapeHtml(reason) + '</span></span>').join('') + '</span>' +
            '</span>' +
          '</span>'
        : '<span class="edition-recommended">Best for Kindle</span>'
      : '';
    const filename = cleanFilename(book.title, format);
    const downloadHref = book.md5
      ? '/download/' + encodeURIComponent(book.md5) + '?filename=' + encodeURIComponent(filename)
      : '';
    const fallbackCoverUrl = String(options.fallbackCoverUrl || '');
    const coverUrl = String(book.cover_url || fallbackCoverUrl);
    const failedCoverFallback = book.cover_url && fallbackCoverUrl && book.cover_url !== fallbackCoverUrl
      ? fallbackCoverUrl
      : '';
    const cover = coverUrl
      ? '<span class="edition-cover-loading" aria-hidden="true"></span><img class="edition-cover" data-cover-src="' + escapeHtml(coverUrl) + '" data-cover-fallback="' + escapeHtml(failedCoverFallback) + '" alt="" loading="lazy" decoding="async" onload="this.classList.add(\'loaded\')" onerror="if(this.dataset.coverFallback&&this.dataset.coverFallbackTried!==\'1\'){this.dataset.coverFallbackTried=\'1\';this.src=this.dataset.coverFallback}else{this.hidden=true;this.nextElementSibling.hidden=false}"><div class="edition-cover-placeholder" hidden aria-hidden="true">' + escapeHtml((title[0] || '?').toUpperCase()) + '</div>'
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
          (kindleCompatible
            ? '<button class="edition-action edition-kindle" type="button" data-md5="' + escapeHtml(book.md5) + '" data-title="' + escapeHtml(book.title || '') + '" data-author="' + escapeHtml(book.author || '') + '" data-publisher="' + escapeHtml(book.publisher || '') + '" data-year="' + escapeHtml(book.year || '') + '" data-language="' + escapeHtml(book.language || '') + '" data-cover-url="' + escapeHtml(coverUrl) + '" data-format="' + escapeHtml(format) + '" aria-label="Send ' + escapeHtml(title) + ' to Kindle">' + icons.send + '<span>Kindle</span></button>'
            : '') +
        '</div>'
      : '<div class="edition-actions"><span class="edition-action edition-kindle" aria-disabled="true">Unavailable</span></div>';

    return '<article class="edition-row' + (recommended ? ' recommended' : '') + '">' +
      '<div class="edition-cover-frame">' + cover + '</div>' +
      '<div class="edition-copy">' +
        '<div class="edition-title-line"><h3 class="edition-title" title="' + escapeHtml(book.title || '') + '">' + escapeHtml(title) + '</h3>' + recommendation + '</div>' +
        (author ? '<div class="edition-byline">' + escapeHtml(author) + '</div>' : '') +
        (publisher ? '<div class="edition-publisher">' + escapeHtml(publisher) + '</div>' : '') +
        '<div class="edition-meta">' + metadata + '</div>' +
      '</div>' +
      actions +
    '</article>';
  }

  function renderEditions(container, books, options) {
    if (!container) return 0;
    options = options || {};
    const visibleBooks = (books || []).filter(book => {
      const extension = String(book.ext || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      return !hiddenKindleFormats.has(extension);
    });
    const bestIndex = Math.max(0, visibleBooks.findIndex(book => book.best_match === true));
    const bestBook = visibleBooks[bestIndex];
    const otherBooks = visibleBooks.filter((book, index) => index !== bestIndex);
    const primary = bestBook
      ? renderEdition(bestBook.best_match === true ? bestBook : { ...bestBook, best_match: true }, 0, options)
      : '';
    const hasMoreOptions = otherBooks.length || options.hasMorePages;
    const others = hasMoreOptions
      ? '<details class="edition-more"' + (options.expanded ? ' open' : '') + '>' +
          '<summary class="edition-more-summary"><span>Other options</span>' + icons.chevron + '</summary>' +
          '<div class="edition-more-list">' + otherBooks.map((book, index) => renderEdition(book, index + 1, options)).join('') + '</div>' +
        '</details>'
      : '';
    container.innerHTML = primary + others;
    container.hidden = !visibleBooks.length;
    window.LibFlixCoverLoader?.register(container);
    wireActions(container);
    resumeKindleJobs(container);
    return visibleBooks.length;
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
      if (!kindle) return;
      const existingJob = storedKindleJobId(kindle.dataset.md5);
      if (existingJob) {
        deliverToKindle({
          button: kindle,
          payload: { md5: kindle.dataset.md5 },
          jobId: existingJob,
        }).catch(error => window.LibFlixNotify?.('Delivery status unavailable: ' + error.message, 'error'));
        return;
      }
      if (typeof window.sendToKindle !== 'function') return;
      window.sendToKindle(
        kindle.dataset.md5,
        kindle.dataset.title,
        kindle.dataset.format,
        kindle,
        {
          author: kindle.dataset.author || '',
          publisher: kindle.dataset.publisher || '',
          year: kindle.dataset.year || '',
          language: kindle.dataset.language || '',
          coverUrl: kindle.dataset.coverUrl || '',
        },
      );
    });
  }

  function createKindleProgress(button) {
    const row = button?.closest('.edition-row');
    if (!row) return null;
    const disclosure = button.closest('.edition-more');
    if (disclosure) disclosure.open = true;
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

  const ACTIVE_KINDLE_JOBS_KEY = 'libflix.kindleActiveJobs.v1';
  const activeJobPolls = new Map();
  let globalTrayHideTimer = null;

  function progressPresentation(event) {
    const stage = String(event?.stage || '');
    if (event?.type === 'complete') return { hasProgress: true, value: 100, label: 'Sent' };
    if (event?.type === 'error') return { hasProgress: false, value: 0, label: 'Failed' };
    const totalBytes = Number(event?.upload_total_bytes ?? event?.total_bytes);
    const sentBytes = Number(event?.uploaded_bytes ?? event?.bytes_sent);
    const isUpload = /upload|sending to kindle/i.test(stage);
    if (isUpload && Number.isFinite(totalBytes) && totalBytes > 0 && Number.isFinite(sentBytes) && sentBytes >= 0) {
      const ratio = Math.max(0, Math.min(1, sentBytes / totalBytes));
      const value = 75 + (ratio * 24);
      return { hasProgress: true, value, label: Math.round(ratio * 100) + '% uploaded' };
    }
    const numeric = Number(event?.display_progress ?? event?.progress);
    if (isUpload) return { hasProgress: false, value: 0, label: 'Uploading' };
    if (event?.progress !== null && event?.progress !== undefined && Number.isFinite(numeric)) {
      return { hasProgress: true, value: numeric, label: Math.round(numeric) + '%' };
    }
    return { hasProgress: false, value: 0, label: 'Working' };
  }

  function readActiveKindleJobs() {
    try {
      const parsed = JSON.parse(window.sessionStorage.getItem(ACTIVE_KINDLE_JOBS_KEY) || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  }

  function writeActiveKindleJobs(jobs) {
    try {
      window.sessionStorage.setItem(ACTIVE_KINDLE_JOBS_KEY, JSON.stringify(jobs));
    } catch {}
  }

  function rememberActiveKindleJob(md5, jobId, metadata = {}) {
    if (!md5 || !jobId) return;
    const jobs = readActiveKindleJobs();
    jobs[String(md5).toLowerCase()] = {
      jobId,
      title: metadata.canonical_title || metadata.title || 'Book',
      startedAt: Date.now(),
    };
    writeActiveKindleJobs(jobs);
    try {
      window.sessionStorage.setItem(kindleJobStorageKey(md5), jobId);
    } catch {}
  }

  function forgetActiveKindleJob(md5, jobId = '') {
    if (!md5) return;
    const key = String(md5).toLowerCase();
    const jobs = readActiveKindleJobs();
    if (!jobId || jobs[key]?.jobId === jobId) delete jobs[key];
    writeActiveKindleJobs(jobs);
    try {
      window.sessionStorage.removeItem(kindleJobStorageKey(md5));
    } catch {}
  }

  function updateGlobalKindleTray(event, metadata = {}) {
    const tray = document.getElementById('kindleGlobalTray');
    if (!tray || !event) return;
    if (globalTrayHideTimer) window.clearTimeout(globalTrayHideTimer);
    globalTrayHideTimer = null;
    const presentation = progressPresentation(event);
    const title = document.getElementById('kindleGlobalTrayTitle');
    const stage = document.getElementById('kindleGlobalTrayStage');
    const value = document.getElementById('kindleGlobalTrayValue');
    const detail = document.getElementById('kindleGlobalTrayDetail');
    const track = document.getElementById('kindleGlobalTrayTrack');
    const fill = document.getElementById('kindleGlobalTrayFill');
    tray.hidden = false;
    tray.classList.toggle('indeterminate', !presentation.hasProgress && event.type === 'progress');
    tray.classList.toggle('complete', event.type === 'complete');
    tray.classList.toggle('error', event.type === 'error' || event.success === false);
    if (title) title.textContent = event.title || metadata.title || metadata.canonical_title || 'Book';
    if (stage) stage.textContent = event.stage || 'Sending to Kindle';
    if (value) value.textContent = presentation.label;
    if (detail) {
      detail.textContent = event.detail || event.error || '';
      detail.hidden = !detail.textContent;
    }
    if (track && fill) {
      if (presentation.hasProgress) {
        const bounded = Math.max(0, Math.min(100, Math.round(presentation.value)));
        fill.style.width = bounded + '%';
        track.setAttribute('aria-valuenow', String(bounded));
        track.removeAttribute('aria-valuetext');
      } else {
        fill.style.width = '';
        track.removeAttribute('aria-valuenow');
        track.setAttribute('aria-valuetext', presentation.label);
      }
    }
    if (event.type === 'complete') {
      globalTrayHideTimer = window.setTimeout(() => { tray.hidden = true; }, 8000);
    }
  }

  function updateKindleProgress(panel, event) {
    if (!panel || !event) return;
    const stage = panel.querySelector('.kindle-progress-stage');
    const value = panel.querySelector('.kindle-progress-value');
    const track = panel.querySelector('.kindle-progress-track');
    const fill = panel.querySelector('.kindle-progress-fill');
    const detail = panel.querySelector('.kindle-progress-detail');
    const presentation = progressPresentation(event);

    stage.textContent = event.stage || 'Sending to Kindle';
    detail.textContent = event.detail || '';
    detail.hidden = !event.detail;
    panel.classList.toggle('indeterminate', !presentation.hasProgress && event.type === 'progress');
    panel.classList.toggle('complete', event.type === 'complete');
    panel.classList.toggle('error', event.type === 'error');

    if (presentation.hasProgress) {
      const bounded = Math.max(0, Math.min(100, Math.round(presentation.value)));
      fill.style.width = bounded + '%';
      value.textContent = presentation.label;
      track.setAttribute('aria-valuenow', String(bounded));
      track.removeAttribute('aria-valuetext');
    } else {
      fill.style.width = '';
      value.textContent = presentation.label;
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

  const wait = duration => new Promise(resolve => window.setTimeout(resolve, duration));

  function formatElapsedTime(value) {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    if (seconds < 1) return 'under a second';
    if (seconds < 60) return seconds + 's';
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes + 'm' + (remainder ? ' ' + remainder + 's' : '');
  }

  async function pollKindleJob(jobId, onEvent) {
    let cursor = 0;
    let failures = 0;
    let idlePolls = 0;
    const pollingStartedAt = performance.now();
    while (true) {
      try {
        const response = await fetch('/api/kindle/jobs/' + encodeURIComponent(jobId) + '?cursor=' + cursor, {
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
          const error = new Error(data.error || 'Delivery status is unavailable');
          error.definitive = response.status === 400 || response.status === 404;
          throw error;
        }
        failures = 0;
        const receivedEvents = Array.isArray(data.events) ? data.events.length : 0;
        idlePolls = receivedEvents ? 0 : idlePolls + 1;
        cursor = Number(data.cursor) || cursor;
        for (const event of data.events || []) {
          onEvent(event);
          if (event.type === 'error' || event.success === false) {
            const error = new Error(event.error || 'Kindle delivery failed');
            error.kindleEvent = event;
            throw error;
          }
          if (event.type === 'complete') return event;
        }
        if (data.status === 'complete') {
          return { type: 'complete', success: true, stage: 'Sent to Kindle', progress: 100 };
        }
        if (data.status === 'failed') {
          const error = new Error('Kindle delivery failed');
          error.definitive = true;
          throw error;
        }
        const elapsed = performance.now() - pollingStartedAt;
        const foregroundDelay = elapsed < 5000
          ? Math.min(180 + (idlePolls * 35), 320)
          : (elapsed < 20000 ? 420 : 700);
        await wait(document.hidden ? Math.max(1000, foregroundDelay) : foregroundDelay);
      } catch (error) {
        if (error.kindleEvent || error.definitive) throw error;
        failures += 1;
        onEvent({
          type: 'progress',
          stage: 'Reconnecting to delivery',
          progress: null,
          detail: 'The delivery continues while LibFlix restores its status connection.',
        });
        if (failures >= 12) {
          error.retryable = true;
          throw error;
        }
        await wait(Math.min(500 * failures, 4000));
      }
    }
  }

  function observeKindleJob(jobId, onEvent) {
    let active = activeJobPolls.get(jobId);
    if (!active) {
      active = { listeners: new Set(), lastEvent: null, promise: null };
      activeJobPolls.set(jobId, active);
      active.promise = pollKindleJob(jobId, event => {
        active.lastEvent = event;
        active.listeners.forEach(listener => {
          try { listener(event); } catch {}
        });
      }).finally(() => {
        if (activeJobPolls.get(jobId) === active) activeJobPolls.delete(jobId);
      });
    }
    active.listeners.add(onEvent);
    if (active.lastEvent) onEvent(active.lastEvent);
    return active.promise.finally(() => active.listeners.delete(onEvent));
  }

  function kindleJobStorageKey(md5) {
    return 'libflix.kindleJob.' + String(md5 || '').toLowerCase();
  }

  function storedKindleJobId(md5) {
    try {
      return window.sessionStorage.getItem(kindleJobStorageKey(md5)) || '';
    } catch {
      return '';
    }
  }

  function resumeKindleJobs(container) {
    container?.querySelectorAll('.edition-kindle[data-md5]').forEach(button => {
      const jobId = storedKindleJobId(button.dataset.md5);
      if (!jobId || button.dataset.resuming === 'true') return;
      button.dataset.resuming = 'true';
      deliverToKindle({ button, payload: { md5: button.dataset.md5 }, jobId })
        .catch(() => {})
        .finally(() => { delete button.dataset.resuming; });
    });
  }

  async function deliverToKindle({ button, payload, jobId = '' }) {
    if (!button) throw new Error('Kindle action is unavailable');
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    if (!button.dataset.originalAriaLabel) button.dataset.originalAriaLabel = button.getAttribute('aria-label') || 'Send to Kindle';

    const panel = createKindleProgress(button);
    const trayMetadata = {
      title: payload.canonical_title || payload.title || button.dataset.title || 'Book',
    };
    const initialEvent = { type: 'progress', stage: 'Preparing delivery', progress: 0 };
    updateKindleProgress(panel, initialEvent);
    updateGlobalKindleTray(initialEvent, trayMetadata);
    button.classList.remove('sent');
    button.classList.add('sending');
    button.setAttribute('aria-busy', 'true');
    button.setAttribute('aria-label', 'Sending book to Kindle');
    button.innerHTML = icons.send + '<span>Sending</span>';
    const deliveryStartedAt = performance.now();

    try {
      if (!jobId) {
        const response = await fetch('/api/kindle/jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const created = await response.json().catch(() => ({}));
        if (!response.ok || created.success === false || !created.job_id) {
          throw new Error(created.error || 'Kindle delivery could not be started');
        }
        jobId = created.job_id;
        rememberActiveKindleJob(payload.md5, jobId, payload);
      } else {
        const restoringEvent = { type: 'progress', stage: 'Restoring delivery status', progress: null };
        const stored = readActiveKindleJobs()[String(payload.md5 || '').toLowerCase()];
        if (stored?.title) trayMetadata.title = stored.title;
        if (!stored && payload.md5) rememberActiveKindleJob(payload.md5, jobId, trayMetadata);
        updateKindleProgress(panel, restoringEvent);
        updateGlobalKindleTray(restoringEvent, trayMetadata);
      }
      const completed = await observeKindleJob(jobId, event => {
        updateKindleProgress(panel, event);
        updateGlobalKindleTray(event, trayMetadata);
      });
      const cleanedTitle = completed.title || payload.canonical_title || payload.title || button.dataset.title || 'Book';
      const elapsedSeconds = Number(completed.elapsed_seconds) > 0
        ? Number(completed.elapsed_seconds)
        : (performance.now() - deliveryStartedAt) / 1000;
      button.classList.add('sent');
      button.innerHTML = icons.check + '<span>Sent</span>';
      button.setAttribute('aria-label', 'Sent to Kindle');
      updateGlobalKindleTray({ ...completed, type: 'complete', stage: completed.stage || 'Sent to Kindle', progress: 100 }, { title: cleanedTitle });
      forgetActiveKindleJob(payload.md5, jobId);
      window.LibFlixNotify?.('Sent to Kindle', 'success', {
        title: cleanedTitle,
        detail: 'Completed in ' + formatElapsedTime(elapsedSeconds),
      });
      return { ...completed, title: cleanedTitle, elapsed_seconds: elapsedSeconds };
    } catch (error) {
      const failure = error.kindleEvent || {};
      const failureEvent = {
        type: 'error',
        stage: failure.stage || (error.retryable ? 'Status connection interrupted' : 'Delivery failed'),
        progress: null,
        detail: error.retryable
          ? 'The delivery may still be running. Use Check status to reconnect.'
          : error.message,
      };
      updateKindleProgress(panel, failureEvent);
      updateGlobalKindleTray(failureEvent, trayMetadata);
      panel?.setAttribute('role', 'alert');
      if (error.retryable && jobId) {
        button.innerHTML = icons.send + '<span>Check status</span>';
        button.setAttribute('aria-label', 'Check Kindle delivery status');
      } else {
        button.innerHTML = button.dataset.originalHtml;
        button.setAttribute('aria-label', button.dataset.originalAriaLabel);
        if (payload.md5) forgetActiveKindleJob(payload.md5, jobId);
      }
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
    let html = '<button class="download-page-button' + (current <= 1 ? ' disabled' : '') + '" type="button" data-page="' + (current - 1) + '"' + (current <= 1 ? ' disabled aria-disabled="true"' : '') + '>Previous</button>';
    for (const value of pages) {
      if (last && value - last > 1) html += '<span class="download-page-button disabled" aria-hidden="true">…</span>';
      html += value === current
        ? '<span class="download-page-current" aria-current="page">' + value + '</span>'
        : '<button class="download-page-button" type="button" data-page="' + value + '">' + value + '</button>';
      last = value;
    }
    html += '<button class="download-page-button' + (current >= total ? ' disabled' : '') + '" type="button" data-page="' + (current + 1) + '"' + (current >= total ? ' disabled aria-disabled="true"' : '') + '>Next</button>';
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

  function restoreGlobalKindleJobs() {
    const jobs = readActiveKindleJobs();
    Object.entries(jobs).forEach(([md5, metadata]) => {
      if (!metadata?.jobId) return;
      const trayMetadata = { title: metadata.title || 'Book' };
      updateGlobalKindleTray(
        { type: 'progress', stage: 'Restoring delivery status', progress: null },
        trayMetadata,
      );
      observeKindleJob(metadata.jobId, event => updateGlobalKindleTray(event, trayMetadata))
        .then(() => forgetActiveKindleJob(md5, metadata.jobId))
        .catch(error => {
          const failure = error.kindleEvent || {};
          updateGlobalKindleTray({
            type: 'error',
            stage: failure.stage || (error.retryable ? 'Status connection interrupted' : 'Delivery failed'),
            progress: null,
            detail: error.retryable
              ? 'The delivery may still be running. Open this book and choose Check status.'
              : error.message,
          }, trayMetadata);
          if (!error.retryable) forgetActiveKindleJob(md5, metadata.jobId);
        });
    });
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
  restoreGlobalKindleJobs();
})();
