(() => {
  'use strict';

  const script = document.currentScript;
  const version = String(script?.dataset.version || '1');
  const withVersion = rawUrl => {
    const url = new URL(rawUrl, window.location.origin);
    url.searchParams.set('v', version);
    return url.pathname + url.search;
  };

  const installHeadMetadata = () => {
    const viewport = document.head.querySelector('meta[name="viewport"]');
    if (viewport && !viewport.content.includes('viewport-fit=')) {
      viewport.content += ', viewport-fit=cover';
    }
    if (!document.querySelector('link[rel="manifest"]')) {
      const manifest = document.createElement('link');
      manifest.rel = 'manifest';
      manifest.href = withVersion(script.dataset.manifest);
      document.head.appendChild(manifest);
    }
    if (!document.querySelector('link[rel="apple-touch-icon"]')) {
      const icon = document.createElement('link');
      icon.rel = 'apple-touch-icon';
      icon.href = withVersion(script.dataset.icon192);
      document.head.appendChild(icon);
    }
    const metaValues = {
      'theme-color': '#0d0e10',
      'apple-mobile-web-app-capable': 'yes',
      'apple-mobile-web-app-status-bar-style': 'black-translucent',
      'apple-mobile-web-app-title': 'LibFlix',
    };
    Object.entries(metaValues).forEach(([name, content]) => {
      let meta = document.head.querySelector(`meta[name="${name}"]`);
      if (!meta) {
        meta = document.createElement('meta');
        meta.name = name;
        document.head.appendChild(meta);
      }
      meta.content = content;
    });
  };

  installHeadMetadata();

  const notify = message => {
    if (typeof window.LibFlixNotify === 'function') window.LibFlixNotify(message);
  };

  const registerServiceWorker = () => {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    const workerUrl = withVersion(script.dataset.worker);
    navigator.serviceWorker.register(workerUrl, { scope: '/', updateViaCache: 'none' })
      .then(registration => {
        if (registration.waiting && navigator.serviceWorker.controller) {
          notify('A LibFlix update is ready and will apply after you close the app.');
        }
        registration.addEventListener('updatefound', () => {
          const installing = registration.installing;
          installing?.addEventListener('statechange', () => {
            if (installing.state === 'installed' && navigator.serviceWorker.controller) {
              notify('A LibFlix update is ready and will apply after you close the app.');
            }
          });
        });
      })
      .catch(error => console.warn('LibFlix offline support is unavailable:', error.message));
  };

  const isStandalone = () => (
    window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true
  );
  let deferredInstallPrompt = null;

  const setupInstallAction = () => {
    const group = document.getElementById('installAppGroup');
    const button = document.getElementById('installAppButton');
    const state = document.getElementById('installAppState');
    if (!group || !button || isStandalone()) return;
    const isIos = (
      /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
    );
    if (isIos) {
      group.hidden = false;
      if (state) state.textContent = 'Add to Home Screen';
      button.addEventListener('click', () => notify('In Safari, tap Share, then Add to Home Screen.'));
    }
    window.addEventListener('beforeinstallprompt', event => {
      event.preventDefault();
      deferredInstallPrompt = event;
      group.hidden = false;
      if (state) state.textContent = 'Ready to install';
    });
    button.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      button.disabled = true;
      deferredInstallPrompt.prompt();
      const choice = await deferredInstallPrompt.userChoice.catch(() => null);
      deferredInstallPrompt = null;
      button.disabled = false;
      if (choice?.outcome === 'accepted') group.hidden = true;
    });
    window.addEventListener('appinstalled', () => {
      deferredInstallPrompt = null;
      group.hidden = true;
      notify('LibFlix was added to your device.');
    });
  };

  const setupMobileNavigation = () => {
    const nav = document.getElementById('mobileAppNav');
    const home = document.getElementById('mobileNavHome');
    const search = document.getElementById('mobileNavSearch');
    const browse = document.getElementById('mobileNavBrowse');
    const settings = document.getElementById('mobileNavSettings');
    const browseSheet = document.getElementById('mobileBrowseSheet');
    const browseLinks = document.getElementById('mobileBrowseLinks');
    const browseClose = document.getElementById('mobileBrowseClose');
    const searchForm = document.querySelector('.search-bar');
    const searchSubmit = searchForm?.querySelector('.search-submit');
    const settingsToggle = document.getElementById('navSettingsToggle');
    const settingsPanel = document.getElementById('navSettingsPanel');
    if (!nav || !home || !search || !browse || !settings || !browseSheet || !browseLinks) return;

    document.documentElement.classList.add('pwa-mobile-nav-ready');

    const setBrowseOpen = (open, restoreFocus = true) => {
      browseSheet.hidden = !open;
      browse.setAttribute('aria-expanded', String(open));
      browse.classList.toggle('sheet-open', open);
      if (open) requestAnimationFrame(() => browseLinks.querySelector('a')?.focus({ preventScroll: true }));
      else if (restoreFocus) browse.focus({ preventScroll: true });
    };

    const renderBrowseLinks = () => {
      const seen = new Set();
      const fragment = document.createDocumentFragment();
      document.querySelectorAll('.cat-tabs a').forEach(source => {
        const url = new URL(source.href, window.location.href);
        const label = source.textContent.trim();
        if (!label || label.toLowerCase() === 'home') return;
        const key = url.pathname + url.search;
        if (seen.has(key)) return;
        seen.add(key);
        const isCurrent = source.matches('.active, [aria-current="page"]');
        const link = source.cloneNode(true);
        link.className = 'mobile-browse-link';
        link.classList.toggle('active', isCurrent);
        if (isCurrent) {
          link.setAttribute('aria-current', 'page');
          const currentMarker = document.createElement('span');
          currentMarker.className = 'mobile-browse-current';
          currentMarker.setAttribute('aria-hidden', 'true');
          currentMarker.textContent = '✓';
          link.appendChild(currentMarker);
        } else {
          link.removeAttribute('aria-current');
        }
        link.removeAttribute('id');
        fragment.appendChild(link);
      });
      browseLinks.replaceChildren(fragment);
    };

    const syncActiveItem = () => {
      const path = location.pathname.replace(/\/+$/, '') || '/';
      const homePaths = new Set(['/', '/fiction', '/cn', '/fiction/cn']);
      const isSearch = /\/(?:discover|search)$/.test(path);
      const isBrowse = /\/category\//.test(path) || /\/topics$/.test(path);
      const currentHome = document.querySelector('.navbar-brand')?.href;
      if (currentHome) home.href = currentHome;
      [home, search, browse].forEach(item => {
        item.classList.remove('active');
        item.removeAttribute('aria-current');
      });
      const active = homePaths.has(path) ? home : (isSearch ? search : (isBrowse ? browse : null));
      if (active) {
        active.classList.add('active');
        active.setAttribute('aria-current', 'page');
      }
      renderBrowseLinks();
      setBrowseOpen(false, false);
    };

    browse.addEventListener('click', event => {
      event.stopPropagation();
      const shouldOpen = browseSheet.hidden;
      window.LibFlixNavUI?.setSearchOpen(false);
      window.LibFlixNavUI?.setSettingsOpen(false);
      setBrowseOpen(shouldOpen);
    });
    browseClose?.addEventListener('click', () => setBrowseOpen(false));
    browseLinks.addEventListener('click', event => {
      if (event.target.closest('a')) setBrowseOpen(false, false);
    });
    search.addEventListener('click', event => {
      event.stopPropagation();
      setBrowseOpen(false, false);
      window.LibFlixNavUI?.setSettingsOpen(false);
      searchSubmit?.click();
    });
    settings.addEventListener('click', event => {
      event.stopPropagation();
      const shouldOpen = !settingsPanel?.classList.contains('open');
      setBrowseOpen(false, false);
      window.LibFlixNavUI?.setSearchOpen(false);
      if (shouldOpen) settingsToggle?.click();
      else window.LibFlixNavUI?.setSettingsOpen(false);
    });
    document.addEventListener('click', event => {
      if (!browseSheet.hidden && !browseSheet.contains(event.target) && !browse.contains(event.target)) {
        setBrowseOpen(false, false);
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !browseSheet.hidden) setBrowseOpen(false);
    });
    new MutationObserver(() => {
      settings.classList.toggle('active', settingsPanel?.classList.contains('open'));
      settings.setAttribute('aria-expanded', String(Boolean(settingsPanel?.classList.contains('open'))));
    }).observe(settingsPanel, { attributes: true, attributeFilter: ['class'] });
    new MutationObserver(() => {
      search.classList.toggle('active', searchForm?.classList.contains('open'));
      search.setAttribute('aria-expanded', String(Boolean(searchForm?.classList.contains('open'))));
    }).observe(searchForm, { attributes: true, attributeFilter: ['class'] });
    window.addEventListener('libflix:navigated', syncActiveItem);
    syncActiveItem();
  };

  const start = () => {
    setupInstallAction();
    setupMobileNavigation();
    registerServiceWorker();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
