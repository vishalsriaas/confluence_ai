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
                    { name: "agent-two", agent_name: "A very long follow-up agent name for narrow screens" }] },
            };
        });
        const native = fs.readFileSync(path.join(frappeApp, "frappe/public/js/frappe/list/list_view.js"), "utf8");
        await page.addScriptTag({ content: native.replace(/^import .*;\r?$/gm, "") });
        await page.addScriptTag({ path: path.join(app, "confluence_ai/confluence_ai/doctype/ai_call_log/ai_call_log_list.js") });
        await page.evaluate(() => {
            document.body.innerHTML = '<h2>AI Call Log</h2><div id="list"></div>';
            window.list = Object.create(frappe.views.ListView.prototype);
            Object.assign(list, { doctype: "AI Call Log", $result: $("#list"), settings: frappe.listview_settings["AI Call Log"],
                data: [
                    { name: "call-one", customer_phone: "+919873090386", customer_name: "Jagmohan", agent: "agent-one", company: "globifit", status: "Completed", duration_sec: 187, ai_disposition: "Follow up", started_at: "08-09-2026 13:44:45", recording_url: "https://never-request.invalid/one" },
                    { name: "call-two", customer_phone: "+919234567890", customer_name: "Customer", agent: "agent-two", company: "globifit", status: "Completed", duration_sec: 0, ai_disposition: "", creation: "08-09-2026 13:43:21", external_recording_url: "https://never-request.invalid/two" },
                    { name: "call-three", customer_phone: "+919876543210", agent: "agent-one", duration_sec: null, ai_disposition: '<img src=x onerror="window.xss=true">', creation: "08-09-2026 13:40:00" },
                ] });
            list.get_column_html = (col, doc) => `<div class="list-row-col ${col.type === "Subject" ? "list-subject" : "hidden-xs"}">${col.type === "Subject" ? '<input type="checkbox">' : ''}${
                col.df.fieldname === "agent" ? list.settings.formatters.agent(doc.agent) : frappe.utils.escape_html(doc[col.df.fieldname] || "-")}</div>`;
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
        const output = process.env.UI_TEST_OUTPUT || process.cwd();
        for (const width of [1440, 390]) {
            await page.setViewportSize({ width, height: 800 });
            const geometry = await page.evaluate(() => {
                const list = document.querySelector("#list");
                const row = document.querySelector(".list-row-container .list-row");
                const left = row.querySelector(".level-left").getBoundingClientRect();
                const right = row.querySelector(".level-right").getBoundingClientRect();
                return { fits: document.documentElement.scrollWidth <= innerWidth, leftEnd: left.right, rightStart: right.left,
                    scrollable: list.scrollWidth > list.clientWidth };
            });
            assert.ok(geometry.fits, "page itself must not overflow");
            assert.ok(geometry.leftEnd <= geometry.rightStart, "columns must not overlap");
            if (width === 390) {
                assert.ok(geometry.scrollable);
                await page.locator("#list").evaluate((el) => { el.scrollLeft = el.scrollWidth; });
            }
            await page.screenshot({ path: path.join(output, `call-log-list-${width}.png`), fullPage: true });
        }
        await page.locator("audio").nth(0).evaluate((el) => el.play());
        await page.locator("audio").nth(1).evaluate((el) => el.play());
        assert.equal(await page.locator("audio").nth(0).evaluate((el) => el.paused), true);
        assert.equal(await page.locator("audio").nth(1).evaluate((el) => el.paused), false);
        await page.evaluate(() => routeChanged());
        assert.equal(await page.locator("audio").nth(1).evaluate((el) => el.paused), true);
        assert.equal(mediaRequests, 2);
        await page.locator("audio").nth(0).evaluate((el) => el.dispatchEvent(new Event("error")));
        assert.equal(await page.locator(".call-recording-error").nth(0).isVisible(), true);
        assert.deepEqual(errors, []);
        console.log("PASS: names, disposition, duration, safe markup, lazy audio, single playback, route pause, error state, desktop/mobile layout.");
    } finally {
        await browser.close();
    }
})().catch((error) => { console.error(error); process.exitCode = 1; });
