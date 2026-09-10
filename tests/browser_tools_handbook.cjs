#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const URL = pathToFileURL(path.join(ROOT, "Agent-Tools-工具调用层学习与开发手册.html")).href;
const ARTIFACTS = path.join(ROOT, "artifacts", "tools-handbook-2026-09-10");
const LESSONS = ["why-tools", "one-task", "model-input", "tool-contract", "three-roles", "five-gates", "results", "approval", "first-unit", "guardrails"];
const VIEWPORTS = [[1440, 1000], [1024, 900], [768, 1024], [375, 812]];

async function noOverflow(page, label) {
  const size = await page.evaluate(() => ({
    actual: document.documentElement.scrollWidth,
    available: document.documentElement.clientWidth,
  }));
  assert.ok(size.actual <= size.available + 1, label + ": page overflows " + JSON.stringify(size));
}

async function jump(page, id) {
  await page.evaluate((target) => {
    const node = document.getElementById(target);
    const link = document.querySelector('a[href="#' + target + '"]');
    if (link) link.click();
    else { location.hash = target; node.scrollIntoView({ behavior: "instant" }); }
  }, id);
  await page.waitForFunction((target) => {
    const rect = document.getElementById(target).getBoundingClientRect();
    return rect.top >= 0 && rect.top < 240;
  }, id);
}

async function run() {
  fs.mkdirSync(ARTIFACTS, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const errors = [];
  const requests = [];
  try {
    for (const [width, height] of VIEWPORTS) {
      const context = await browser.newContext({ viewport: { width, height }, offline: true, reducedMotion: "reduce" });
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      page.on("request", (request) => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
      await page.goto(URL, { waitUntil: "networkidle" });
      assert.equal(await page.locator("h1").count(), 1);
      await noOverflow(page, "cover " + width);
      if (width > 900) {
        assert.ok(await page.evaluate(() =>
          document.getElementById("main-content").getBoundingClientRect().left >=
          document.getElementById("sidebar").getBoundingClientRect().right - 1), "fixed nav overlaps body");
      } else {
        assert.equal(await page.locator("#sidebar").getAttribute("aria-hidden"), "true");
        await page.locator("#mobile-nav-toggle").click();
        await page.waitForFunction(() => document.activeElement.id === "mobile-nav-close");
        await page.keyboard.press("Shift+Tab");
        assert.ok(await page.evaluate(() => document.getElementById("sidebar").contains(document.activeElement)));
        await page.keyboard.press("Tab");
        assert.equal(await page.evaluate(() => document.activeElement.id), "mobile-nav-close");
        await page.keyboard.press("Escape");
        assert.equal(await page.locator("#mobile-nav-toggle").getAttribute("aria-expanded"), "false");
        assert.equal(await page.evaluate(() => document.activeElement.id), "mobile-nav-toggle");
      }
      if (width === 1440) {
        await page.screenshot({ path: path.join(ARTIFACTS, "desktop-cover.png") });
        for (const id of LESSONS) {
          const questions = page.locator("#" + id + " .self-check > ol > li");
          const answers = page.locator("#" + id + " .answer-key > ol > li");
          assert.equal(await questions.count(), await answers.count(), id + ": question/answer mismatch");
          assert.ok(await questions.count() >= 3);
          const key = page.locator("#" + id + " .answer-key");
          assert.equal(await key.getAttribute("open"), null);
          await key.locator("summary").click();
          assert.ok(await key.evaluate((node) => node.open));
          await key.locator("summary").click();
        }
        await jump(page, "approval-preview");
        await page.screenshot({ path: path.join(ARTIFACTS, "desktop-approval.png") });
        await page.locator("#handbook-search").fill("磁盘满");
        await page.waitForFunction(() => document.querySelectorAll(".search-hidden").length > 0);
        assert.equal(await page.locator("#results").evaluate((node) => node.classList.contains("search-hidden")), false);
        // A valid cross-chapter link restores a target hidden by search.
        await page.locator('#results a[href="#D-L179"]').click();
        assert.equal(await page.locator("#handbook-search").inputValue(), "");
        assert.ok(await page.locator("#source-D .source-details").evaluate((node) => node.open));
        assert.ok(await page.locator("#D-L179").isVisible());
        await page.locator("#handbook-search").fill("不存在的搜索词xyz987");
        await page.waitForFunction(() => document.getElementById("search-status").textContent.includes("没有匹配章节"));
        await page.locator("#clear-search").click();
        assert.equal(await page.locator(".search-hidden").count(), 0);
        await jump(page, "five-gates");
        await page.waitForFunction(() => document.querySelector('.toc a[href="#five-gates"]').getAttribute("aria-current") === "location");
        await page.locator('#five-gates .lesson-nav a[href="#gate-execute"]').click();
        assert.equal(await page.evaluate(() => location.hash), "#gate-execute");

        // Print opens all answers and sources, and does not print only search matches.
        await page.locator("#handbook-search").fill("磁盘满");
        await page.waitForFunction(() => document.querySelectorAll(".search-hidden").length > 0);
        const before = await page.locator("details").evaluateAll((items) => items.map((item) => item.open));
        await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
        await page.emulateMedia({ media: "print" });
        assert.ok(await page.locator("details").evaluateAll((items) => items.every((item) => item.open)));
        assert.equal(await page.locator("#sidebar").evaluate((node) => getComputedStyle(node).display), "none");
        assert.ok(await page.locator(".chapter.search-hidden").evaluateAll((items) => items.every((item) => getComputedStyle(item).display !== "none")));
        assert.equal(await page.locator("#raw-M").evaluate((node) => node.textContent), fs.readFileSync(path.join(ROOT, "meeting/0910/会议录制：agent-meeting 0910-1.md"), "utf8"));
        await page.emulateMedia({ media: "screen" });
        await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));
        assert.deepEqual(await page.locator("details").evaluateAll((items) => items.map((item) => item.open)), before);
        await page.locator("#clear-search").click();
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        await page.waitForFunction(() => document.getElementById("back-to-top").classList.contains("is-visible"));
        assert.notEqual(await page.locator("#reading-progress").evaluate((node) => node.style.transform), "scaleX(0)");
        await page.locator("#back-to-top").click();
        await page.waitForFunction(() => window.scrollY < 2);
      }
      if (width === 375) {
        await page.locator("#mobile-nav-toggle").click();
        await page.locator('.toc a[href="#five-gates"]').click();
        assert.equal(await page.locator("#mobile-nav-toggle").getAttribute("aria-expanded"), "false");
        await noOverflow(page, "mobile gates");
        await page.screenshot({ path: path.join(ARTIFACTS, "mobile-gates.png") });
        await jump(page, "approval-preview");
        await page.screenshot({ path: path.join(ARTIFACTS, "mobile-approval.png") });
        await page.locator("#approval .answer-key summary").click();
        assert.ok(await page.locator("#approval .answer-key").evaluate((node) => node.open));
        await noOverflow(page, "mobile answers");
        await page.evaluate(() => document.documentElement.style.fontSize = "20px");
        await noOverflow(page, "mobile larger text");
        await page.evaluate(() => document.documentElement.style.fontSize = "");
        await page.setViewportSize({ width: 812, height: 375 });
        await noOverflow(page, "phone landscape");
      }
      // Initial deep links must reveal folded source text, not land on a closed panel.
      await page.goto(URL + "#M-L687", { waitUntil: "networkidle" });
      assert.ok(await page.locator("#source-M .source-details").evaluate((node) => node.open));
      assert.ok(await page.locator("#M-L687").isVisible());
      await noOverflow(page, "source " + width);
      assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior), "auto");
      await context.close();
    }
    const context = await browser.newContext({ viewport: { width: 375, height: 812 }, javaScriptEnabled: false, offline: true });
    const page = await context.newPage();
    await page.goto(URL);
    assert.ok(await page.locator('.toc a[href="#five-gates"]').isVisible());
    await page.locator('.toc a[href="#five-gates"]').click();
    assert.ok((await page.url()).endsWith("#five-gates"));
    await page.locator("#five-gates .answer-key summary").click();
    assert.ok(await page.locator("#five-gates .answer-key").evaluate((node) => node.open));
    await page.locator("#source-M .source-details summary").click();
    assert.ok(await page.locator("#raw-M").isVisible());
    await noOverflow(page, "no-script source");
    await context.close();
    assert.deepEqual(errors, [], "browser errors");
    assert.deepEqual(requests, [], "unexpected external network requests");
    console.log("PASS: tools workbook browser checks — four widths, source deep links, 10 answer pairs, search, print, keyboard, no JS, offline");
    console.log("Screenshots: " + ARTIFACTS);
  } finally {
    await browser.close();
  }
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
