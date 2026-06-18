// Copyright (c) 2026, Google DeepMind and contributors
// For license information, please see license.txt

frappe.ui.form.on('AI Agent', {
	// Custom form triggers
});

frappe.ui.form.on('AI Agent MCP Tool', {
	tool: function(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.tool) {
			frappe.db.get_value('AI MCP Tool', row.tool, 'expected_json', (r) => {
				if (r && r.expected_json) {
					frappe.model.set_value(cdt, cdn, 'expected_json', r.expected_json);
				}
			});
		} else {
			frappe.model.set_value(cdt, cdn, 'expected_json', '');
		}
	}
});
