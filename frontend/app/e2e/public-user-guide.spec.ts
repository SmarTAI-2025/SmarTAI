import { expect, test } from '@playwright/test';

for (const language of ['zh', 'en'] as const) {
  test(`public ${language} manual preserves navigation, search, images and site destinations`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(locale => localStorage.setItem('smartai_locale', locale), language === 'zh' ? 'zh-CN' : 'en-US');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/');
    const homeBrand = await page.locator('.frontier-nav .frontier-wordmark-image').boundingBox();
    const guide = page.locator('.frontier-nav nav a', { hasText: language === 'zh' ? '用户手册' : 'User manual' });
    await expect(guide).toHaveAttribute('href', `/guide/?lang=${language}`);
    await guide.click();
    await expect(page.locator('body')).toHaveAttribute('data-language', language);
    await expect(page.locator('#quickstart')).toBeVisible();
    const guideBrand = await page.locator('.brand-image').boundingBox();
    expect(homeBrand).not.toBeNull();
    expect(guideBrand).not.toBeNull();
    expect(Math.abs(homeBrand!.x - guideBrand!.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(homeBrand!.y - guideBrand!.y)).toBeLessThanOrEqual(2);
    expect(Math.abs(homeBrand!.width - guideBrand!.width)).toBeLessThanOrEqual(1);
    await expect(page.locator('.topbar a[href="/login"]')).toHaveText(language === 'zh' ? '注册/登录' : 'Register / Sign in');
    await expect(page.locator('.topbar a[href="/frontier/enter"]')).toContainText(language === 'zh' ? '进入真实 Demo' : 'Enter live demo');

    // Check every chapter and both localized image collections, not just the first screen.
    const chapters = await page.locator('.chapter').evaluateAll(nodes => nodes.map(node => node.id));
    expect(chapters).toHaveLength(27);
    const missingAnchors = await page.locator('a[href^="#"]').evaluateAll(nodes => nodes.map(node => (node as HTMLAnchorElement).hash.slice(1)).filter(id => !document.getElementById(id)));
    expect(missingAnchors).toEqual([]);
    const imageFailures = await page.locator('.figure img').evaluateAll(async nodes => {
      const failures: string[] = [];
      for (const node of nodes) {
        const image = new Image(); image.src = (node as HTMLImageElement).src;
        try { await image.decode(); if (!image.naturalWidth) failures.push(image.src); }
        catch { failures.push(image.src); }
      }
      return failures;
    });
    expect(imageFailures).toEqual([]);
    await expect(page.locator('.figure img')).toHaveCount(35);
    for (const id of chapters) {
      await page.evaluate(chapter => { location.hash = chapter; }, id);
      await expect(page.locator(`article#${id}`)).toBeVisible();
      await expect(page.locator('.chapter:visible')).toHaveCount(1);
    }

    await page.locator('.book-tabs [data-book="manual"]').click();
    await expect(page.locator('.chapter[data-group="manual"]:visible')).toHaveCount(1);
    await page.locator('#search').fill('API');
    await expect(page.locator('#search-results')).toBeVisible();
    await expect(page.locator('.search-result')).not.toHaveCount(0);
    await page.locator('.search-result a').first().click();
    await expect(page.locator('.chapter:visible')).toHaveCount(1);
    await page.evaluate(() => { location.hash = 'questions-4'; });
    const note = page.locator('#questions-4 details');
    await expect(note).not.toHaveAttribute('open');
    await note.locator('summary').click();
    await expect(note.locator('div')).toContainText(language === 'zh' ? '消耗额度' : 'consuming quota');
    await page.locator('#language-toggle').click();
    const other = language === 'zh' ? 'en' : 'zh';
    await expect(page.locator('body')).toHaveAttribute('data-language', other);
    await expect(page).toHaveURL(new RegExp(`lang=${other}#questions-4$`));
    expect(await page.evaluate(() => localStorage.getItem('smartai_locale'))).toBe(other === 'en' ? 'en-US' : 'zh-CN');

    await page.evaluate(() => { location.hash = 'quickstart'; });
    await page.locator('#quickstart .zoom-image').first().click();
    await expect(page.locator('#lightbox')).toBeVisible();
    await page.locator('#lightbox-image').evaluate(image => (image as HTMLImageElement).decode());
    await page.keyboard.press('Escape');
    await expect(page.locator('#lightbox')).not.toBeVisible();
    await page.locator('#quickstart .figure img').first().evaluate(image => (image as HTMLImageElement).decode());
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: testInfo.outputPath(`manual-${other}-desktop.png`) });
    await page.evaluate(() => { location.hash = 'api-minimax'; });
    await page.locator('#api-minimax .page-foot').scrollIntoViewIfNeeded();
    await page.locator('.brand').click();
    await expect(page).toHaveURL('/');
    await expect(page.locator('.frontier-site')).toBeVisible();
    expect(errors).toEqual([]);
  });
}

for (const width of [320, 390, 820, 1024, 1920]) {
  test(`manual and landing navigation fit at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/guide/?lang=en#api-google');
    await expect(page.locator('body')).toHaveAttribute('data-language', 'en');
    await expect(page.locator('.chapter:visible')).toHaveCount(1);
    const clipped = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(clipped).toBe(false);
    const boxes = await page.locator('.topbar a, .topbar button').evaluateAll(nodes => nodes.filter(n => n.getBoundingClientRect().width > 0).map(n => {
      const r = n.getBoundingClientRect(); return { text: n.textContent, x: r.x, y: r.y, right: r.right, bottom: r.bottom };
    }));
    for (let i = 0; i < boxes.length; i++) {
      expect(boxes[i].x).toBeGreaterThanOrEqual(0);
      expect(boxes[i].right).toBeLessThanOrEqual(width);
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        expect(Math.min(a.right, b.right) - Math.max(a.x, b.x) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 1, `${a.text} overlaps ${b.text}`).toBe(false);
      }
    }
    if (width <= 700) {
      await page.locator('#menu').click();
      await expect(page.locator('#sidebar')).toBeInViewport();
      await page.locator('#search').fill('DeepSeek');
      await page.locator('#search').press('Enter');
      await page.locator('.search-result a').first().click();
      await expect(page.locator('body')).not.toHaveClass(/nav-open/);
    }
    await page.screenshot({ path: testInfo.outputPath(`manual-en-${width}.png`) });
    await page.locator('.topbar a[href="/login"]').click();
    await expect(page).toHaveURL('/login');
    await page.goto('/guide/?lang=en#api-relay');
    await page.locator('.topbar a[href="/frontier/enter"]').click();
    await expect(page).toHaveURL(/\/frontier\/enter|\/login/);
    await page.goto('/');
    await expect(page.getByRole('link', { name: 'User manual', exact: true }).filter({ visible: true })).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`landing-en-${width}.png`) });
  });
}
