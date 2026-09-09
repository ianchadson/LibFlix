// Run with an isolated headless playwright-cli session on a local /preview page:
// playwright-cli -s=books-ux run-code --filename=tests/browser/apple_books.js
// Only synthetic edition metadata is used; no EPUB or native app is opened.
async page => {
  const origin = await page.evaluate(() => location.origin);
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const books = [{ title: 'The Quiet Hours', author: 'Alex Morgan', ext: 'epub',
    md5: 'a'.repeat(32), size: '1.2 MB', language: 'English', year: '2026', best_match: true }];
  await page.route('**/api/search?**', route => route.fulfill({ json: {
    success: true, books, total: 1, total_pages: 1, page: 1,
  }}));
  await page.goto(origin + '/preview?title=The+Quiet+Hours&author=Alex+Morgan');
  const renderFixture = () => page.evaluate(books => {
    const container = document.getElementById('resultsBody');
    window.LibFlixDownloads.renderEditions(container, books);
    container.hidden = false;
    document.getElementById('downloadEmpty').hidden = true;
  }, books);
  await renderFixture();
  await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); });
  await page.locator('.edition-apple-books').waitFor();
  const bookButton = page.locator('.edition-apple-books');
  const handoff = await bookButton.evaluate(button => {
    const url = new URL(button.href);
    return { protocol: url.protocol, input: url.searchParams.get('text') };
  });
  assert(handoff.protocol === 'shortcuts:', 'Must hand off to Shortcuts');
  assert(handoff.input.includes('/download/' + 'a'.repeat(32)), 'Selected EPUB must be the input');
  // Replace only the native navigation target so this test cannot launch a Mac app.
  await bookButton.evaluate(button => button.href = '#books-handoff-test');
  await bookButton.click();
  const dialog = page.locator('#appleBooksSetup');
  assert(await dialog.isVisible(), 'First use must show setup');
  assert(await page.locator('#appleBooksSetupTitle').textContent() === 'Set up  Books', 'Concise setup title');
  assert(await page.locator('#appleBooksSetupIntro').isHidden(), 'Instructions are deferred');
  assert(await page.locator('[data-apple-books-selected]').count() === 0, 'No redundant book-title box');
  assert(await page.locator('[data-apple-books-progress]').count() === 0, 'No redundant setup label');
  assert(await page.locator('[data-apple-books-fallback]').isHidden(), 'Only two initial actions');
  assert((await page.locator('[data-apple-books-fallback]').getAttribute('href')).includes('/download/'), 'Offer EPUB fallback');
  const widths = [320, 390, 768, 1280];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 844 });
    const box = await page.locator('.apple-books-setup-card').boundingBox();
    assert(box.x >= 0 && box.x + box.width <= width, 'Sheet must fit ' + width);
    assert(Math.abs(box.x - (width - box.x - box.width)) < 1, 'Sheet must be centered');
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.LibFlixNotify?.('Test background notification'));
  const frontmost = await page.locator('[data-apple-books-intro] [data-apple-books-install]').evaluate(button => {
    const box = button.getBoundingClientRect();
    return button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
  });
  assert(frontmost, 'Background notifications must not cover the setup controls');
  await page.screenshot({ path: 'output/playwright/apple-books-setup.png' });
  await page.locator('[data-apple-books-intro] [data-apple-books-ready]').focus();
  await page.keyboard.press('Tab');
  assert(await page.locator('[data-apple-books-close]').evaluate(button => button === document.activeElement), 'Trap focus');
  await page.keyboard.press('Escape');
  assert(await dialog.isHidden(), 'Escape closes sheet');
  assert(await bookButton.evaluate(button => button === document.activeElement), 'Restore triggering focus');
  await bookButton.click();
  for (const [status, body] of [[404, 'Shortcut unavailable'], [200, '<html>Not a shortcut</html>']]) {
    await page.route('**/apple-books-shortcut', route => route.fulfill({ status, body }));
    await page.locator('[data-apple-books-intro] [data-apple-books-install]').click();
    await page.locator('[data-apple-books-error]:visible').waitFor();
    assert(await dialog.getAttribute('data-stage') === 'intro', 'Failed/invalid file must not advance');
    assert(await page.locator('[data-apple-books-fallback]').isVisible(), 'Offer fallback when needed');
    assert(await page.locator('[data-apple-books-intro] [data-apple-books-install]').isEnabled(), 'Retry stays enabled');
    await page.unroute('**/apple-books-shortcut');
  }
  await page.screenshot({ path: 'output/playwright/apple-books-error.png' });
  const downloadPromise = page.waitForEvent('download');
  await page.locator('[data-apple-books-intro] [data-apple-books-install]').click();
  const download = await downloadPromise;
  assert(download.suggestedFilename() === 'LibFlix to Books.shortcut', 'Correct installer filename');
  await page.locator('[data-apple-books-finish]:visible').waitFor();
  assert(await page.locator('#appleBooksSetupIntro').isVisible(), 'Instructions appear after getting shortcut');
  assert(await page.evaluate(() => !localStorage.getItem('libflix.appleBooksShortcutReady')), 'Downloading is not installation');
  await page.screenshot({ path: 'output/playwright/apple-books-finish.png' });
  await page.locator('[data-apple-books-finish] [data-apple-books-ready]').click();
  assert(await dialog.isHidden(), 'Confirmed setup closes');
  assert(await bookButton.getAttribute('aria-busy') === 'true', 'Show handoff progress');
  assert(await bookButton.getAttribute('aria-disabled') === 'true', 'Prevent repeat launches');
  assert(await page.evaluate(() => localStorage.getItem('libflix.appleBooksShortcutReady')) === '1', 'Remember confirmed setup');
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  assert(await bookButton.getAttribute('aria-busy') === null, 'Reset on return');
  await page.locator('.apple-books-recovery button').click();
  assert(await dialog.getAttribute('data-stage') === 'intro', 'Repair can reopen setup');
  await page.keyboard.press('Escape');
  await bookButton.click();
  assert(await dialog.isHidden(), 'Returning users bypass onboarding');
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  assert(await page.locator('.apple-books-recovery').count() === 1, 'Recovery must not duplicate');
  await page.reload();
  await renderFixture();
  await page.locator('.edition-apple-books').waitFor();
  await bookButton.evaluate(button => button.href = '#books-handoff-test');
  await bookButton.click();
  assert(await page.locator('#appleBooksSetup').count() === 0, 'Remember setup across reloads');
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  return { passed: ['selected EPUB', 'four viewport widths', 'notification layering', 'keyboard dismissal and focus',
    '404 and invalid installer recovery', 'signed installer download', 'confirmation-only persistence',
    'loading and return reset', 'repair setup', 'remembered setup across reload'], nativeImport: 'Not tested' };
}
