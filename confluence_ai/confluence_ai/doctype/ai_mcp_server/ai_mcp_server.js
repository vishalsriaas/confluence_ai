// Copyright (c) 2026, Google DeepMind and contributors
// For license information, please see license.txt

frappe.ui.form.on('AI MCP Server', {
	refresh: function(frm) {
		frm.add_custom_button(__('Test Connection'), function() {
			if (!frm.doc.server_url) {
				frappe.msgprint({
					title: __('Validation Error'),
					indicator: 'red',
					message: __('Please enter the <strong>Frappe URL</strong> first.')
				});
				return;
			}
			
			// Call the backend connection tester
			frappe.call({
				method: 'confluence_ai.api.mcp.test_server_connection',
				args: {
					server_name: frm.doc.name || null,
					server_url: frm.doc.server_url,
					api_key: frm.doc.api_key || null,
					api_secret: frm.doc.api_secret || null,
					bearer_token: frm.doc.bearer_token || null
				},
				freeze: true,
				freeze_message: __('Testing connection to Frappe server...'),
				callback: function(r) {
					if (r.message && r.message.status === 'success') {
						frappe.msgprint({
							title: __('Connection Succeeded'),
							indicator: 'green',
							message: r.message.message
						});
					} else {
						frappe.msgprint({
							title: __('Connection Failed'),
							indicator: 'red',
							message: r.message ? r.message.message : __('Unknown connection error.')
						});
					}
				}
			});
		});
	}
});
