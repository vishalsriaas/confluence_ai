frappe.ui.form.on("Order Confirmation Workflow", {
	refresh(frm) {
		clear_order_confirmation_timer(frm);
		update_order_confirmation_timer(frm);
		frm.order_confirmation_timer_interval = setInterval(() => {
			update_order_confirmation_timer(frm);
		}, 1000);
	},
});

function clear_order_confirmation_timer(frm) {
	if (frm.order_confirmation_timer_interval) {
		clearInterval(frm.order_confirmation_timer_interval);
		frm.order_confirmation_timer_interval = null;
	}
}

function update_order_confirmation_timer(frm) {
	const timer = get_order_confirmation_form_timer(frm.doc);
	const field = frm.fields_dict.timer_status;
	if (field && field.$input) {
		field.$input.val(timer.label);
	}
	if (field && field.$wrapper) {
		field.$wrapper.find(".control-value").text(timer.label);
	}

	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(
		`<strong>Timer:</strong> ${frappe.utils.escape_html(timer.label)}`,
		timer.state === "overdue" ? "red" : timer.state === "done" ? "green" : "blue"
	);
}

function get_order_confirmation_form_timer(doc) {
	const final_states = ["Confirmed", "Issue Created", "Level 3 Ticket Created", "Failed", "Cancelled"];
	if (final_states.includes(doc.status)) {
		return { state: "done", label: doc.status };
	}

	let target = null;
	let prefix = "";
	if (doc.status === "Level 1 WhatsApp Sent" && doc.whatsapp_deadline) {
		target = doc.whatsapp_deadline;
		prefix = "WhatsApp reply deadline";
	} else if (["Level 2 Call Queued", "Level 2 Call Missed", "Level 3 Retry Queued"].includes(doc.status) && doc.next_call_time) {
		target = doc.next_call_time;
		prefix = "Next call";
	}

	if (!target) {
		return { state: "idle", label: "No active timer" };
	}

	const seconds = Math.floor((frappe.datetime.str_to_obj(target) - new Date()) / 1000);
	const overdue = seconds < 0;
	return {
		state: overdue ? "overdue" : "running",
		label: `${prefix} ${overdue ? "overdue by" : "in"} ${format_order_confirmation_form_duration(Math.abs(seconds))}`,
	};
}

function format_order_confirmation_form_duration(total_seconds) {
	const seconds = Math.max(0, total_seconds || 0);
	const days = Math.floor(seconds / 86400);
	const hours = Math.floor((seconds % 86400) / 3600);
	const minutes = Math.floor((seconds % 3600) / 60);
	const secs = seconds % 60;

	if (days) return `${days}d ${hours}h ${minutes}m`;
	if (hours) return `${hours}h ${minutes}m ${secs}s`;
	if (minutes) return `${minutes}m ${secs}s`;
	return `${secs}s`;
}
