(() => {
    const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
    const duration = (value) => {
        if (value === null || value === undefined || value === "") return "-";
        const seconds = Math.max(0, Math.floor(Number(value)));
        if (!Number.isFinite(seconds)) return "-";
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const rest = seconds % 60;
        return hours ? `${hours}h ${minutes}m ${rest}s` : minutes ? `${minutes}m ${String(rest).padStart(2, "0")}s` : `${rest}s`;
    };
    const cell = (value, className = "") => `<span class="call-log-cell ${className}" title="${escape(value)}">${escape(value)}</span>`;
    const recording = (doc) => {
        if (!doc.recording_url && !doc.external_recording_url) return cell(__("Not Available"));
        const url = `/api/method/confluence_ai.api.call_log.recording_audio?call_log=${encodeURIComponent(doc.name)}`;
        return `<div class="call-recording"><audio controls preload="none" src="${escape(url)}"
            aria-label="${escape(__("Recording for {0}", [doc.customer_phone || doc.name]))}"></audio>
            <span class="call-recording-error text-muted" hidden>${escape(__("Recording unavailable"))}</span></div>`;
    };

    frappe.listview_settings["AI Call Log"] = {
        add_fields: ["customer_phone", "customer_name", "company", "status", "agent", "duration_sec",
            "ai_disposition", "started_at", "creation", "recording_url", "external_recording_url"],
        hide_name_column: true,
        formatters: {
            agent(value) {
                if (!value) return "-";
                return `<a class="call-log-agent" data-agent="${escape(value)}" title="${escape(value)}"
                    href="/app/ai-agent/${encodeURIComponent(value)}">${escape(value)}</a>`;
            },
        },
        onload(listview) {
            if (listview.call_log_layout_ready) return;
            listview.call_log_layout_ready = true;
            listview.$result.addClass("call-log-table");
            if (!document.getElementById("call-log-table-style")) {
                $(`<style id="call-log-table-style">
                    .call-log-table { overflow-x: auto; }
                    .call-log-table .list-row, .call-log-table .list-row-head { min-width: 1260px; gap: 16px; }
                    .call-log-table .list-row { height: 54px; }
                    .call-log-table .list-row > .level-left,
                    .call-log-table .list-row-head > .list-header-subject {
                        display: grid; grid-template-columns: 170px 120px minmax(150px, 1fr) 90px 95px;
                        gap: 12px; flex: 1; min-width: 673px; margin-right: 0;
                    }
                    .call-log-table .list-row-col { min-width: 0; margin-right: 0; }
                    .call-log-table .list-row > .level-right,
                    .call-log-table .list-row-head > .level-right { flex: 0 0 600px; min-width: 600px; }
                    .call-log-table .call-log-details {
                        display: grid; grid-template-columns: 62px 120px 160px 225px;
                        gap: 11px; align-items: center; width: 600px;
                    }
                    .call-log-table .call-log-cell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
                    .call-log-table .call-recording { width: 225px; }
                    .call-log-table audio { display: block; width: 225px; height: 32px; }
                    .call-log-table .call-recording-heading { display: flex; justify-content: space-between; gap: 8px; }
                    .call-log-table .call-recording-heading .list-count { font-size: 11px; }
                    .call-log-table .list-row-col.hidden-xs { display: block !important; }
                    .call-log-table .list-row-head { height: 40px; }
                    .call-log-table a.call-log-agent { display: block; overflow: hidden; text-overflow: ellipsis; }
                    @media (max-width: 767px) { .call-log-table { -webkit-overflow-scrolling: touch; } }
                </style>`).appendTo(document.head);
            }

            // Keep Frappe's filters, selection, sorting and pagination; change this list only.
            listview.setup_columns = function () {
                this.columns = ["customer_phone", "customer_name", "agent", "company", "status"].map((fieldname, index) => ({
                    type: index ? "Field" : "Subject",
                    df: { ...frappe.meta.get_docfield(this.doctype, fieldname),
                        label: fieldname === "agent" ? __("Agent Name") : frappe.meta.get_docfield(this.doctype, fieldname).label },
                }));
            };
            const originalHeader = listview.get_header_html.bind(listview);
            listview.get_header_html = function () {
                const header = $(originalHeader());
                header.find(".level-right").html(`<div class="call-log-details">
                    <span data-sort-by="duration_sec">${escape(__("Talk Time"))}</span>
                    <span data-sort-by="ai_disposition">${escape(__("Disposition"))}</span>
                    <span data-sort-by="started_at">${escape(__("Time"))}</span>
                    <span class="call-recording-heading">${escape(__("Recording"))}<span class="list-count"></span></span>
                </div>`);
                return header.prop("outerHTML");
            };
            listview.get_right_html = (doc) => {
                const time = frappe.datetime.str_to_user(doc.started_at || doc.creation);
                return `<div class="call-log-details">${cell(duration(doc.duration_sec))}
                    ${cell(doc.ai_disposition || "-")}${cell(time)}${recording(doc)}</div>`;
            };
            const originalRender = listview.render_list.bind(listview);
            listview.render_list = function () {
                this.$result.find("audio").each((_, audio) => audio.pause());
                originalRender();
                this.$result.find(".call-recording").on("click keydown", (event) => event.stopPropagation());
                this.$result.find("audio").on("play", (event) => {
                    this.$result.find("audio").each((_, audio) => {
                        if (audio !== event.currentTarget) audio.pause();
                    });
                }).on("error", (event) => {
                    $(event.currentTarget).hide().siblings(".call-recording-error").prop("hidden", false);
                });
                const names = [...new Set(this.data.map((doc) => doc.agent).filter(Boolean))];
                if (!names.length) return;
                frappe.db.get_list("AI Agent", { fields: ["name", "agent_name"],
                    filters: { name: ["in", names] }, limit: names.length }).then((agents) => {
                    const titles = new Map(agents.map((agent) => [agent.name, agent.agent_name || agent.name]));
                    this.$result.find(".call-log-agent").each((_, element) => {
                        const name = element.dataset.agent;
                        if (titles.has(name)) $(element).text(titles.get(name)).attr("title", `${titles.get(name)} (${name})`);
                    });
                }).catch(() => { /* Keep the permitted Link ID if agent titles are not readable. */ });
            };
            frappe.router.on("change", () => listview.$result.find("audio").each((_, audio) => audio.pause()));
            listview.setup_columns();
            listview.render_header(true);
        },
    };
})();
