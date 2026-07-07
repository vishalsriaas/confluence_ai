frappe.listview_settings["Order Confirmation Workflow"] = {
	add_fields: [
		"status",
		"whatsapp_deadline",
		"next_call_time",
		"retry_count",
		"max_retry_count",
		"timer_status",
	],
	formatters: {
		timer_status(value, df, doc) {
			return render_order_confirmation_timer(doc);
		},
	},
	onload(listview) {
		listview.order_confirmation_timer_interval = setInterval(() => {
			if (listview.$result && listview.$result.is(":visible")) {
				listview.refresh();
			}
		}, 30000);
	},
};

function render_order_confirmation_timer(doc) {
	const timer = get_order_confirmation_timer(doc);
	const color = timer.state === "overdue" ? "red" : timer.state === "done" ? "green" : "blue";
	return `<span class="indicator-pill ${color}">${frappe.utils.escape_html(timer.label)}</span>`;
}

function get_order_confirmation_timer(doc) {
	const final_states = ["Confirmed", "Issue Created", "Level 3 Ticket Created", "Failed", "Cancelled"];
	if (final_states.includes(doc.status)) {
		return { state: "done", label: doc.status };
	}

	let target = null;
	let prefix = "";
	if (doc.status === "Level 1 WhatsApp Sent" && doc.whatsapp_deadline) {
		target = doc.whatsapp_deadline;
		prefix = "WhatsApp";
	} else if (["Level 2 Call Queued", "Level 2 Call Missed", "Level 3 Retry Queued"].includes(doc.status) && doc.next_call_time) {
		target = doc.next_call_time;
		prefix = "Call";
	}

	if (!target) {
		return { state: "idle", label: "No active timer" };
	}

	const seconds = Math.floor((frappe.datetime.str_to_obj(target) - new Date()) / 1000);
	const overdue = seconds < 0;
	return {
		state: overdue ? "overdue" : "running",
		label: `${prefix} ${overdue ? "overdue by" : "in"} ${format_order_confirmation_duration(Math.abs(seconds))}`,
	};
}

function format_order_confirmation_duration(total_seconds) {
	const seconds = Math.max(0, total_seconds || 0);
	const days = Math.floor(seconds / 86400);
	const hours = Math.floor((seconds % 86400) / 3600);
	const minutes = Math.floor((seconds % 3600) / 60);
	const secs = seconds % 60;

	if (days) return `${days}d ${hours}h`;
	if (hours) return `${hours}h ${minutes}m`;
	if (minutes) return `${minutes}m ${secs}s`;
	return `${secs}s`;
}
