// Browser regression for the list customisation, with Frappe's real list renderers.
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
    const app = path.resolve(__dirname, "../..");
    const frappeApp = path.resolve(app, "../frappe");
    const browser = await chromium.launch({ channel: "msedge", headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let mediaRequests = 0;
    const wav = Buffer.alloc(44 + 16000 * 4);
    wav.write("RIFF"); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
    wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
    wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28);
    wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
    wav.write("data", 36); wav.writeUInt32LE(wav.length - 44, 40);
    await page.route("https://list.test/**", async (route) => {
        if (route.request().url().includes("recording_audio")) {
            mediaRequests++;
            return route.fulfill({ contentType: "audio/wav", body: wav });
        }
        return route.fulfill({ contentType: "text/html", body: "<html><head></head><body></body></html>" });
    });
    try {
        await page.goto("https://list.test/");
        await page.addScriptTag({ path: path.join(frappeApp, "node_modules/jquery/dist/jquery.min.js") });
        await page.addStyleTag({ content: `
            * { box-sizing: border-box; } body { margin: 16px; font: 13px Arial; color: #263238; }
            .level { display: flex; align-items: center; justify-content: space-between; }
            .level-left,.level-right { display: flex; align-items: center; }
            .list-row { padding: 10px 8px; border-bottom: 1px solid #eee; }
            .list-row-head { background: #f5f6f7; color: #666; }
            .list-subject { justify-content: flex-start; gap: 8px; }
            .ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
            a { color: #174e79; text-decoration: none; } .checkbox-actions { display: none; }
            .hidden-xs { display: block; } h2 { font-size: 20px; }
            .container { width: 100%; max-width: 1240px; margin: auto; }
            .page-wrapper { display: flex; gap: 20px; }
            .layout-side-section { flex: 0 0 180px; }
            .layout-main-section-wrapper { flex: 1; }
            .call-log-review svg { width: 18px; height: 18px; }
            .call-log-review { border: 1px solid #ddd; background: #fff; cursor: pointer; }
            .test-dialog { position: fixed; inset: 0; background: #0004; padding: 20px; z-index: 100; }
            .test-dialog-content { max-width: 760px; max-height: 90vh; overflow: auto; padding: 24px;
                background: white; margin: auto; border-radius: 6px; }
            .call-log-preview h5 { font-size: 14px; margin-bottom: 8px; }
            @media (max-width: 767px) { .layout-side-section { display: none; } }
        ` });
        await page.evaluate(() => {
            window.__ = (s, args = []) => s.replace(/\{(\d+)\}/g, (_, i) => args[i]);
            window.frappe = {
                listview_settings: {}, views: { BaseList: class {} }, provide() {},
                utils: { escape_html: (value) => String(value).replace(/[&<>"']/g,
                    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])), icon: () => "" },
                model: { is_numeric_field: () => false },
                meta: { get_docfield: (_, fieldname) => ({ fieldname, label: fieldname.replaceAll("_", " ") }) },
                datetime: { str_to_user: (s) => s },
                router: { on: (_, cb) => { window.routeChanged = cb; } },
                db: { get_list: async () => [{ name: "agent-one", agent_name: "Globifit MI Sales Agent" },
                    { name: "agent-two", agent_name: "A very long follow-up agent name for narrow screens" }],
                    get_value: async (_, name, fields) => {
                        window.previewRequests = (window.previewRequests || 0) + 1;
                        window.previewFields = fields;
                        if (window.previewReject) throw new Error("permission denied");
                        if (window.previewDeferred) return new Promise((resolve) => { window.resolvePreview = resolve; });
                        return { message: { name, customer_name: "Jagmohan", customer_phone: "+919873090386", duration_sec: 187,
                            creation: "08-09-2026 13:44:45", ai_disposition: "Follow up", erp_status_update_status: "Succeeded",
                            initiate_event_status: "Applied", transcript_event_status: "Applied", recording_event_status: "Applied",
                            hangup_event_status: "Applied", transcript: '[CUSTOMER]: Please call tomorrow.\n[AGENT]: Confirmed. <img src=x onerror="window.xss=true">',
                            ai_disposition_summary: "Customer requested a callback tomorrow." } };
                    } },
                set_route: (...args) => { window.lastRoute = args; },
                ui: { Dialog: class {
                    constructor(config) {
                        this.$wrapper = $('<div class="test-dialog" role="dialog"><div class="test-dialog-content"><h2>Call details</h2><div class="details"></div><button class="primary">Open call log</button></div></div>').appendTo(document.body).hide();
                        this.fields_dict = { details: { $wrapper: this.$wrapper.find(".details") } };
                        this.$wrapper.find(".primary").on("click", config.primary_action);
                    }
                    show() { this.$wrapper.show(); }
                    hide() { this.$wrapper.hide().trigger("hidden.bs.modal"); }
                } },
            };
        });
        const messageIcon = fs.readFileSync(path.join(frappeApp, "frappe/public/icons/timeless/message.svg"), "utf8");
        await page.evaluate((svg) => { frappe.utils.icon = () => svg; }, messageIcon);
        const native = fs.readFileSync(path.join(frappeApp, "frappe/public/js/frappe/list/list_view.js"), "utf8");
        await page.addScriptTag({ content: native.replace(/^import .*;\r?$/gm, "") });
        await page.addScriptTag({ path: path.join(app, "confluence_ai/confluence_ai/doctype/ai_call_log/ai_call_log_list.js") });
        await page.evaluate(() => {
            document.body.innerHTML = '<div class="page-container"><div class="page-head"><div class="container"><h2>AI Call Log</h2></div></div><div class="container page-body"><div class="page-wrapper"><aside class="layout-side-section">Filter By</aside><main class="layout-main-section-wrapper"><div id="list"></div></main></div></div></div>';
            window.list = Object.create(frappe.views.ListView.prototype);
            const fields = {};
            for (const fieldname of ["name", "provider", "sip_call_id"]) {
                fields[fieldname] = { $wrapper: $(`<div data-filter-field="${fieldname}">${fieldname}</div>`).prependTo(".page-body"),
                    get_value: () => fieldname === "sip_call_id" ? "active-filter" : "" };
            }
            Object.assign(list, { doctype: "AI Call Log", $result: $("#list"), settings: frappe.listview_settings["AI Call Log"],
                page: { fields_dict: fields },
                data: [
                    { name: "call-one", customer_phone: "+919873090386", customer_name: "Jagmohan", agent: "agent-one", company: "globifit", status: "Completed", duration_sec: 187, ai_disposition: "Follow up", transcript_event_status: "Applied", started_at: "08-09-2026 13:44:45", recording_url: "https://never-request.invalid/one" },
                    { name: "call-two", customer_phone: "+919234567890", customer_name: "Customer", agent: "agent-two", company: "globifit", status: "Completed", duration_sec: 0, ai_disposition: "", transcript_event_status: "Not Received", creation: "08-09-2026 13:43:21", external_recording_url: "https://never-request.invalid/two" },
                    { name: "call-three", customer_phone: "+919876543210", agent: "agent-one", duration_sec: null, ai_disposition: '<img src=x onerror="window.xss=true">', creation: "08-09-2026 13:40:00" },
                ] });
            list.get_column_html = (col, doc) => `<div class="list-row-col ${col.type === "Subject" ? "list-subject" : "hidden-xs"}">${col.type === "Subject" ? '<input type="checkbox">' : ''}${
                list.settings.formatters[col.df.fieldname] ? list.settings.formatters[col.df.fieldname](doc[col.df.fieldname], col.df, doc) : frappe.utils.escape_html(doc[col.df.fieldname] || "-")}</div>`;
            list.render_header = () => { list.$result.find("header").remove(); list.$result.prepend(list.get_header_html()); };
            list.settings.onload(list);
            list.render_list();
        });
        await page.getByText("Globifit MI Sales Agent").first().waitFor();
        assert.equal(mediaRequests, 0, "recordings must not download on page load");
        assert.equal(await page.locator("audio").count(), 2);
        assert.equal(await page.getByText("3m 07s", { exact: true }).count(), 1);
        assert.equal(await page.getByText("0s", { exact: true }).count(), 1);
        assert.equal(await page.locator(".call-log-details img").count(), 0);
        assert.equal(await page.evaluate(() => window.xss), undefined);
        assert.equal(await page.evaluate(() => window.previewRequests || 0), 0, "do not load private transcripts for the full list");
        assert.equal(await page.locator('[data-filter-field="provider"]').isVisible(), false);
        assert.equal(await page.locator('[data-filter-field="sip_call_id"]').isVisible(), true, "never hide an active technical filter");
        await page.locator(".call-log-technical").check();
        assert.equal(await page.locator('[data-filter-field="provider"]').isVisible(), true);
        await page.locator(".call-log-technical").uncheck();
        assert.match(await page.locator(".call-log-loaded").innerText(), /3 loaded.*1 awaiting transcript/);
        assert.equal(await page.getByText("Awaiting transcript", { exact: true }).count(), 1);
        assert.equal(await page.locator(".list-row-container .list-subject").nth(1).innerText(), "08-09-2026 13:43:21");
        assert.equal(await page.evaluate(() => list.columns[0].df.fieldname), "started_at");
        const states = await page.evaluate(() => [
            { transcript_event_status: "Applied" },
            { transcript_event_status: "Failed" },
            { transcript_event_status: "Pending Matching" },
            { erp_status_update_status: "Failed" },
            { ai_disposition: "Follow up", erp_status_update_status: "Failed" },
        ].map((doc) => $(list.get_right_html(doc)).find(".call-log-cell").eq(1).text()));
        assert.deepEqual(states, ["Not classified", "Transcript failed", "Matching pending", "Processing failed", "Follow up"]);
        const output = process.env.UI_TEST_OUTPUT || process.cwd();
        for (const width of [1680, 1440, 390]) {
            await page.setViewportSize({ width, height: 800 });
            await page.locator("#list").evaluate((el) => { el.scrollLeft = 0; });
            const geometry = await page.evaluate(() => {
                const list = document.querySelector("#list");
                const row = document.querySelector(".list-row-container .list-row");
                const left = row.querySelector(".level-left").getBoundingClientRect();
                const right = row.querySelector(".level-right").getBoundingClientRect();
                const body = document.querySelector(".page-body").getBoundingClientRect();
                return { fits: document.documentElement.scrollWidth <= innerWidth, bodyWidth: body.width, leftEnd: left.right, rightStart: right.left,
                    scrollable: list.scrollWidth > list.clientWidth };
            });
            assert.ok(geometry.fits, "page itself must not overflow");
            assert.equal(geometry.bodyWidth, width - 32, "use available page width, not the centered Frappe max-width");
            assert.ok(geometry.leftEnd <= geometry.rightStart, "columns must not overlap");
            if (width === 390) {
                assert.ok(geometry.scrollable);
                await page.locator("#list").evaluate((el) => { el.scrollLeft = el.scrollWidth; });
            }
            await page.screenshot({ path: path.join(output, `call-log-list-${width}.png`), fullPage: true });
        }
        await page.setViewportSize({ width: 1680, height: 800 });
        await page.locator(".layout-side-section").evaluate((el) => { el.style.display = "none"; });
        await page.locator("#list").evaluate((el) => { el.scrollLeft = 0; });
        assert.equal(await page.locator("#list").evaluate((el) => el.scrollWidth <= el.clientWidth), true,
            "all columns should fit on wide desktop with sidebar closed");
        await page.screenshot({ path: path.join(output, "call-log-list-wide.png"), fullPage: true });
        await page.locator(".call-log-review").first().click();
        await page.locator('.test-dialog:visible .call-preview-transcript').waitFor();
        assert.equal(await page.evaluate(() => previewFields.includes("payload_json")), false, "do not download raw prompts/payloads in quick view");
        assert.match(await page.locator('.test-dialog:visible .call-preview-transcript').innerText(), /Please call tomorrow/);
        assert.equal(await page.locator('.test-dialog:visible .call-preview-transcript img').count(), 0);
        await page.screenshot({ path: path.join(output, "call-log-preview-desktop.png"), fullPage: true });
        await page.setViewportSize({ width: 390, height: 800 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({ path: path.join(output, "call-log-preview-mobile.png"), fullPage: true });
        await page.locator('.test-dialog:visible .primary').click();
        assert.deepEqual(await page.evaluate(() => window.lastRoute), ["Form", "AI Call Log", "call-one"]);
        await page.setViewportSize({ width: 1680, height: 800 });
        await page.evaluate(() => { window.previewReject = true; });
        await page.locator(".call-log-review").first().click();
        await page.locator('.test-dialog:visible .call-preview-retry').waitFor();
        await page.evaluate(() => { window.previewReject = false; });
        await page.locator('.test-dialog:visible .call-preview-retry').click();
        await page.locator('.test-dialog:visible .call-preview-transcript').waitFor();
        await page.evaluate(() => list.call_preview.hide());
        await page.evaluate(() => { window.previewDeferred = true; });
        await page.locator(".call-log-review").first().click();
        await page.evaluate(() => { list.call_preview.hide(); window.resolvePreview({ transcript: "late response" }); });
        assert.equal(await page.locator('.test-dialog:visible').count(), 0, "late requests cannot reopen closed dialogs");
        await page.locator("audio").nth(0).evaluate((el) => el.play());
        await page.locator("audio").nth(1).evaluate((el) => el.play());
        assert.equal(await page.locator("audio").nth(0).evaluate((el) => el.paused), true);
        assert.equal(await page.locator("audio").nth(1).evaluate((el) => el.paused), false);
        await page.evaluate(() => routeChanged());
        assert.equal(await page.locator("audio").nth(1).evaluate((el) => el.paused), true);
        assert.equal(mediaRequests, 2);
        await page.locator("audio").nth(0).evaluate((el) => el.dispatchEvent(new Event("error")));
        assert.equal(await page.locator(".call-recording-error").nth(0).isVisible(), true);
        await page.evaluate(() => { list.data = []; list.render_list(); list.settings.onload(list); });
        assert.equal(await page.locator(".list-row-container").count(), 0);
        assert.equal(await page.locator(".call-log-monitor").count(), 1);
        assert.match(await page.locator(".call-log-loaded").innerText(), /0 loaded.*0 awaiting transcript/);
        assert.deepEqual(errors, []);
        console.log("PASS: monitoring counts, active filters, lazy transcript preview, retry, safe markup, empty state, lazy audio, single playback, desktop/mobile layout.");
    } finally {
        await browser.close();
    }
})().catch((error) => { console.error(error); process.exitCode = 1; });
