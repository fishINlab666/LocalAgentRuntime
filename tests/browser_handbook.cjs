#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const FILE_NAME = "Local-Agent-Runtime-小白入门与开发防跑偏手册.html";
const PAGE_URL = `http://127.0.0.1:8765/${encodeURIComponent(FILE_NAME)}`;
const VIEWPORTS = [
  { name: "desktop-1440", width: 1440, height: 1000 },
  { name: "desktop-1024", width: 1024, height: 900 },
  { name: "tablet-768", width: 768, height: 1024 },
  { name: "mobile-375", width: 375, height: 812 },
];

async function assertNoHorizontalOverflow(page, label) {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  assert.ok(
    metrics.scrollWidth <= metrics.clientWidth + 1,
    `${label}: horizontal overflow ${metrics.scrollWidth} > ${metrics.clientWidth}`
  );
}

async function run() {
  const browser = await chromium.launch({
    headless: true,
    executablePath: CHROME,
  });
  const consoleErrors = [];

  try {
    for (const viewport of VIEWPORTS) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        reducedMotion: "no-preference",
      });
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error") {
          const location = message.location();
          consoleErrors.push(
            `${viewport.name}: ${message.text()} @ ${location.url || "unknown"}:${location.lineNumber ?? 0}`
          );
        }
      });
      page.on("response", (response) => {
        if (response.status() >= 400) {
          consoleErrors.push(
            `${viewport.name}: HTTP ${response.status()} ${response.url()}`
          );
        }
      });
      page.on("pageerror", (error) => {
        consoleErrors.push(`${viewport.name}: ${error.message}`);
      });

      await page.goto(PAGE_URL, { waitUntil: "networkidle" });
      await page.locator("h1").waitFor({ state: "visible" });
      assert.equal(await page.locator("h1").count(), 1, `${viewport.name}: h1 count`);
      await assertNoHorizontalOverflow(page, viewport.name);

      if (viewport.width > 900) {
        await assert.doesNotReject(async () => {
          await page.locator("#sidebar").waitFor({ state: "visible" });
        });
        const layout = await page.evaluate(() => {
          const sidebar = document.getElementById("sidebar").getBoundingClientRect();
          const main = document.getElementById("main-content").getBoundingClientRect();
          return { sidebarRight: sidebar.right, mainLeft: main.left };
        });
        assert.ok(
          layout.mainLeft >= layout.sidebarRight - 1,
          `${viewport.name}: sidebar overlaps main content`
        );
      } else {
        assert.equal(
          await page.locator("#sidebar").getAttribute("aria-hidden"),
          "true",
          `${viewport.name}: closed drawer aria state`
        );
        await page.locator("#mobile-nav-toggle").click();
        assert.equal(
          await page.locator("#mobile-nav-toggle").getAttribute("aria-expanded"),
          "true",
          `${viewport.name}: drawer opens`
        );
        await page.waitForFunction(
          () => document.activeElement && document.activeElement.id === "mobile-nav-close"
        );
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "mobile-nav-close",
          `${viewport.name}: close button receives focus`
        );
        await page.keyboard.press("Escape");
        assert.equal(
          await page.locator("#mobile-nav-toggle").getAttribute("aria-expanded"),
          "false",
          `${viewport.name}: Escape closes drawer`
        );
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "mobile-nav-toggle",
          `${viewport.name}: focus returns to drawer trigger`
        );

        await page.locator("#mobile-nav-toggle").click();
        await page.locator('.toc a[href="#first-slice"]').click();
        await page.waitForFunction(
          () => document.activeElement && document.activeElement.id === "first-slice"
        );
        assert.equal(
          await page.locator("#mobile-nav-toggle").getAttribute("aria-expanded"),
          "false",
          `${viewport.name}: chapter link closes drawer`
        );
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "first-slice",
          `${viewport.name}: chapter link moves focus to target`
        );
      }

      if (viewport.name === "desktop-1440") {
        await page.screenshot({ path: "/tmp/local-agent-handbook-1440.png", fullPage: false });

        const search = page.locator("#handbook-search");
        await search.fill("Cron");
        await page.waitForFunction(() =>
          /找到 \d+ 个相关章节/.test(document.getElementById("search-status").textContent || "")
        );
        assert.match(
          await page.locator("#search-status").innerText(),
          /找到 \d+ 个相关章节/,
          "desktop-1440: search reports matches"
        );
        const searchState = await page.evaluate(() => ({
          visible: document.querySelectorAll("[data-searchable]:not(.search-hidden)").length,
          hidden: document.querySelectorAll("[data-searchable].search-hidden").length,
        }));
        assert.ok(searchState.visible > 0 && searchState.hidden > 0, "search filters sections");

        await search.fill("完全不存在的词XYZ987");
        await page.waitForFunction(() =>
          /没有匹配章节/.test(document.getElementById("search-status").textContent || "")
        );
        assert.match(
          await page.locator("#search-status").innerText(),
          /没有匹配章节/,
          "desktop-1440: no-result recovery message"
        );
        await page.locator("#clear-search").click();
        assert.equal(
          await page.locator("[data-searchable].search-hidden").count(),
          0,
          "desktop-1440: clear restores all sections"
        );

        await page.locator('.toc a[href="#first-slice"]').click();
        await page.waitForFunction(() => window.location.hash === "#first-slice");
        assert.equal(new URL(page.url()).hash, "#first-slice", "anchor navigation updates hash");

        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        await page.waitForFunction(() =>
          document.getElementById("back-to-top").classList.contains("is-visible")
        );
        assert.ok(
          await page.locator("#back-to-top").evaluate((node) => node.classList.contains("is-visible")),
          "desktop-1440: back-to-top becomes visible"
        );
        assert.notEqual(
          await page.locator("#reading-progress").evaluate((node) => node.style.transform),
          "scaleX(0)",
          "desktop-1440: reading progress advances"
        );

        await page.emulateMedia({ media: "print" });
        assert.equal(
          await page.locator("#sidebar").evaluate((node) => getComputedStyle(node).display),
          "none",
          "print hides sidebar"
        );
        assert.equal(
          await page.locator("#back-to-top").evaluate((node) => getComputedStyle(node).display),
          "none",
          "print hides floating control"
        );
      }

      if (viewport.name === "mobile-375") {
        await page.screenshot({ path: "/tmp/local-agent-handbook-375.png", fullPage: false });
      }

      await context.close();
    }

    const reducedContext = await browser.newContext({
      viewport: { width: 375, height: 812 },
      reducedMotion: "reduce",
    });
    const reducedPage = await reducedContext.newPage();
    await reducedPage.goto(PAGE_URL, { waitUntil: "networkidle" });
    assert.equal(
      await reducedPage.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior),
      "auto",
      "reduced motion disables smooth scrolling"
    );
    await reducedContext.close();

    assert.deepEqual(consoleErrors, [], `browser console errors:\n${consoleErrors.join("\n")}`);
    console.log("PASS: browser handbook checks succeeded");
    console.log("screenshots: /tmp/local-agent-handbook-1440.png, /tmp/local-agent-handbook-375.png");
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(`FAIL: ${error.stack || error.message}`);
  process.exit(1);
});
