// Run with an isolated headless playwright-cli session on a local /preview page:
// playwright-cli -s=pdf-epub run-code --filename=tests/browser/pdf_epub.js
// Synthetic responses exercise the UI; test_pdf_epub.py verifies real EPUB bytes.
async page => {
  const origin = await page.evaluate(() => location.origin);
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const md5 = 'b'.repeat(32);
  const books = [{ md5, ext: 'pdf', title: 'The Quiet Hours', author: 'Alex Morgan',
    kindle_compatible: true, kindle_conversion: true, best_match: true, size: '1 MB' }];
  await page.route('**/api/search?**', route => route.fulfill({ json: {
    success: true, books, total: 1, total_pages: 1, page: 1,
  } }));
  await page.route('**/api/download/prepare/**', route => route.fulfill({
    contentType: 'application/x-ndjson',
    body: [
      { type: 'progress', stage: 'Converting to EPUB', progress: 66 },
      { type: 'complete', success: true, stage: 'Download ready', progress: 100 },
    ].map(event => JSON.stringify(event) + '\n').join(''),
  }));
  await page.route('**/download/' + md5 + '?**', route => route.fulfill({
    contentType: 'application/epub+zip', body: 'Synthetic UI download fixture',
    headers: { 'Content-Disposition': 'attachment; filename="The Quiet Hours.epub"' },
  }));
  await page.goto(origin + '/preview?title=The+Quiet+Hours&author=Alex+Morgan');
  await page.evaluate(books => {
    const container = document.getElementById('resultsBody');
    window.LibFlixDownloads.renderEditions(container, books);
    container.hidden = false;
    document.getElementById('downloadEmpty').hidden = true;
  }, books);
  await page.locator('.edition-epub-convert').waitFor();
  assert(await page.locator('.edition-download').innerText() === 'PDF', 'Keep original PDF');
  assert(await page.locator('.edition-epub-convert').innerText() === 'EPUB', 'Offer converted EPUB');
  assert(await page.locator('.edition-kindle').innerText() === 'Kindle', 'PDF conversion is automatic');
  assert(await page.locator('select.edition-kindle-format').count() === 0, 'Integrate format menu into Kindle button');
  await page.evaluate(() => {
    window.sendToKindle = (md5, title, extension, button, edition) => { window.testPdfDelivery = edition.pdfDelivery; };
  });
  await page.locator('.edition-kindle-toggle').click();
  assert(await page.getByRole('menuitemradio', { name: 'Convert to EPUB', exact: true }).getAttribute('aria-checked') === 'true', 'EPUB checked by default');
  await page.getByRole('menuitemradio', { name: 'Original PDF', exact: true }).click();
  assert(await page.evaluate(() => window.testPdfDelivery) === undefined, 'Changing format does not send');
  await page.locator('.edition-kindle').click();
  assert(await page.evaluate(() => window.testPdfDelivery) === 'original', 'Main action sends selected PDF format');
  await page.locator('.edition-kindle-toggle').click();
  assert(await page.getByRole('menuitemradio', { name: 'Original PDF', exact: true }).getAttribute('aria-checked') === 'true', 'Keep selection after send');
  await page.getByRole('menuitemradio', { name: 'Convert to EPUB', exact: true }).click();
  await page.locator('.edition-kindle').click();
  assert(await page.evaluate(() => window.testPdfDelivery) === 'epub', 'Can switch back to EPUB');
  for (const width of [320, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    const box = await page.locator('.edition-actions').boundingBox();
    assert(box.x >= 0 && box.x + box.width <= width, 'Actions must fit ' + width);
  }
  const downloaded = page.waitForEvent('download');
  await page.locator('.edition-epub-convert').click();
  assert((await downloaded).suggestedFilename() === 'The Quiet Hours.epub', 'Use EPUB attachment');
  assert(await page.locator('.kindle-progress-value').innerText() === 'Ready', 'Show ready, not sent');
  await page.unroute('**/api/download/prepare/**');
  await page.route('**/api/download/prepare/**', route => route.fulfill({
    contentType: 'application/x-ndjson',
    body: JSON.stringify({ type: 'error', success: false, stage: 'Download unavailable', error: 'Password-protected PDF' }) + '\n',
  }));
  await page.locator('.edition-epub-convert').click();
  await page.locator('.kindle-progress.error').waitFor();
  assert((await page.locator('.kindle-progress-detail').innerText()).includes('Password-protected PDF'), 'Explain conversion failure');
  assert(await page.locator('.edition-download').isVisible(), 'Original PDF stays available');
  assert(!await page.locator('.edition-epub-convert').getAttribute('aria-busy'), 'Allow retry');
};
