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
    const disposition = (doc) => {
        if (doc.ai_disposition) return cell(doc.ai_disposition);
        let label = __("Not classified");
        if (doc.erp_status_update_status === "Failed") label = __("Processing failed");
        else if (doc.transcript_event_status === "Failed") label = __("Transcript failed");
        else if (doc.transcript_event_status === "Pending Matching") label = __("Matching pending");
        else if (doc.transcript_event_status === "Not Received") label = __("Awaiting transcript");
        return cell(label, "text-muted");
    };
    const recording = (doc) => {
        if (!doc.recording_url && !doc.external_recording_url) return cell(__("Not Available"));
        const url = `/api/method/confluence_ai.api.call_log.recording_audio?call_log=${encodeURIComponent(doc.name)}`;
        return `<div class="call-recording"><audio controls preload="none" src="${escape(url)}"
            aria-label="${escape(__("Recording for {0}", [doc.customer_phone || doc.name]))}"></audio>
            <span class="call-recording-error text-muted" hidden>${escape(__("Recording unavailable"))}</span></div>`;
    };
    const eventLabel = (value) => value === "Applied" ? __("Received") : __(value || "Unknown");
    const pauseRecordings = () => $(".call-recording audio").each((_, audio) => audio.pause());
    const preview = (name) => {
        pauseRecordings();
        const dialog = new frappe.ui.Dialog({
            title: __("Call details"), size: "large",
            fields: [{ fieldtype: "HTML", fieldname: "details" }],
            primary_action_label: __("Open call log"),
            primary_action() { dialog.hide(); frappe.set_route("Form", "AI Call Log", name); },
        });
        const wrapper = dialog.fields_dict.details.$wrapper;
        dialog.$wrapper.addClass("call-log-preview");
        let closed = false;
        dialog.$wrapper.on("hidden.bs.modal", () => { closed = true; dialog.$wrapper.remove(); });
        const load = async () => {
            wrapper.text(__("Loading call details..."));
            try {
                // Load private transcript content only when explicitly opened.
                const response = await frappe.db.get_value("AI Call Log", name, [
                    "name", "customer_name", "customer_phone", "started_at", "creation", "duration_sec",
                    "ai_disposition", "erp_status_update_status", "ai_disposition_summary", "ai_disposition_reason",
                    "transcript_summary", "transcript", "initiate_event_status", "hangup_event_status",
                    "recording_event_status", "transcript_event_status",
                ]);
                if (closed) return;
                const doc = response.message;
                if (!doc?.name) throw new Error("Call details unavailable");
                const facts = [
                    [__("Customer"), doc.customer_name || "-"], [__("Phone"), doc.customer_phone || "-"],
                    [__("Date / Time"), frappe.datetime.str_to_user(doc.started_at || doc.creation)],
                    [__("Talk Time"), duration(doc.duration_sec)], [__("Disposition"), doc.ai_disposition || __("Not classified")],
                    [__("ERP update"), doc.erp_status_update_status || __("Not started")],
                ];
                wrapper.html(`<dl class="call-preview-facts">${facts.map(([label, value]) =>
                    `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join("")}</dl>
                    <dl class="call-preview-events">${["initiate", "hangup", "recording", "transcript"].map((kind) =>
                        `<div><dt>${escape(__(kind === "initiate" ? "Initiated" : kind[0].toUpperCase() + kind.slice(1)))}</dt>
                        <dd>${escape(eventLabel(doc[kind + "_event_status"]))}</dd></div>`).join("")}</dl>
                    <h5>${escape(__("Summary"))}</h5><p class="call-preview-text">${escape(doc.ai_disposition_summary || doc.transcript_summary || __("No summary available"))}</p>
                    ${doc.ai_disposition_reason ? `<h5>${escape(__("Disposition reason"))}</h5><p class="call-preview-text">${escape(doc.ai_disposition_reason)}</p>` : ""}
                    <h5>${escape(__("Transcript"))}</h5><div class="call-preview-transcript">${escape(doc.transcript || __("Transcript not available"))}</div>`);
            } catch (_) {
                if (closed) return;
                wrapper.html(`<p>${escape(__("Call details could not be loaded. Check your access or try again."))}</p>
                    <button type="button" class="btn btn-default btn-sm call-preview-retry">${escape(__("Retry"))}</button>`);
                wrapper.find(".call-preview-retry").on("click", load);
            }
        };
        dialog.show();
        load();
        return dialog;
    };

    frappe.listview_settings["AI Call Log"] = {
        add_fields: ["customer_phone", "customer_name", "company", "status", "agent", "duration_sec",
            "ai_disposition", "started_at", "creation", "recording_url", "external_recording_url",
            "transcript_event_status", "erp_status_update_status"],
        hide_name_column: true,
        formatters: {
            started_at(value, df, doc) {
                return escape(frappe.datetime.str_to_user(value || doc.creation));
            },
            customer_phone(value, df, doc) {
                return `<div class="call-log-stacked">${cell(value || "-")}${cell(doc.customer_name || __("Name unavailable"), "text-muted")}</div>`;
            },
            agent(value, df, doc) {
                return `<div class="call-log-stacked">${value ? `<a class="call-log-agent" data-agent="${escape(value)}" title="${escape(value)}"
                    href="/app/ai-agent/${encodeURIComponent(value)}">${escape(value)}</a>` : "-"}${cell(doc.company || "-", "text-muted")}</div>`;
            },
        },
        onload(listview) {
            if (listview.call_log_layout_ready) return;
            listview.call_log_layout_ready = true;
            listview.$result.addClass("call-log-table");
            listview.$result.closest(".page-container").addClass("call-log-wide-page");
            const monitor = $(`<div class="call-log-monitor">
                <span class="call-log-loaded" aria-live="polite"></span>
                <label><input type="checkbox" class="call-log-technical"> ${escape(__("Technical filters"))}</label>
            </div>`).insertBefore(listview.$result);
            const updateFilters = () => {
                const expanded = monitor.find("input").prop("checked");
                for (const fieldname of ["name", "provider", "event_type", "task", "call_uuid", "sip_call_id", "trunk_id"]) {
                    const field = listview.page?.fields_dict?.[fieldname];
                    if (field) field.$wrapper.toggle(Boolean(expanded || field.get_value()));
                }
            };
            monitor.find("input").on("change", updateFilters);
            updateFilters();
            if (!document.getElementById("call-log-table-style")) {
                $(`<style id="call-log-table-style">
                    .call-log-wide-page .page-head > .container,
                    .call-log-wide-page > .container.page-body { width: 100%; max-width: none; }
                    .call-log-wide-page .layout-main-section-wrapper { min-width: 0; }
                    .call-log-table { overflow-x: auto; max-width: 100%; }
                    .call-log-table .list-row-container { padding-left: 0; padding-right: 0; }
                    .call-log-table .list-row-head { margin-left: 0; margin-right: 0; }
                    .call-log-table .list-row, .call-log-table .list-row-head {
                        min-width: 1320px; gap: 12px; padding-left: 10px; padding-right: 10px;
                    }
                    .call-log-table .list-row { height: 72px; }
                    .call-log-table .list-row > .level-left,
                    .call-log-table .list-row-head > .list-header-subject {
                        display: grid; grid-template-columns: 185px 190px minmax(200px, 1fr) 105px;
                        gap: 12px; flex: 1; min-width: 716px; margin-right: 0;
                    }
                    .call-log-table .list-row-col { min-width: 0; margin-right: 0; }
                    .call-log-table .list-row > .level-right,
                    .call-log-table .list-row-head > .level-right { flex: 0 0 572px; min-width: 572px; }
                    .call-log-table .call-log-details {
                        display: grid; grid-template-columns: 80px 180px 240px 36px;
                        gap: 12px; align-items: center; width: 572px;
                    }
                    .call-log-table .call-log-cell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
                    .call-log-table .call-recording { width: 240px; }
                    .call-log-table audio { display: block; width: 240px; height: 36px; }
                    .call-log-table .call-recording-heading { display: flex; justify-content: space-between; gap: 8px; }
                    .call-log-table .call-recording-heading .list-count { font-size: 11px; }
                    .call-log-table .list-row-col.hidden-xs { display: block !important; }
                    .call-log-table .list-row-head { height: 40px; }
                    .call-log-table .list-row-head span { white-space: nowrap; }
                    .call-log-table a.call-log-agent { display: block; overflow: hidden; text-overflow: ellipsis; }
                    .call-log-table .call-log-stacked { display: grid; gap: 4px; min-width: 0; }
                    .call-log-table .call-log-stacked .text-muted { font-size: 12px; }
                    .call-log-table .call-log-error { color: var(--red-600, #b42318); }
                    .call-log-table .call-log-review { width: 34px; height: 34px; padding: 6px; border-radius: 4px; }
                    .call-log-monitor { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
                        gap: 12px; padding: 12px 10px; font-size: 12px; color: var(--text-muted, #667078); }
                    .call-log-monitor label { margin: 0; display: flex; gap: 6px; align-items: center; cursor: pointer; }
                    .call-log-preview .call-preview-facts, .call-log-preview .call-preview-events {
                        display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 20px;
                    }
                    .call-log-preview .call-preview-events { grid-template-columns: repeat(4, minmax(0, 1fr));
                        border-top: 1px solid var(--border-color, #ddd); border-bottom: 1px solid var(--border-color, #ddd); padding: 14px 0; }
                    .call-log-preview dt { font-size: 12px; color: var(--text-muted, #667078); font-weight: 400; }
                    .call-log-preview dd { margin: 4px 0 0; overflow-wrap: anywhere; }
                    .call-log-preview .call-preview-text, .call-log-preview .call-preview-transcript { white-space: pre-wrap; overflow-wrap: anywhere; }
                    .call-log-preview .call-preview-transcript { max-height: 320px; overflow-y: auto; line-height: 1.7; }
                    @media (max-width: 767px) { .call-log-preview .call-preview-facts,
                        .call-log-preview .call-preview-events { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
                    @media (max-width: 767px) { .call-log-table { -webkit-overflow-scrolling: touch; } }
                </style>`).appendTo(document.head);
            }

            // Keep Frappe's filters, selection, sorting and pagination; change this list only.
            listview.setup_columns = function () {
                this.columns = ["started_at", "customer_phone", "agent", "status"].map((fieldname, index) => ({
                    type: index ? "Field" : "Subject",
                    df: { ...frappe.meta.get_docfield(this.doctype, fieldname),
                        label: fieldname === "agent" ? __("Agent / Company") : fieldname === "customer_phone" ? __("Customer") : fieldname === "started_at" ? __("Date / Time") : frappe.meta.get_docfield(this.doctype, fieldname).label },
                }));
            };
            const originalHeader = listview.get_header_html.bind(listview);
            listview.get_header_html = function () {
                const header = $(originalHeader());
                header.find(".level-right").html(`<div class="call-log-details">
                    <span data-sort-by="duration_sec">${escape(__("Talk Time"))}</span>
                    <span data-sort-by="ai_disposition">${escape(__("Disposition"))}</span>
                    <span class="call-recording-heading">${escape(__("Recording"))}<span class="list-count"></span></span>
                    <span></span>
                </div>`);
                return header.prop("outerHTML");
            };
            listview.get_right_html = (doc) => {
                return `<div class="call-log-details">${cell(duration(doc.duration_sec))}
                    <div class="call-log-stacked">${disposition(doc)}${cell(doc.erp_status_update_status === "Failed" ? __("ERP update failed") :
                        `${__("Transcript")}: ${eventLabel(doc.transcript_event_status)}`, doc.erp_status_update_status === "Failed" ? "call-log-error" : "text-muted")}</div>
                    ${recording(doc)}<button type="button" class="btn btn-default btn-xs call-log-review" data-call="${escape(doc.name)}"
                        title="${escape(__("View summary and transcript"))}" aria-label="${escape(__("View details for {0}", [doc.customer_phone || doc.name]))}">
                        ${frappe.utils.icon("message", "sm")}</button></div>`;
            };
            const originalRender = listview.render_list.bind(listview);
            listview.render_list = function () {
                this.$result.find("audio").each((_, audio) => audio.pause());
                originalRender();
                updateFilters();
                const loaded = this.data.length;
                const waiting = this.data.filter((doc) => doc.transcript_event_status === "Not Received" || doc.transcript_event_status === "Pending Matching").length;
                const failed = this.data.filter((doc) => doc.erp_status_update_status === "Failed" || doc.transcript_event_status === "Failed").length;
                monitor.find(".call-log-loaded").text(__("{0} loaded | {1} awaiting transcript | {2} with errors", [loaded, waiting, failed]));
                this.$result.find(".call-log-review").on("click", (event) => {
                    event.stopPropagation();
                    listview.call_preview?.hide();
                    listview.call_preview = preview(event.currentTarget.dataset.call);
                }).on("keydown", (event) => event.stopPropagation());
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
            frappe.router.on("change", () => { pauseRecordings(); listview.call_preview?.hide(); });
            listview.setup_columns();
            listview.render_header(true);
        },
    };
})();
