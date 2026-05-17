from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_q.tools.base import ToolDefinition


@dataclass(slots=True)
class WebsiteSpec:
    instruction: str
    site_kind: str
    brand_name: str
    industry: str
    tone: str
    theme: str
    pages: list[str]
    features: list[str]
    primary_cta: str
    secondary_cta: str
    slug: str


class WebsiteGeneratorTool:
    definition = ToolDefinition(
        tool_id="code.generate_website",
        name="Generate Website",
        description="Create a prompt-aware website or static web app with HTML, CSS, JavaScript, README, and dispatch tracking",
        tier=2,
    )

    def __init__(self, workspace_root: Path, dispatch_service=None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.dispatch_service = dispatch_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction", "")).strip()
        if not instruction:
            raise ValueError("code.generate_website requires an instruction")

        spec = self._build_spec(instruction, payload)
        project_dir = self._project_dir(payload, spec)
        project_dir.mkdir(parents=True, exist_ok=True)

        files = self._files_for_spec(spec)
        written_files: list[dict[str, Any]] = []
        for filename, content in files.items():
            path = project_dir / filename
            path.write_text(content, encoding="utf-8")
            written_files.append(
                {
                    "path": str(path),
                    "bytes_written": len(content.encode("utf-8")),
                }
            )

        summary = f"Created a {spec.site_kind.replace('_', ' ')} for {spec.brand_name}."
        dispatch = None
        if self.dispatch_service is not None:
            dispatch = self.dispatch_service.record_completed(
                task_type="fullstack_app" if spec.site_kind == "static_web_app" else "multipage_website",
                project_name=spec.brand_name,
                original_prompt=instruction,
                project_path=str(project_dir),
                artifacts=written_files,
                acceptance_criteria=self._acceptance_criteria(spec),
                summary=summary,
            )

        result = {
            "status": "created",
            "site_kind": spec.site_kind,
            "brand_name": spec.brand_name,
            "industry": spec.industry,
            "theme": spec.theme,
            "pages": spec.pages,
            "project_dir": str(project_dir),
            "entrypoint": str(project_dir / "index.html"),
            "files": written_files,
            "summary": summary,
            "how_to_open": f"Open {project_dir / 'index.html'} in your browser.",
        }
        if dispatch is not None:
            result["dispatch_id"] = dispatch["id"]
        return result

    def _build_spec(self, instruction: str, payload: dict[str, Any]) -> WebsiteSpec:
        lowered = instruction.lower()
        site_kind = "static_web_app" if self._looks_like_web_app(lowered) else "multipage_website"
        industry = self._industry(instruction)
        brand_name = str(payload.get("company_name") or self._brand_name(instruction, industry)).strip()
        tone = self._tone(lowered)
        theme = self._theme(lowered, industry)
        pages = self._pages(lowered, site_kind)
        features = self._features(lowered, industry, site_kind)
        return WebsiteSpec(
            instruction=instruction,
            site_kind=site_kind,
            brand_name=brand_name,
            industry=industry,
            tone=tone,
            theme=theme,
            pages=pages,
            features=features,
            primary_cta=self._primary_cta(lowered, industry, site_kind),
            secondary_cta="Explore the Work" if "gallery" in pages else "See Services",
            slug=self._slug(brand_name),
        )

    def _files_for_spec(self, spec: WebsiteSpec) -> dict[str, str]:
        if spec.site_kind == "static_web_app":
            return {
                "index.html": self._app_html(spec),
                "styles.css": self._styles_css(spec),
                "app.js": self._app_js(spec),
                "README.md": self._readme(spec),
            }

        files = {
            "index.html": self._website_html(spec, "home"),
            "styles.css": self._styles_css(spec),
            "script.js": self._script_js(spec),
            "README.md": self._readme(spec),
        }
        for page in spec.pages:
            if page == "home":
                continue
            files[f"{page}.html"] = self._website_html(spec, page)
        return files

    def _project_dir(self, payload: dict[str, Any], spec: WebsiteSpec) -> Path:
        raw_target = str(payload.get("target_dir", "")).strip()
        if raw_target:
            candidate = Path(raw_target)
        else:
            folder = "generated_apps" if spec.site_kind == "static_web_app" else "generated_sites"
            candidate = Path(folder) / spec.slug
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise PermissionError("Website projects must be generated inside the workspace.")
        return resolved

    def _website_html(self, spec: WebsiteSpec, page: str) -> str:
        safe_brand = html.escape(spec.brand_name)
        safe_industry = html.escape(spec.industry)
        safe_instruction = html.escape(spec.instruction)
        page_title = self._page_title(page)
        nav = self._nav_html(spec, page)
        page_body = self._page_body(spec, page)
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta
      name="description"
      content="{safe_brand} is a {spec.tone} {safe_industry} website generated by Project Q."
    />
    <title>{safe_brand} | {page_title}</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body data-theme="{html.escape(spec.theme)}">
    <header class="site-header">
      <a class="brand" href="index.html" aria-label="{safe_brand} home">
        <span class="brand-mark">{self._brand_mark(spec.brand_name)}</span>
        <span>{safe_brand}</span>
      </a>
      <nav aria-label="Primary navigation">
        {nav}
      </nav>
    </header>

    <main>
      {page_body}
    </main>

    <footer>
      <span>{safe_brand}</span>
      <span>Built for {safe_industry}. Generated from: "{safe_instruction}"</span>
    </footer>
    <script src="script.js"></script>
  </body>
</html>
"""

    def _app_html(self, spec: WebsiteSpec) -> str:
        safe_brand = html.escape(spec.brand_name)
        cards = "\n".join(
            f"""          <article class="dashboard-card">
            <span>{html.escape(feature)}</span>
            <strong>{self._feature_metric(feature)}</strong>
            <p>{html.escape(self._feature_copy(feature, spec.industry))}</p>
          </article>"""
            for feature in spec.features[:4]
        )
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="description" content="{safe_brand} static web app generated by Project Q." />
    <title>{safe_brand} | Web App</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body data-theme="{html.escape(spec.theme)}">
    <main class="app-frame">
      <aside class="app-sidebar">
        <a class="brand" href="index.html">
          <span class="brand-mark">{self._brand_mark(spec.brand_name)}</span>
          <span>{safe_brand}</span>
        </a>
        <nav aria-label="App sections">
          <a href="#dashboard">Dashboard</a>
          <a href="#clients">Clients</a>
          <a href="#reports">Reports</a>
          <a href="#notes">Client Notes</a>
        </nav>
      </aside>

      <section class="app-main" id="dashboard">
        <div class="app-topbar">
          <div>
            <p class="eyebrow">Static Web App Prototype</p>
            <h1>{safe_brand}</h1>
          </div>
          <button id="demoLogin" type="button">{html.escape(spec.primary_cta)}</button>
        </div>

        <section class="dashboard-grid" aria-label="Application dashboard">
{cards}
        </section>

        <section id="clients" class="workspace-panel">
          <div>
            <p class="eyebrow">Clients</p>
            <h2>Pipeline, owners, and next best actions.</h2>
          </div>
          <ul id="clientList"></ul>
        </section>

        <section id="reports" class="workspace-panel">
          <div>
            <p class="eyebrow">Reports</p>
            <h2>Revenue, retention, and delivery health.</h2>
          </div>
          <div class="report-bars" id="reportBars"></div>
        </section>

        <section id="notes" class="workspace-panel">
          <div>
            <p class="eyebrow">Client Notes</p>
            <h2>Capture important context without losing the thread.</h2>
          </div>
          <textarea id="notePad" rows="7" placeholder="Type notes here..."></textarea>
        </section>
      </section>
    </main>
    <script src="app.js"></script>
  </body>
</html>
"""

    def _page_body(self, spec: WebsiteSpec, page: str) -> str:
        safe_brand = html.escape(spec.brand_name)
        safe_industry = html.escape(spec.industry)
        if page == "pricing":
            return self._pricing_section(spec)
        if page == "gallery":
            return self._gallery_section(spec)
        if page == "booking":
            return self._booking_section(spec)
        if page == "contact":
            return self._contact_section(spec)
        if page == "menu":
            return self._menu_section(spec)
        if page == "reservations":
            return self._booking_section(spec, title="Reserve a Table")
        if page == "about":
            return self._about_section(spec)
        if page == "services":
            return self._services_section(spec)

        feature_cards = "\n".join(
            f"""        <article>
          <h3>{html.escape(feature)}</h3>
          <p>{html.escape(self._feature_copy(feature, spec.industry))}</p>
        </article>"""
            for feature in spec.features[:3]
        )
        return f"""      <section class="hero">
        <div class="hero-copy">
          <p class="eyebrow">{html.escape(spec.tone)} {safe_industry}</p>
          <h1>{html.escape(self._headline(spec))}</h1>
          <p>
            {safe_brand} turns a specific customer promise into a memorable digital experience:
            clear copy, practical sections, strong calls to action, and a visual system matched to the request.
          </p>
          <div class="hero-actions">
            <a class="primary-button" href="{self._page_href(spec.primary_cta, spec)}">{html.escape(spec.primary_cta)}</a>
            <a class="secondary-button" href="{self._page_href(spec.secondary_cta, spec)}">{html.escape(spec.secondary_cta)}</a>
          </div>
        </div>
        <aside class="signal-card" aria-label="Project Q generation highlights">
          <span>{safe_industry}</span>
          <strong>{len(spec.pages)}</strong>
          <p>Prompt-aware pages generated for the exact request instead of a fixed landing page.</p>
        </aside>
      </section>

      <section class="metrics" aria-label="Highlights">
        <article><strong>{self._metric_one(spec)}</strong><span>{html.escape(spec.primary_cta)}</span></article>
        <article><strong>{len(spec.features)}</strong><span>Custom sections</span></article>
        <article><strong>Local</strong><span>Generated in your workspace</span></article>
      </section>

      <section class="section">
        <p class="eyebrow">What This Site Covers</p>
        <h2>Built around the prompt, not around a canned template.</h2>
        <div class="service-grid">
{feature_cards}
        </div>
      </section>"""

    def _services_section(self, spec: WebsiteSpec) -> str:
        items = "\n".join(
            f"""        <article>
          <h3>{html.escape(feature)}</h3>
          <p>{html.escape(self._feature_copy(feature, spec.industry))}</p>
        </article>"""
            for feature in spec.features
        )
        return f"""      <section class="section">
        <p class="eyebrow">Services</p>
        <h1>{html.escape(spec.brand_name)} services</h1>
        <div class="service-grid">
{items}
        </div>
      </section>"""

    def _pricing_section(self, spec: WebsiteSpec) -> str:
        if "barber" in spec.industry.lower():
            plans = [("Signature Cut", "$38", "Consultation, precision cut, styling."), ("Crown Package", "$72", "Cut, beard work, hot towel finish."), ("Monthly Chair", "$180", "Weekly maintenance for regular clients.")]
        else:
            plans = [("Starter", "$499", "Launch-ready essentials."), ("Growth", "$1,500", "Strategy, build, and automation support."), ("Partner", "Custom", "High-touch execution and ongoing optimization.")]
        cards = "\n".join(
            f"""        <article class="price-card">
          <h3>{html.escape(name)}</h3>
          <strong>{html.escape(price)}</strong>
          <p>{html.escape(copy)}</p>
        </article>"""
            for name, price, copy in plans
        )
        return f"""      <section class="section">
        <p class="eyebrow">Pricing</p>
        <h1>Simple options for getting started.</h1>
        <div class="pricing-grid">
{cards}
        </div>
      </section>"""

    def _gallery_section(self, spec: WebsiteSpec) -> str:
        items = "\n".join(
            f"""        <article class="gallery-tile">
          <span>0{index}</span>
          <h3>{html.escape(label)}</h3>
        </article>"""
            for index, label in enumerate(["Signature look", "Detail work", "Client result", "Studio atmosphere"], start=1)
        )
        return f"""      <section class="section">
        <p class="eyebrow">Gallery</p>
        <h1>Visual proof for {html.escape(spec.brand_name)}.</h1>
        <div class="gallery-grid">
{items}
        </div>
      </section>"""

    def _booking_section(self, spec: WebsiteSpec, title: str | None = None) -> str:
        safe_title = html.escape(title or spec.primary_cta)
        return f"""      <section class="section split">
        <div>
          <p class="eyebrow">Booking</p>
          <h1>{safe_title}</h1>
          <p>Use this static form as the front-end placeholder for your future scheduling or CRM integration.</p>
        </div>
        <form class="booking-form">
          <input placeholder="Name" />
          <input placeholder="Email or phone" />
          <select>
            <option>First available</option>
            <option>Premium slot</option>
            <option>Consultation</option>
          </select>
          <textarea rows="4" placeholder="What do you need?"></textarea>
          <button type="button">{html.escape(spec.primary_cta)}</button>
        </form>
      </section>"""

    def _contact_section(self, spec: WebsiteSpec) -> str:
        return f"""      <section class="cta">
        <p class="eyebrow">Contact</p>
        <h1>Talk to {html.escape(spec.brand_name)}.</h1>
        <p>Email hello@example.com, call (555) 010-2026, or connect this form to your real backend later.</p>
        <a class="primary-button" href="mailto:hello@example.com">hello@example.com</a>
      </section>"""

    def _menu_section(self, spec: WebsiteSpec) -> str:
        items = [("Chef Selection", "$28"), ("Seasonal Plate", "$22"), ("House Dessert", "$12"), ("Reserve Pairing", "$18")]
        rows = "\n".join(f"          <li><span>{html.escape(name)}</span><strong>{price}</strong></li>" for name, price in items)
        return f"""      <section class="section">
        <p class="eyebrow">Menu</p>
        <h1>A focused menu for {html.escape(spec.brand_name)}.</h1>
        <ul class="menu-list">
{rows}
        </ul>
      </section>"""

    def _about_section(self, spec: WebsiteSpec) -> str:
        return f"""      <section class="section split">
        <div>
          <p class="eyebrow">About</p>
          <h1>A sharper identity for {html.escape(spec.brand_name)}.</h1>
        </div>
        <p>{html.escape(spec.brand_name)} is positioned as a {html.escape(spec.tone)} {html.escape(spec.industry)} brand with a clear promise, memorable visuals, and direct conversion paths.</p>
      </section>"""

    def _styles_css(self, spec: WebsiteSpec) -> str:
        palette = self._palette(spec)
        layout = "app" if spec.site_kind == "static_web_app" else "site"
        return f""":root {{
  --ink: {palette['ink']};
  --muted: {palette['muted']};
  --paper: {palette['paper']};
  --panel: {palette['panel']};
  --accent: {palette['accent']};
  --accent-2: {palette['accent_2']};
  --night: {palette['night']};
  --line: {palette['line']};
  font-family: {palette['font']};
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(circle at 18% 12%, {palette['glow']}, transparent 30rem),
    linear-gradient(135deg, var(--paper), {palette['wash']} 56%, var(--panel));
}}

a {{ color: inherit; text-decoration: none; }}
button, input, textarea, select {{ font: inherit; }}

.site-header {{
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1.2rem clamp(1rem, 4vw, 4rem);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--paper) 82%, transparent);
  backdrop-filter: blur(18px);
}}

.brand, nav, .hero-actions, footer {{
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}}

.brand {{ font-weight: 900; letter-spacing: -0.04em; }}
.brand-mark {{
  display: grid;
  place-items: center;
  width: 2.65rem;
  height: 2.65rem;
  color: var(--night);
  background: var(--accent);
  border-radius: 999px;
  box-shadow: 0 16px 50px color-mix(in srgb, var(--accent) 28%, transparent);
}}

nav a {{ color: var(--muted); font-weight: 800; }}

.hero {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) 22rem;
  gap: clamp(2rem, 5vw, 5rem);
  padding: clamp(4rem, 9vw, 8rem) clamp(1rem, 4vw, 4rem);
  min-height: 70vh;
  align-items: center;
}}

.hero h1, .section h1, .section h2, .cta h1, .app-topbar h1 {{
  margin: 0;
  letter-spacing: -0.075em;
  line-height: 0.9;
}}

.hero h1 {{ max-width: 12ch; font-size: clamp(3.4rem, 8vw, 8.2rem); }}
.section h1, .section h2, .cta h1 {{ max-width: 16ch; font-size: clamp(2.5rem, 5.6vw, 5.2rem); }}

p, li, input, textarea, select {{ line-height: 1.7; }}
.hero p, .section p, .cta p, .signal-card p {{ color: var(--muted); max-width: 44rem; }}
.eyebrow {{
  color: var(--accent-2) !important;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  font-size: 0.74rem;
  font-weight: 900;
}}

.primary-button, .secondary-button, button {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 3rem;
  padding: 0 1.15rem;
  border-radius: 999px;
  border: 1px solid transparent;
  font-weight: 900;
}}
.primary-button, button {{ background: var(--night); color: white; }}
.secondary-button {{ border-color: var(--line); color: var(--ink); }}

.signal-card, .price-card, .gallery-tile, .dashboard-card, .workspace-panel {{
  padding: 1.4rem;
  border: 1px solid var(--line);
  border-radius: 1.5rem;
  background: color-mix(in srgb, var(--panel) 64%, white 18%);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.12);
}}

.signal-card {{ color: white; background: linear-gradient(160deg, var(--night), color-mix(in srgb, var(--night), var(--accent) 28%)); }}
.signal-card strong {{ display: block; margin: 1rem 0; font-size: 4.4rem; line-height: 1; }}

.metrics, .service-grid, .pricing-grid, .gallery-grid, .dashboard-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
}}
.metrics {{ padding: 0 clamp(1rem, 4vw, 4rem) 4rem; }}
.metrics article {{
  padding: 1.2rem;
  border: 1px solid var(--line);
  border-radius: 1.2rem;
  background: color-mix(in srgb, var(--panel) 60%, white 22%);
}}
.metrics strong {{ display: block; font-size: 2rem; }}
.metrics span {{ color: var(--muted); }}

.section, .cta {{
  margin: 0 clamp(1rem, 4vw, 4rem) 4rem;
  padding: clamp(2rem, 5vw, 4rem);
}}
.service-grid, .pricing-grid, .gallery-grid {{ margin-top: 2rem; }}
.split {{ display: grid; grid-template-columns: 0.9fr 1.1fr; gap: 2rem; align-items: start; }}
.booking-form, .menu-list {{ display: grid; gap: 0.8rem; }}
input, textarea, select {{
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 1rem;
  background: color-mix(in srgb, var(--paper) 70%, white 18%);
  color: var(--ink);
  padding: 0.95rem 1rem;
}}
.gallery-tile {{ min-height: 13rem; display: flex; flex-direction: column; justify-content: space-between; }}
.gallery-tile span {{ color: var(--accent-2); font-weight: 900; }}
.menu-list {{ list-style: none; padding: 0; }}
.menu-list li {{ display: flex; justify-content: space-between; border-bottom: 1px solid var(--line); padding: 1rem 0; }}
.cta {{ text-align: center; }}
.cta h1, .cta p {{ margin-left: auto; margin-right: auto; }}
footer {{ justify-content: space-between; padding: 2rem clamp(1rem, 4vw, 4rem); color: var(--muted); border-top: 1px solid var(--line); }}

.app-frame {{ min-height: 100vh; display: grid; grid-template-columns: 17rem 1fr; }}
.app-sidebar {{ padding: 1.2rem; border-right: 1px solid var(--line); background: color-mix(in srgb, var(--night) 92%, black 8%); color: white; }}
.app-sidebar nav {{ align-items: stretch; flex-direction: column; margin-top: 2rem; }}
.app-sidebar nav a {{ color: color-mix(in srgb, white 78%, var(--accent)); padding: 0.8rem 0; }}
.app-main {{ padding: clamp(1rem, 3vw, 2.4rem); }}
.app-topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 1.4rem; }}
.dashboard-grid {{ grid-template-columns: repeat(4, 1fr); margin-bottom: 1.2rem; }}
.dashboard-card strong {{ display: block; margin: 0.6rem 0; font-size: 2rem; }}
.workspace-panel {{ margin-top: 1rem; }}
.report-bars {{ display: grid; gap: 0.7rem; }}
.bar {{ height: 0.9rem; border-radius: 999px; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}

@media (max-width: 920px) {{
  .site-header, .brand, nav, .hero-actions, footer, .app-topbar {{ align-items: flex-start; flex-direction: column; }}
  .hero, .split, .metrics, .service-grid, .pricing-grid, .gallery-grid, .dashboard-grid, .app-frame {{ grid-template-columns: 1fr; }}
  .hero h1 {{ font-size: clamp(3rem, 18vw, 5.8rem); }}
  .app-sidebar {{ border-right: 0; }}
}}

/* Generated layout mode: {layout}. */
"""

    def _script_js(self, spec: WebsiteSpec) -> str:
        return f"""const header = document.querySelector(".site-header");

window.addEventListener("scroll", () => {{
  header?.classList.toggle("is-scrolled", window.scrollY > 24);
}});

document.querySelectorAll('a[href^="#"]').forEach((link) => {{
  link.addEventListener("click", (event) => {{
    const target = document.querySelector(link.getAttribute("href"));
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({{ behavior: "smooth", block: "start" }});
  }});
}});

console.log("{self._escape_js(spec.brand_name)} generated by Project Q");
"""

    def _app_js(self, spec: WebsiteSpec) -> str:
        clients = [
            {"name": "Apex Medical", "stage": "Discovery", "value": "$12.4k"},
            {"name": "Northwind Supply", "stage": "Proposal", "value": "$8.8k"},
            {"name": "Crown Retail", "stage": "Delivery", "value": "$21.0k"},
        ]
        return f"""const clients = {self._js_array(clients)};
const reportData = [78, 54, 92, 66];

function renderClients() {{
  const list = document.querySelector("#clientList");
  if (!list) return;
  list.innerHTML = clients
    .map((client) => `<li><strong>${{client.name}}</strong> - ${{client.stage}} - ${{client.value}}</li>`)
    .join("");
}}

function renderReports() {{
  const bars = document.querySelector("#reportBars");
  if (!bars) return;
  bars.innerHTML = reportData
    .map((value) => `<div class="bar" style="width: ${{value}}%"></div>`)
    .join("");
}}

document.querySelector("#demoLogin")?.addEventListener("click", () => {{
  document.body.classList.toggle("is-authenticated");
  document.querySelector("#demoLogin").textContent = "Demo Session Active";
}});

renderClients();
renderReports();

console.log("{self._escape_js(spec.brand_name)} static web app generated by Project Q");
"""

    def _readme(self, spec: WebsiteSpec) -> str:
        files = ["index.html", "styles.css", "README.md"]
        if spec.site_kind == "static_web_app":
            files.append("app.js")
        else:
            files.append("script.js")
            files.extend(f"{page}.html" for page in spec.pages if page != "home")
        file_lines = "\n".join(f"- `{file}`" for file in files)
        return f"""# {spec.brand_name}

Generated by Project Q from:

```text
{spec.instruction}
```

## Type

{spec.site_kind}

## Prompt-Aware Choices

- Industry: {spec.industry}
- Tone: {spec.tone}
- Theme: {spec.theme}
- Pages: {", ".join(spec.pages)}

## Files

{file_lines}

## How To Open

Open `index.html` in a browser.
"""

    def _nav_html(self, spec: WebsiteSpec, active_page: str) -> str:
        return "\n        ".join(
            f"""<a class="{ 'active' if page == active_page else '' }" href="{self._filename_for_page(page)}">{html.escape(self._page_title(page))}</a>"""
            for page in spec.pages
        )

    @staticmethod
    def _looks_like_web_app(lowered: str) -> bool:
        return any(marker in lowered for marker in ("web app", "dashboard", "crm", "login", "reports", "client notes", "portal", "saas"))

    def _brand_name(self, instruction: str, industry: str) -> str:
        for pattern in (
            r"\bcalled\s+(.+?)(?:\s+with\b|\s+for\b|$)",
            r"\bnamed\s+(.+?)(?:\s+with\b|\s+for\b|$)",
            r"\bfor\s+([A-Z][A-Za-z0-9&' -]+?)(?:\s+with\b|$)",
        ):
            match = re.search(pattern, instruction)
            if match:
                candidate = self._clean_brand(match.group(1))
                if candidate and not self._looks_like_industry(candidate):
                    return candidate
        if "barber" in industry.lower():
            return "Crown Fade Studio"
        if "restaurant" in industry.lower():
            return "Saffron Table"
        if "it" in industry.lower() or "consulting" in industry.lower():
            return "Apex Signal IT"
        if "crm" in instruction.lower():
            return "Project Q CRM"
        return "Project Q Studio"

    @staticmethod
    def _industry(instruction: str) -> str:
        lowered = instruction.lower()
        if "barber" in lowered or "fade" in lowered:
            return "Barber Shop"
        if "restaurant" in lowered or "menu" in lowered or "reservation" in lowered:
            return "Restaurant"
        if "crm" in lowered:
            return "CRM Software"
        if "consult" in lowered and ("it" in lowered or "tech" in lowered):
            return "IT Consulting"
        if "ai" in lowered:
            return "AI Services"
        if "real estate" in lowered:
            return "Real Estate"
        if "fitness" in lowered or "gym" in lowered:
            return "Fitness"
        return "Professional Services"

    @staticmethod
    def _tone(lowered: str) -> str:
        if any(word in lowered for word in ("luxury", "premium", "high-end")):
            return "luxury"
        if any(word in lowered for word in ("warm", "cozy", "friendly")):
            return "warm"
        if any(word in lowered for word in ("minimal", "clean", "simple")):
            return "minimal"
        if any(word in lowered for word in ("bold", "edgy", "modern")):
            return "bold"
        return "modern"

    @staticmethod
    def _theme(lowered: str, industry: str) -> str:
        if "dark" in lowered or "luxury" in lowered:
            return "dark-luxury"
        if "restaurant" in industry.lower() or "warm" in lowered:
            return "warm-editorial"
        if "crm" in industry.lower() or "dashboard" in lowered:
            return "operator-dashboard"
        if "minimal" in lowered:
            return "minimal-light"
        return "executive"

    def _pages(self, lowered: str, site_kind: str) -> list[str]:
        if site_kind == "static_web_app":
            return ["home", "dashboard", "clients", "reports", "notes"]
        pages = ["home"]
        marker_map = {
            "services": ("services", "service"),
            "pricing": ("pricing", "prices", "packages", "rates"),
            "gallery": ("gallery", "portfolio", "photos", "images"),
            "booking": ("booking", "book", "appointments", "schedule"),
            "menu": ("menu",),
            "reservations": ("reservation", "reservations"),
            "about": ("about", "story"),
            "contact": ("contact", "email", "phone"),
        }
        for page, markers in marker_map.items():
            if any(marker in lowered for marker in markers) and page not in pages:
                pages.append(page)
        if len(pages) == 1:
            pages.extend(["services", "about", "contact"])
        elif "contact" not in pages:
            pages.append("contact")
        return pages

    @staticmethod
    def _features(lowered: str, industry: str, site_kind: str) -> list[str]:
        if site_kind == "static_web_app":
            features = ["Login", "Dashboard", "Reports", "Client Notes"]
            if "crm" in lowered:
                features.insert(1, "CRM Pipeline")
            return list(dict.fromkeys(features))
        if "barber" in industry.lower():
            base = ["Signature Cuts", "Beard Detailing", "Hot Towel Service", "Online Booking"]
        elif "restaurant" in industry.lower():
            base = ["Seasonal Menu", "Reservations", "Private Dining", "Chef Specials"]
        elif "it" in industry.lower():
            base = ["Infrastructure Modernization", "Automation Systems", "Security Reviews", "Managed Support"]
        else:
            base = ["Strategy", "Execution", "Automation", "Support"]
        if "gallery" in lowered and "Gallery" not in base:
            base.append("Gallery")
        if "pricing" in lowered and "Pricing" not in base:
            base.append("Pricing")
        return base

    @staticmethod
    def _primary_cta(lowered: str, industry: str, site_kind: str) -> str:
        if site_kind == "static_web_app":
            return "Launch Demo"
        if "barber" in industry.lower() or "booking" in lowered:
            return "Book a Chair"
        if "restaurant" in industry.lower() or "reservation" in lowered:
            return "Reserve a Table"
        if "consult" in industry.lower():
            return "Book a Strategy Call"
        return "Start a Project"

    @staticmethod
    def _acceptance_criteria(spec: WebsiteSpec) -> list[str]:
        if spec.site_kind == "static_web_app":
            return [
                "Includes static app shell, dashboard, reports, client notes, and demo login interaction.",
                "Responsive layout works on desktop and mobile.",
                "Generated files are self-contained and can open from index.html.",
                "README documents prompt-aware choices and run instructions.",
            ]
        return [
            "Generates multiple pages when the prompt asks for pages or sections.",
            "Brand, industry, theme, calls to action, and copy reflect the owner prompt.",
            "Responsive layout works on desktop and mobile.",
            "README documents prompt-aware choices and run instructions.",
        ]

    @staticmethod
    def _palette(spec: WebsiteSpec) -> dict[str, str]:
        palettes = {
            "dark-luxury": {
                "ink": "#f7f0df",
                "muted": "#baa982",
                "paper": "#0b0b0c",
                "panel": "#17130d",
                "accent": "#d7b46a",
                "accent_2": "#f1dfaa",
                "night": "#050505",
                "line": "rgba(215, 180, 106, 0.24)",
                "glow": "rgba(215, 180, 106, 0.2)",
                "wash": "#16110a",
                "font": '"Aptos Display", "Bahnschrift", sans-serif',
            },
            "warm-editorial": {
                "ink": "#2b1b13",
                "muted": "#7a5a43",
                "paper": "#fff7e9",
                "panel": "#efd8b7",
                "accent": "#b94725",
                "accent_2": "#d99441",
                "night": "#30170f",
                "line": "rgba(91, 45, 24, 0.18)",
                "glow": "rgba(185, 71, 37, 0.2)",
                "wash": "#f5dfbf",
                "font": 'Georgia, "Times New Roman", serif',
            },
            "operator-dashboard": {
                "ink": "#eaf8f4",
                "muted": "#93b7ae",
                "paper": "#07100f",
                "panel": "#102320",
                "accent": "#8affdb",
                "accent_2": "#62b9ff",
                "night": "#020706",
                "line": "rgba(138, 255, 219, 0.18)",
                "glow": "rgba(98, 185, 255, 0.16)",
                "wash": "#0c1716",
                "font": '"Aptos Display", "Bahnschrift", sans-serif',
            },
            "minimal-light": {
                "ink": "#1f2933",
                "muted": "#667085",
                "paper": "#f8faf7",
                "panel": "#e9efe6",
                "accent": "#3a7d5e",
                "accent_2": "#c68a28",
                "night": "#102018",
                "line": "rgba(31, 41, 51, 0.12)",
                "glow": "rgba(58, 125, 94, 0.18)",
                "wash": "#edf3ea",
                "font": '"Aptos Display", "Bahnschrift", sans-serif',
            },
        }
        return palettes.get(spec.theme, palettes["minimal-light"])

    @staticmethod
    def _headline(spec: WebsiteSpec) -> str:
        if "barber" in spec.industry.lower():
            return "Sharp cuts, premium detail, zero guesswork."
        if "restaurant" in spec.industry.lower():
            return "A table worth remembering."
        if "crm" in spec.industry.lower():
            return "One command center for every client."
        if "it" in spec.industry.lower():
            return "Secure systems, sharper workflows, calmer operations."
        return "A clearer digital home for serious work."

    @staticmethod
    def _metric_one(spec: WebsiteSpec) -> str:
        if "barber" in spec.industry.lower():
            return "45m"
        if "restaurant" in spec.industry.lower():
            return "7d"
        if "it" in spec.industry.lower():
            return "99.9%"
        return "24h"

    @staticmethod
    def _feature_copy(feature: str, industry: str) -> str:
        lowered = feature.lower()
        if "login" in lowered:
            return "A simple demo authentication action that can later connect to real auth."
        if "dashboard" in lowered:
            return "A central workspace for status, metrics, and next actions."
        if "reports" in lowered:
            return "Readable reporting blocks for revenue, delivery, and performance."
        if "notes" in lowered:
            return "A dedicated area to preserve important client context."
        if "barber" in industry.lower():
            return "A polished service block focused on trust, style, and easy booking."
        if "restaurant" in industry.lower():
            return "A warm hospitality section built to move visitors toward a reservation."
        return "A focused section that turns the offer into a clear action."

    @staticmethod
    def _feature_metric(feature: str) -> str:
        lowered = feature.lower()
        if "login" in lowered:
            return "Auth"
        if "dashboard" in lowered:
            return "Live"
        if "reports" in lowered:
            return "4"
        if "notes" in lowered:
            return "Memo"
        if "pipeline" in lowered:
            return "$42k"
        return "Ready"

    def _page_href(self, cta: str, spec: WebsiteSpec) -> str:
        lowered = cta.lower()
        if "book" in lowered and "booking" in spec.pages:
            return "booking.html"
        if "reserve" in lowered and "reservations" in spec.pages:
            return "reservations.html"
        if "work" in lowered and "gallery" in spec.pages:
            return "gallery.html"
        if "service" in lowered and "services" in spec.pages:
            return "services.html"
        if "contact" in spec.pages:
            return "contact.html"
        return "#"

    @staticmethod
    def _page_title(page: str) -> str:
        return {
            "home": "Home",
            "services": "Services",
            "pricing": "Pricing",
            "gallery": "Gallery",
            "booking": "Booking",
            "menu": "Menu",
            "reservations": "Reservations",
            "about": "About",
            "contact": "Contact",
            "dashboard": "Dashboard",
            "clients": "Clients",
            "reports": "Reports",
            "notes": "Client Notes",
        }.get(page, page.replace("-", " ").title())

    @staticmethod
    def _filename_for_page(page: str) -> str:
        return "index.html" if page == "home" else f"{page}.html"

    @staticmethod
    def _brand_mark(value: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9]+", value)
        if not tokens:
            return "Q"
        return "".join(token[0].upper() for token in tokens[:2])

    @staticmethod
    def _clean_brand(value: str) -> str:
        cleaned = re.sub(r"\b(company|business|firm|website|web app|app|site)\b", "", value, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
        return " ".join(word.upper() if word.upper() in {"AI", "IT", "CRM"} else word.capitalize() for word in cleaned.split())

    @staticmethod
    def _looks_like_industry(value: str) -> bool:
        lowered = value.lower()
        return any(word in lowered for word in ("consulting", "company", "business", "firm", "barber shop", "restaurant"))

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or "website"

    @staticmethod
    def _escape_js(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _js_array(items: list[dict[str, str]]) -> str:
        rows = []
        for item in items:
            pairs = ", ".join(f"{key}: \"{WebsiteGeneratorTool._escape_js(value)}\"" for key, value in item.items())
            rows.append("{" + pairs + "}")
        return "[" + ", ".join(rows) + "]"


class ProjectGeneratorTool:
    definition = ToolDefinition(
        tool_id="code.generate_project",
        name="Generate Project",
        description="Create a prompt-aware script, API, dashboard app, or project scaffold with README and dispatch tracking",
        tier=2,
    )

    def __init__(self, workspace_root: Path, dispatch_service=None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.dispatch_service = dispatch_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction", "")).strip()
        if not instruction:
            raise ValueError("code.generate_project requires an instruction")

        project_type = self._project_type(instruction)
        project_name = str(payload.get("project_name") or self._project_name(instruction, project_type)).strip()
        project_dir = self._project_dir(payload, project_name)
        project_dir.mkdir(parents=True, exist_ok=True)

        files = self._files_for(project_type, project_name, instruction)
        written_files: list[dict[str, Any]] = []
        for relative_path, content in files.items():
            path = project_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written_files.append({"path": str(path), "bytes_written": len(content.encode("utf-8"))})

        summary = f"Created a {project_type.replace('_', ' ')} project for {project_name}."
        dispatch = None
        if self.dispatch_service is not None:
            dispatch = self.dispatch_service.record_completed(
                task_type=project_type,
                project_name=project_name,
                original_prompt=instruction,
                project_path=str(project_dir),
                artifacts=written_files,
                acceptance_criteria=self._acceptance_criteria(project_type),
                summary=summary,
            )

        result = {
            "status": "created",
            "project_type": project_type,
            "project_name": project_name,
            "project_dir": str(project_dir),
            "files": written_files,
            "summary": summary,
            "how_to_run": self._how_to_run(project_type, project_dir),
        }
        if dispatch is not None:
            result["dispatch_id"] = dispatch["id"]
        return result

    def _files_for(self, project_type: str, project_name: str, instruction: str) -> dict[str, str]:
        if project_type == "api":
            return {
                "app.py": self._api_app_py(project_name),
                "README.md": self._project_readme(project_name, project_type, instruction, "python app.py"),
                "tests/test_contract.py": self._api_contract_test(),
            }
        if project_type == "fullstack_app":
            return {
                "index.html": self._dashboard_html(project_name, instruction),
                "styles.css": self._dashboard_css(),
                "app.js": self._dashboard_js(project_name),
                "README.md": self._project_readme(project_name, project_type, instruction, "open index.html"),
            }
        return {
            "src/main.py": self._script_main_py(instruction),
            "README.md": self._project_readme(project_name, "script_tool", instruction, "python src/main.py ."),
            "tests/test_smoke.py": self._script_smoke_test(),
        }

    @staticmethod
    def _project_type(instruction: str) -> str:
        lowered = instruction.lower()
        if any(marker in lowered for marker in ("api", "endpoint", "json endpoint", "health check", "server")):
            return "api"
        if any(marker in lowered for marker in ("dashboard", "crm", "login", "reports", "portal", "app", "saas")):
            return "fullstack_app"
        return "script_tool"

    def _project_name(self, instruction: str, project_type: str) -> str:
        match = re.search(r"\b(?:called|named)\s+(.+?)(?:\s+with\b|$)", instruction, flags=re.IGNORECASE)
        if match:
            return self._title(match.group(1))
        lowered = instruction.lower()
        if project_type == "api":
            if "task" in lowered:
                return "Task Tracker API"
            return "Project Q API"
        if project_type == "fullstack_app":
            if "crm" in lowered:
                return "Project Q CRM"
            return "Project Q Dashboard"
        if "todo" in lowered:
            return "TODO Scanner"
        return "Project Q Script"

    def _project_dir(self, payload: dict[str, Any], project_name: str) -> Path:
        raw_target = str(payload.get("target_dir", "")).strip()
        if raw_target:
            candidate = Path(raw_target)
        else:
            candidate = Path("generated_projects") / self._slug(project_name)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise PermissionError("Generated projects must stay inside the workspace.")
        return resolved

    @staticmethod
    def _script_main_py(instruction: str) -> str:
        lowered = instruction.lower()
        if "todo" in lowered:
            return '''from __future__ import annotations

import argparse
from pathlib import Path


TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".ps1", ".bat", ".sh",
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".project_q"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def scan_todos(root: Path) -> list[tuple[Path, int, str]]:
    matches: list[tuple[Path, int, str]] = []
    for path in iter_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if "TODO" in line.upper():
                matches.append((path, line_number, line.strip()))
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a repository for TODO comments.")
    parser.add_argument("root", nargs="?", default=".", help="Repository root to scan.")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"Root does not exist: {root}")
        return 2
    matches = scan_todos(root)
    if not matches:
        print("No TODO comments found.")
        return 0
    for path, line_number, text in matches:
        print(f"{path}:{line_number}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
        return f'''from __future__ import annotations

from pathlib import Path


def main() -> int:
    print("Project Q generated script")
    print("Request: {ProjectGeneratorTool._escape_py(instruction)}")
    print(f"Running from {{Path.cwd()}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

    @staticmethod
    def _api_app_py(project_name: str) -> str:
        safe_name = ProjectGeneratorTool._escape_py(project_name)
        return f'''from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


TASKS = [
    {{"id": 1, "title": "Review intake", "status": "pending"}},
    {{"id": 2, "title": "Ship first API endpoint", "status": "in_progress"}},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._json({{"status": "ok", "service": "{safe_name}"}})
            return
        if path == "/tasks":
            self._json({{"items": TASKS}})
            return
        self._json({{"error": "not found"}}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/tasks":
            self._json({{"error": "not found"}}, status=404)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{{}}")
        title = str(payload.get("title", "")).strip()
        if not title:
            self._json({{"error": "title is required"}}, status=400)
            return
        item = {{"id": len(TASKS) + 1, "title": title, "status": "pending"}}
        TASKS.append(item)
        self._json(item, status=201)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("Serving {safe_name} on http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
'''

    @staticmethod
    def _dashboard_html(project_name: str, instruction: str) -> str:
        safe_name = html.escape(project_name)
        safe_instruction = html.escape(instruction)
        return f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_name}</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <aside class="sidebar">
      <strong>{safe_name}</strong>
      <a href="#dashboard">Dashboard</a>
      <a href="#reports">Reports</a>
      <a href="#notes">Client Notes</a>
    </aside>
    <main>
      <section class="hero" id="dashboard">
        <p class="eyebrow">Generated Web App</p>
        <h1>Client command center</h1>
        <p>{safe_instruction}</p>
        <button id="demoLogin">Demo Login</button>
      </section>
      <section class="grid">
        <article><span>Pipeline</span><strong>$42k</strong></article>
        <article><span>Reports</span><strong>4</strong></article>
        <article><span>Active Clients</span><strong>18</strong></article>
      </section>
      <section id="reports" class="panel"><h2>Reports</h2><div id="reportsOut"></div></section>
      <section id="notes" class="panel"><h2>Client Notes</h2><textarea rows="8"></textarea></section>
    </main>
    <script src="app.js"></script>
  </body>
</html>
'''

    @staticmethod
    def _dashboard_css() -> str:
        return ''':root {
  --bg: #07100f;
  --panel: #102320;
  --text: #ecfff8;
  --muted: #9bb9af;
  --accent: #8affdb;
  font-family: "Aptos Display", "Bahnschrift", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; grid-template-columns: 16rem 1fr; color: var(--text); background: var(--bg); }
.sidebar { padding: 1.4rem; background: #030807; border-right: 1px solid rgba(138, 255, 219, 0.16); display: flex; flex-direction: column; gap: 1rem; }
a { color: var(--muted); text-decoration: none; font-weight: 800; }
main { padding: clamp(1rem, 4vw, 3rem); }
.hero, .panel, article { padding: 1.2rem; border: 1px solid rgba(138, 255, 219, 0.16); border-radius: 1.4rem; background: var(--panel); }
h1 { margin: 0; font-size: clamp(3rem, 8vw, 6rem); line-height: 0.86; letter-spacing: -0.08em; }
.eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: 0.16em; font-size: 0.75rem; }
button { border: 0; border-radius: 999px; padding: 0.9rem 1.2rem; background: var(--accent); color: #03100d; font-weight: 900; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin: 1rem 0; }
article strong { display: block; margin-top: 0.6rem; font-size: 2rem; }
textarea { width: 100%; border-radius: 1rem; padding: 1rem; color: var(--text); background: #061311; border: 1px solid rgba(138, 255, 219, 0.16); }
@media (max-width: 800px) { body, .grid { grid-template-columns: 1fr; } .sidebar { position: static; } }
'''

    @staticmethod
    def _dashboard_js(project_name: str) -> str:
        return f'''const reports = ["Revenue trend ready", "Client activity ready", "Delivery risks ready"];

function renderReports() {{
  const out = document.querySelector("#reportsOut");
  if (!out) return;
  out.innerHTML = reports.map((report) => `<p>${{report}}</p>`).join("");
}}

document.querySelector("#demoLogin")?.addEventListener("click", () => {{
  document.querySelector("#demoLogin").textContent = "Demo Session Active";
}});

renderReports();
console.log("{ProjectGeneratorTool._escape_js(project_name)} generated by Project Q");
'''

    @staticmethod
    def _project_readme(project_name: str, project_type: str, instruction: str, run_command: str) -> str:
        return f'''# {project_name}

Generated by Project Q.

## Request

```text
{instruction}
```

## Type

{project_type}

## Run

```powershell
{run_command}
```
'''

    @staticmethod
    def _api_contract_test() -> str:
        return '''from app import TASKS


def test_seed_tasks_exist():
    assert TASKS
    assert TASKS[0]["title"]
'''

    @staticmethod
    def _script_smoke_test() -> str:
        return '''from pathlib import Path
from src.main import scan_todos


def test_scan_todos_finds_todo(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text("# TODO: verify generated script\\n", encoding="utf-8")
    assert scan_todos(tmp_path)
'''

    @staticmethod
    def _acceptance_criteria(project_type: str) -> list[str]:
        if project_type == "api":
            return [
                "Creates a runnable stdlib Python API with /health and JSON endpoints.",
                "Handles validation errors with JSON responses.",
                "Includes README run instructions and a contract smoke test.",
            ]
        if project_type == "fullstack_app":
            return [
                "Creates a runnable static dashboard app with HTML, CSS, and JavaScript.",
                "Includes app interactions and responsive layout.",
                "Includes README run instructions.",
            ]
        return [
            "Creates a runnable Python script with pathlib-safe file handling.",
            "Includes README run instructions and a smoke test.",
            "Avoids raw hardcoded Windows paths.",
        ]

    @staticmethod
    def _how_to_run(project_type: str, project_dir: Path) -> str:
        if project_type == "api":
            return f"cd {project_dir}; python app.py"
        if project_type == "fullstack_app":
            return f"Open {project_dir / 'index.html'} in your browser."
        return f"cd {project_dir}; python src/main.py ."

    @staticmethod
    def _title(value: str) -> str:
        return " ".join(word.upper() if word.upper() in {"AI", "API", "CRM"} else word.capitalize() for word in re.findall(r"[A-Za-z0-9]+", value))

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or "project"

    @staticmethod
    def _escape_py(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _escape_js(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
