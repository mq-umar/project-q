const { chromium, firefox, webkit } = require("playwright");

async function main() {
  const job = JSON.parse(process.argv[2] || "{}");
  const browserType = resolveBrowserType(job.browser_type);
  const launchOptions = {
    headless: job.headless !== false,
  };

  if (job.executable_path) {
    launchOptions.executablePath = job.executable_path;
  } else if (job.channel) {
    launchOptions.channel = job.channel;
  }

  let browser;
  try {
    browser = await browserType.launch(launchOptions);
  } catch (error) {
    if (launchOptions.channel) {
      delete launchOptions.channel;
      browser = await browserType.launch(launchOptions);
    } else {
      throw error;
    }
  }

  const page = await browser.newPage();
  page.setDefaultTimeout((job.timeout_seconds || 25) * 1000);

  try {
    await page.goto(job.url, { waitUntil: "domcontentloaded" });

    let result;
    if (job.mode === "inspect") {
      result = await inspectPage(page, job);
    } else if (job.mode === "actions") {
      result = await runActions(page, job);
    } else {
      throw new Error(`Unsupported browser mode: ${job.mode}`);
    }

    console.log(JSON.stringify(result));
  } finally {
    await page.close();
    await browser.close();
  }
}

function resolveBrowserType(name) {
  switch (name) {
    case "firefox":
      return firefox;
    case "webkit":
      return webkit;
    default:
      return chromium;
  }
}

async function inspectPage(page, job) {
  const title = await page.title();
  const url = page.url();
  const text = await page.locator("body").innerText().catch(() => "");
  let links = [];
  if (job.include_links) {
    links = await page
      .locator("a")
      .evaluateAll((anchors) =>
        anchors
          .map((anchor) => ({
            text: (anchor.textContent || "").trim(),
            href: anchor.href || "",
          }))
          .filter((item) => item.href)
          .slice(0, 15)
      )
      .catch(() => []);
  }

  if (job.screenshot_path) {
    await page.screenshot({ path: job.screenshot_path, fullPage: true });
  }

  return {
    mode: "inspect",
    title,
    url,
    text_excerpt: text.slice(0, 4000),
    links,
    screenshot_path: job.screenshot_path || "",
  };
}

async function runActions(page, job) {
  const actionResults = [];
  let lastExtractedText = "";

  for (const action of job.actions || []) {
    switch (action.type) {
      case "goto":
        await page.goto(action.url, { waitUntil: action.wait_until || "domcontentloaded" });
        actionResults.push({ type: "goto", url: page.url() });
        break;
      case "click":
        await page.click(action.selector);
        actionResults.push({ type: "click", selector: action.selector });
        break;
      case "fill":
        await page.fill(action.selector, action.value || "");
        actionResults.push({ type: "fill", selector: action.selector });
        break;
      case "press":
        await page.press(action.selector, action.key || "Enter");
        actionResults.push({ type: "press", selector: action.selector, key: action.key || "Enter" });
        break;
      case "wait_for_selector":
        await page.waitForSelector(action.selector, {
          state: action.state || "visible",
          timeout: action.timeout_ms || 10000,
        });
        actionResults.push({ type: "wait_for_selector", selector: action.selector });
        break;
      case "extract_text":
        lastExtractedText = await page.locator(action.selector || "body").innerText();
        actionResults.push({
          type: "extract_text",
          selector: action.selector || "body",
          text_excerpt: lastExtractedText.slice(0, 4000),
        });
        break;
      case "screenshot":
        if (!job.screenshot_path) {
          throw new Error("screenshot action requested without screenshot_path");
        }
        await page.screenshot({ path: job.screenshot_path, fullPage: action.full_page !== false });
        actionResults.push({ type: "screenshot", path: job.screenshot_path });
        break;
      default:
        throw new Error(`Unsupported action type: ${action.type}`);
    }
  }

  return {
    mode: "actions",
    title: await page.title(),
    url: page.url(),
    extracted_text: lastExtractedText.slice(0, 4000),
    action_results: actionResults,
    screenshot_path: job.screenshot_path || "",
  };
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
