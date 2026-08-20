// Copyright (c) 2026, Google DeepMind and contributors
// For license information, please see license.txt

frappe.ui.form.on('AI Agent', {
	refresh: function(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__('Apply Voice Settings'), function() {
				const apply_settings = () => {
					frappe.call({
						method: 'confluence_ai.confluence_ai.doctype.ai_agent.ai_agent.apply_voice_environment_settings',
						args: {
							agent_name: frm.doc.name
						},
						freeze: true,
						freeze_message: __('Applying voice settings...'),
						callback: function(r) {
							const result = r.message || {};
							frappe.show_alert({
								message: result.enabled
									? __('Voice settings applied: {0} at {1}%', [result.background_sound, result.background_volume])
									: __('Voice background sound disabled for this agent'),
								indicator: 'green'
							});
							frm.reload_doc();
						}
					});
				};

				if (frm.is_dirty()) {
					frm.save().then(apply_settings);
				} else {
					apply_settings();
				}
			}, __('Voice'));
		}

		if (frm.is_new()) {
			frm.add_custom_button(__('Clone from Existing'), function() {
				const dialog = new frappe.ui.Dialog({
					title: __('Clone from Existing Agent'),
					fields: [
						{
							label: __('Source Agent'),
							fieldname: 'source_agent',
							fieldtype: 'Link',
							options: 'AI Agent',
							reqd: 1
						}
					],
					primary_action_label: __('Clone'),
					primary_action(values) {
						frappe.db.get_doc('AI Agent', values.source_agent).then(doc => {
							const exclude_fields = ['name', 'amended_from'];

							// Clear child tables first
							frm.clear_table('allowed_mcp_tools');
							frm.clear_table('stage_prompts');

							// Map fields dynamically based on DocType metadata
							for (let key in doc) {
								if (exclude_fields.includes(key)) continue;
								if (!frappe.meta.has_field(frm.doctype, key)) continue;

								if (Array.isArray(doc[key])) {
									// Copy child tables
									const df = frappe.meta.get_docfield(frm.doctype, key);
									const child_doctype = df ? df.options : null;

									doc[key].forEach(row => {
										const child = frm.add_child(key);
										for (let row_key in row) {
											if (exclude_fields.includes(row_key)) continue;
											if (child_doctype && !frappe.meta.has_field(child_doctype, row_key)) continue;

											child[row_key] = row[row_key];
										}
									});
								} else {
									frm.set_value(key, doc[key]);
								}
							}
							frm.refresh();
							dialog.hide();
							frappe.show_alert({
								message: __('Copied configuration from {0}', [values.source_agent]),
								indicator: 'green'
							});
						});
					}
				});
				dialog.show();
			});
		}
	}
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
