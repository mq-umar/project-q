const { chromium, firefox, webkit } = require("playwright");
const dns = require("node:dns").promises;
const fs = require("node:fs").promises;
const net = require("node:net");
const path = require("node:path");

async function main() {
  const job = JSON.parse(process.argv[2] || "{}");
  const browserType = resolveBrowserType(job.browser_type);
  const launchOptions = {
    headless: job.headless !== false,
  };
  const contextOptions = {
    acceptDownloads: true,
    serviceWorkers: "block",
  };

  if (job.executable_path) {
    launchOptions.executablePath = job.executable_path;
  } else if (job.channel) {
    launchOptions.channel = job.channel;
  }

  const launched = await launchBrowser(
    job,
    browserType,
    launchOptions,
    contextOptions
  );
  const { browser, context } = launched;
  const securityStats = {
    blockedRequests: 0,
    blockedWebSockets: 0,
  };
  context.setDefaultTimeout((job.timeout_seconds || 25) * 1000);
  await context.route("**/*", async (route) => {
    const request = route.request();
    const requestUrl = request.url();
    if (isSafeInPageUrl(requestUrl)) {
      await route.continue();
      return;
    }
    try {
      await assertPublicHttpUrl(requestUrl);
      await route.continue();
    } catch (_error) {
      securityStats.blockedRequests += 1;
      await route.abort("blockedbyclient");
    }
  });
  await context.routeWebSocket(/.*/, async (webSocketRoute) => {
    try {
      await assertPublicWebSocketUrl(webSocketRoute.url());
      webSocketRoute.connectToServer();
    } catch (_error) {
      securityStats.blockedWebSockets += 1;
      await webSocketRoute.close({
        code: 1008,
        reason: "Project Q blocked a private WebSocket target.",
      });
    }
  });
  for (const existingPage of context.pages()) {
    await existingPage.close().catch(() => {});
  }
  const page = await context.newPage();
  page.setDefaultTimeout((job.timeout_seconds || 25) * 1000);

  try {
    let result;
    if (job.mode === "self_test") {
      result = await runSelfTest(context, page, job, securityStats);
    } else {
      await assertPublicHttpUrl(job.url);
      await page.goto(job.url, { waitUntil: "domcontentloaded" });
    }

    if (job.mode === "inspect") {
      result = await inspectPage(page, job);
    } else if (job.mode === "actions") {
      result = await runActions(context, page, job);
    } else if (job.mode !== "self_test") {
      throw new Error(`Unsupported browser mode: ${job.mode}`);
    }

    console.log(JSON.stringify(result));
  } finally {
    await context.close();
    if (browser) {
      await browser.close();
    }
  }
}

async function launchBrowser(job, browserType, launchOptions, contextOptions) {
  const launch = async (options) => {
    if (job.user_data_dir) {
      const context = await browserType.launchPersistentContext(
        job.user_data_dir,
        { ...options, ...contextOptions }
      );
      return { browser: null, context };
    }
    const browser = await browserType.launch(options);
    const context = await browser.newContext(contextOptions);
    return { browser, context };
  };

  try {
    return await launch(launchOptions);
  } catch (error) {
    if (!launchOptions.channel) {
      throw error;
    }
    const fallbackOptions = { ...launchOptions };
    delete fallbackOptions.channel;
    return launch(fallbackOptions);
  }
}

function isSafeInPageUrl(rawUrl) {
  try {
    const protocol = new URL(rawUrl).protocol;
    return ["about:", "blob:", "data:"].includes(protocol);
  } catch (_error) {
    return false;
  }
}

async function assertPublicHttpUrl(rawUrl) {
  const parsed = new URL(rawUrl);
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("Browser worker only supports public HTTP(S) URLs.");
  }
  await assertPublicHostname(parsed.hostname);
}

async function assertPublicWebSocketUrl(rawUrl) {
  const parsed = new URL(rawUrl);
  if (!["ws:", "wss:"].includes(parsed.protocol)) {
    throw new Error("Browser worker only supports public WebSocket URLs.");
  }
  await assertPublicHostname(parsed.hostname);
}

async function assertPublicHostname(rawHostname) {
  const hostname = String(rawHostname || "")
    .replace(/^\[|\]$/g, "")
    .toLowerCase();
  if (!hostname || hostname === "localhost" || hostname.endsWith(".localhost")) {
    throw new Error("Browser worker blocked a local hostname.");
  }
  const addresses = net.isIP(hostname)
    ? [{ address: hostname }]
    : await dns.lookup(hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some((item) => isPrivateAddress(item.address))) {
    throw new Error("Browser worker blocked a private network address.");
  }
}

function isPrivateAddress(rawAddress) {
  const address = String(rawAddress || "").toLowerCase().split("%", 1)[0];
  const version = net.isIP(address);
  if (version === 4) {
    const [a, b] = address.split(".").map(Number);
    return (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) ||
      a >= 224
    );
  }
  if (version === 6) {
    if (address === "::" || address === "::1") return true;
    if (address.startsWith("fc") || address.startsWith("fd")) return true;
    if (/^fe[89ab]/.test(address)) return true;
    if (address.startsWith("::ffff:")) {
      return isPrivateAddress(address.slice("::ffff:".length));
    }
  }
  return version === 0;
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

async function runSelfTest(context, page, job, securityStats) {
  const checks = {
    launch: true,
    form: false,
    tabs: false,
    screenshot: false,
    download: false,
    upload: false,
    private_requests_blocked: false,
    private_websockets_blocked: false,
  };
  await page.setContent(`
    <!doctype html>
    <html>
      <head><title>Project Q Browser Self-Test</title></head>
      <body>
        <label>Name <input id="name"></label>
        <label>Mode
          <select id="mode">
            <option value="inspect">Inspect</option>
            <option value="control">Control</option>
          </select>
        </label>
        <label><input id="confirmed" type="checkbox"> Confirmed</label>
        <button id="submit" type="button">Run</button>
        <a id="download" href="data:text/plain,Project%20Q" download="browser-self-test.txt">Download</a>
        <input id="upload" type="file">
        <output id="upload-name"></output>
        <output id="result"></output>
        <script>
          document.querySelector("#submit").addEventListener("click", () => {
            const name = document.querySelector("#name").value;
            const mode = document.querySelector("#mode").value;
            const confirmed = document.querySelector("#confirmed").checked;
            document.querySelector("#result").textContent =
              name + "|" + mode + "|" + String(confirmed);
          });
          document.querySelector("#upload").addEventListener("change", (event) => {
            document.querySelector("#upload-name").textContent =
              event.target.files[0]?.name || "";
          });
        </script>
      </body>
    </html>
  `);
  const actionProbe = await runActions(context, page, {
    ...job,
    screenshot_path: "",
    actions: [
      { type: "fill", selector: "#name", value: "Project Q" },
      { type: "press", selector: "#name", key: "End" },
      { type: "select_option", selector: "#mode", value: "control" },
      { type: "check", selector: "#confirmed" },
      { type: "hover", selector: "#submit" },
      { type: "click", selector: "#submit" },
      { type: "wait_for_selector", selector: "#result", state: "visible" },
      { type: "wait_for_timeout", timeout_ms: 10 },
      { type: "extract_text", selector: "#result" },
      { type: "uncheck", selector: "#confirmed" },
      { type: "download", selector: "#download" },
      {
        type: "upload",
        selector: "#upload",
        path: job.self_test_upload_path,
      },
    ],
  });
  checks.form = actionProbe.extracted_text === "Project Q|control|true";
  const downloadResult = actionProbe.action_results.find(
    (result) => result.type === "download"
  );
  if (downloadResult?.path) {
    await fs.access(downloadResult.path);
    checks.download = true;
  }
  checks.upload =
    (await page.locator("#upload-name").innerText()) ===
    path.basename(job.self_test_upload_path || "");

  const secondPage = await context.newPage();
  await secondPage.setContent("<title>Second Tab</title><p id='tab'>ready</p>");
  const secondPageIndex = activePages(context).indexOf(secondPage);
  const tabProbe = await runActions(context, page, {
    ...job,
    screenshot_path: "",
    actions: [
      { type: "switch_tab", index: secondPageIndex },
      { type: "extract_text", selector: "#tab" },
      { type: "close_tab" },
    ],
  });
  checks.tabs =
    tabProbe.extracted_text === "ready" &&
    secondPage.isClosed();

  await page.evaluate(async () => {
    await fetch("http://127.0.0.1:9/project-q-private-request").catch(() => {});
    await new Promise((resolve) => {
      const socket = new WebSocket("ws://127.0.0.1:9/project-q-private-websocket");
      socket.addEventListener("error", resolve, { once: true });
      socket.addEventListener("close", resolve, { once: true });
      setTimeout(resolve, 250);
    });
  });
  checks.private_requests_blocked = securityStats.blockedRequests > 0;
  checks.private_websockets_blocked = securityStats.blockedWebSockets > 0;

  if (job.screenshot_path) {
    await page.screenshot({ path: job.screenshot_path, fullPage: true });
    checks.screenshot = true;
  }

  let previousProfileProbe = "";
  if (job.profile_probe) {
    const cookies = await context.cookies("https://example.com");
    previousProfileProbe =
      cookies.find((cookie) => cookie.name === "project_q_browser_probe")?.value || "";
    await context.addCookies([
      {
        name: "project_q_browser_probe",
        value: String(job.profile_probe),
        url: "https://example.com",
        sameSite: "Lax",
        expires: Math.floor(Date.now() / 1000) + 3600,
      },
    ]);
  }

  return {
    mode: "self_test",
    title: await page.title(),
    checks,
    screenshot_path: job.screenshot_path || "",
    previous_profile_probe: previousProfileProbe,
  };
}

async function runActions(context, initialPage, job) {
  const actionResults = [];
  let lastExtractedText = "";
  let page = initialPage;
  let screenshotTaken = false;

  for (const action of job.actions || []) {
    switch (action.type) {
      case "goto":
        await assertPublicHttpUrl(action.url);
        await page.goto(action.url, { waitUntil: action.wait_until || "domcontentloaded" });
        actionResults.push({ type: "goto", url: page.url() });
        break;
      case "click": {
        if (action.expect_popup) {
          const popupPromise = context.waitForEvent("page");
          await page.click(action.selector);
          page = await popupPromise;
          page.setDefaultTimeout((job.timeout_seconds || 25) * 1000);
          await page.waitForLoadState(action.wait_until || "domcontentloaded").catch(() => {});
        } else {
          await page.click(action.selector);
        }
        actionResults.push({
          type: "click",
          selector: action.selector,
          url: page.url(),
        });
        break;
      }
      case "fill":
        await page.fill(action.selector, action.value || "");
        actionResults.push({ type: "fill", selector: action.selector });
        break;
      case "select_option":
        await page.selectOption(action.selector, action.value);
        actionResults.push({ type: "select_option", selector: action.selector });
        break;
      case "check":
        await page.check(action.selector);
        actionResults.push({ type: "check", selector: action.selector });
        break;
      case "uncheck":
        await page.uncheck(action.selector);
        actionResults.push({ type: "uncheck", selector: action.selector });
        break;
      case "hover":
        await page.hover(action.selector);
        actionResults.push({ type: "hover", selector: action.selector });
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
      case "wait_for_timeout":
        await page.waitForTimeout(action.timeout_ms || 1000);
        actionResults.push({ type: "wait_for_timeout", timeout_ms: action.timeout_ms || 1000 });
        break;
      case "pause_for_owner":
        await page.waitForTimeout(action.timeout_ms || 30000);
        actionResults.push({ type: "pause_for_owner", timeout_ms: action.timeout_ms || 30000 });
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
        screenshotTaken = true;
        actionResults.push({ type: "screenshot", path: job.screenshot_path });
        break;
      case "new_tab":
        await assertPublicHttpUrl(action.url);
        page = await context.newPage();
        page.setDefaultTimeout((job.timeout_seconds || 25) * 1000);
        await page.goto(action.url, { waitUntil: action.wait_until || "domcontentloaded" });
        actionResults.push({
          type: "new_tab",
          index: activePages(context).indexOf(page),
          url: page.url(),
        });
        break;
      case "switch_tab": {
        const pages = activePages(context);
        if (!pages[action.index]) {
          throw new Error(`Browser tab index ${action.index} does not exist.`);
        }
        page = pages[action.index];
        await page.bringToFront();
        actionResults.push({ type: "switch_tab", index: action.index, url: page.url() });
        break;
      }
      case "close_tab": {
        const pages = activePages(context);
        if (pages.length <= 1) {
          throw new Error("Cannot close the only browser tab.");
        }
        const closingIndex = pages.indexOf(page);
        await page.close();
        const remaining = activePages(context);
        page = remaining[Math.min(Math.max(closingIndex - 1, 0), remaining.length - 1)];
        await page.bringToFront();
        actionResults.push({ type: "close_tab", url: page.url() });
        break;
      }
      case "download": {
        if (!job.download_dir) {
          throw new Error("download action requested without download_dir");
        }
        const downloadPromise = page.waitForEvent("download");
        await page.click(action.selector);
        const download = await downloadPromise;
        const filename = safeDownloadName(action.filename || download.suggestedFilename());
        const target = path.resolve(job.download_dir, filename);
        const root = path.resolve(job.download_dir) + path.sep;
        if (!target.startsWith(root)) {
          throw new Error("Browser download path escaped the artifact directory.");
        }
        await download.saveAs(target);
        actionResults.push({ type: "download", path: target });
        break;
      }
      case "upload":
        await page.setInputFiles(action.selector, action.path);
        actionResults.push({
          type: "upload",
          selector: action.selector,
          filename: path.basename(action.path),
        });
        break;
      default:
        throw new Error(`Unsupported action type: ${action.type}`);
    }
  }

  if (job.screenshot_path && !screenshotTaken) {
    await page.screenshot({ path: job.screenshot_path, fullPage: true });
  }

  const pages = activePages(context);
  return {
    mode: "actions",
    title: await page.title(),
    url: page.url(),
    extracted_text: lastExtractedText.slice(0, 4000),
    action_results: actionResults,
    screenshot_path: job.screenshot_path || "",
    active_tab_index: pages.indexOf(page),
    tabs: await Promise.all(
      pages.map(async (candidate, index) => ({
        index,
        title: await candidate.title().catch(() => ""),
        url: candidate.url(),
      }))
    ),
  };
}

function activePages(context) {
  return context.pages().filter((page) => !page.isClosed());
}

function safeDownloadName(rawName) {
  const filename = path.basename(String(rawName || "download.bin")).replace(
    /[^A-Za-z0-9._ -]/g,
    "_"
  );
  if (!filename || filename === "." || filename === "..") {
    return "download.bin";
  }
  return filename.slice(0, 180);
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});
