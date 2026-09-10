#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const FILE_NAME = "Local-Agent-Runtime-小白入门与开发防跑偏手册.html";
const PAGE_URL = pathToFileURL(path.resolve(__dirname, "..", FILE_NAME)).href;
const ARTIFACTS = path.resolve(__dirname, "..", "artifacts", "handbook-deep-2026-09-10");
const DEEP_LESSONS = [
  "session-memory", "context-compression", "long-term-memory", "tool-runtime",
  "completion-evaluation", "provider-models", "extensions",
];
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

async function checkDeepLessons(page, label) {
  for (const id of DEEP_LESSONS) {
    const chapter = page.locator(`#${id}`);
    const answer = chapter.locator(":scope > .answer-key");
    const questions = chapter.locator(":scope > .self-check > ol > li");
    assert.equal(await answer.count(), 1, `${label} ${id}: answer group`);
    assert.equal(await answer.getAttribute("open"), null, `${label} ${id}: initially folded`);
    assert.equal(await answer.locator("ol > li").count(), await questions.count(), `${label} ${id}: answer per question`);
    for (const item of await answer.locator("ol > li").all()) {
      assert.ok((await item.textContent()).trim().length > 30, `${label} ${id}: explanation, not just a verdict`);
    }
    const firstLink = chapter.locator(".lesson-nav a").first();
    const href = await firstLink.getAttribute("href");
    await firstLink.click();
    await page.waitForFunction((target) => {
      const top = document.querySelector(target).getBoundingClientRect().top;
      return location.hash === target && top > 60 && top < 200;
    }, href);
    await assertNoHorizontalOverflow(page, `${label} ${id}`);
    for (const example of await chapter.locator("pre.lesson-example").all()) {
      assert.ok(await example.evaluate((node) => node.scrollWidth <= node.clientWidth + 1), `${label} ${id}: teaching examples wrap without sideways reading`);
    }
    if (id === "session-memory") {
      await page.screenshot({ path: path.join(ARTIFACTS, `${label}-session-story.png`) });
    }
    await answer.locator("summary").click();
    assert.ok(await answer.evaluate((node) => node.open), `${label} ${id}: answers open`);
    if (id === "context-compression") {
      await page.screenshot({ path: path.join(ARTIFACTS, `${label}-compression-answers.png`) });
    }
    await assertNoHorizontalOverflow(page, `${label} ${id} expanded`);
    await answer.locator("summary").click();
  }
  await page.locator('#context-compression .lesson-nav a[href="#compression-good-summary"]').click();
  await page.waitForFunction(() => {
    const top = document.getElementById("compression-good-summary").getBoundingClientRect().top;
    return top > 60 && top < 200;
  });
  await page.screenshot({ path: path.join(ARTIFACTS, `${label}-compression-example.png`) });
}

async function run() {
  fs.mkdirSync(ARTIFACTS, { recursive: true });
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

        await page.screenshot({ path: path.join(ARTIFACTS, "desktop-cover.png") });
        for (const id of ["project", "model-to-agent", "context", "tool", "agent-loop", "runtime", "first-slice", "guardrails"]) {
          const answer = page.locator(`#${id} > .answer-key`);
          assert.equal(await answer.count(), 1, `${id}: has one answer group`);
          assert.equal(await answer.getAttribute("open"), null, `${id}: answers start collapsed`);
          assert.equal(await answer.locator("ol > li").count(), await page.locator(`#${id} > .self-check > ol > li`).count(), `${id}: one explanation per question`);
        }
        await page.locator('.toc a[href="#runtime"]').click();
        await page.locator('#runtime .answer-key summary').click();
        assert.ok(await page.locator('#runtime .answer-key').evaluate((node) => node.open));
        await page.screenshot({ path: path.join(ARTIFACTS, "desktop-runtime-answer.png") });
        await page.locator('#runtime .answer-key summary').click();
        await page.locator('#runtime .lesson-nav a[href="#runtime-story"]').click();
        await page.waitForFunction(() => window.location.hash === "#runtime-story");
        await page.waitForFunction(() => Math.abs(document.getElementById("runtime-story").getBoundingClientRect().top - 120) < 40);
        await page.screenshot({ path: path.join(ARTIFACTS, "desktop-runtime-story.png") });

        await checkDeepLessons(page, "desktop");
        const search = page.locator("#handbook-search");
        await search.fill("RunContext");
        await page.waitForFunction(() => document.getElementById("first-slice").classList.contains("search-hidden"));
        await page.locator('#runtime .chapter-pager a[href="#first-slice"]').click();
        assert.equal(await page.locator('#first-slice').evaluate((node) => node.classList.contains('search-hidden')), false, "cross-chapter link reveals a search-hidden target");
        assert.equal(await search.inputValue(), "", "cross-chapter recovery clears filtering");
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

        await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
        await page.emulateMedia({ media: "print" });
        assert.ok(await page.locator("#tool").isVisible(), "print retains chapters hidden by search");
        assert.equal(await page.locator("details:not([open])").count(), 0, "printing expands explanations");
        await page.emulateMedia({ media: "screen" });
        await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));
        assert.equal(await page.locator(".answer-key[open]").count(), 0, "print restores answer fold state");
        assert.equal(await search.inputValue(), "Cron", "print preserves search query");

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
        await page.locator("#mobile-nav-toggle").click();
        await page.waitForFunction(() => document.activeElement.id === "mobile-nav-close");
        await page.keyboard.press("Shift+Tab");
        assert.ok(await page.evaluate(() => document.getElementById("sidebar").contains(document.activeElement)), "mobile focus stays in drawer");
        await page.keyboard.press("Tab");
        assert.equal(await page.evaluate(() => document.activeElement.id), "mobile-nav-close", "mobile focus cycles to first control");
        await page.locator('.toc a[href="#runtime"]').click();
        await page.locator('#runtime .lesson-nav a[href="#runtime-story"]').click();
        await page.waitForFunction(() => {
          const top = document.getElementById("runtime-story").getBoundingClientRect().top;
          return top > 60 && top < 200;
        });
        await assertNoHorizontalOverflow(page, "mobile runtime story");
        await page.screenshot({ path: path.join(ARTIFACTS, "mobile-runtime-story.png") });
        await page.locator('#runtime .answer-key summary').click();
        await page.screenshot({ path: path.join(ARTIFACTS, "mobile-runtime-answer.png") });
        await checkDeepLessons(page, "mobile");
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

    const offlineContext = await browser.newContext({ viewport: { width: 375, height: 812 }, javaScriptEnabled: false });
    const offlinePage = await offlineContext.newPage();
    await offlinePage.goto(PAGE_URL);
    await assertNoHorizontalOverflow(offlinePage, "no-script mobile");
    assert.ok(await offlinePage.locator('.toc a[href="#runtime"]').isVisible(), "mobile navigation works without JavaScript");
    await offlinePage.locator('.toc a[href="#runtime"]').click();
    assert.equal(new URL(offlinePage.url()).hash, "#runtime");
    await offlineContext.close();

    assert.deepEqual(consoleErrors, [], `browser console errors:\n${consoleErrors.join("\n")}`);
    console.log("PASS: browser handbook checks succeeded");
    console.log(`screenshots: ${ARTIFACTS}`);
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(`FAIL: ${error.stack || error.message}`);
  process.exit(1);
});
