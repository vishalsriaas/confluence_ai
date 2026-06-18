// Copyright (c) 2026, Google DeepMind and contributors
// For license information, please see license.txt

frappe.ui.form.on('AI MCP Tool', {
	onload: function(frm) {
		frm.trigger('load_schema_cache');
		frm.trigger('toggle_fields');
		update_client_field_suggestions(frm);
		frm.trigger('update_curl_and_json');
		frm.trigger('update_client_doctypes');
	},
	refresh: function(frm) {
		frm.trigger('load_schema_cache');
		frm.trigger('toggle_fields');
		update_client_field_suggestions(frm);
		frm.trigger('update_curl_and_json');
		frm.trigger('update_client_doctypes');

		// 1. Fetch ERP Schema button
		frm.add_custom_button(__('Fetch ERP Schema'), function() {
			if (!frm.doc.server) {
				frappe.msgprint({
					title: __('Validation Error'),
					indicator: 'red',
					message: __('Please select an <strong>MCP Server</strong> first to resolve ERP connection details.')
				});
				return;
			}
			if (!frm.doc.client_doctype) {
				frappe.msgprint({
					title: __('Validation Error'),
					indicator: 'red',
					message: __('Please enter the target <strong>Client DocType</strong> (e.g. Lead, Patient) to introspect.')
				});
				return;
			}

			frappe.call({
				method: 'confluence_ai.api.mcp.fetch_client_schema',
				args: {
					server: frm.doc.server,
					client_doctype: frm.doc.client_doctype
				},
				freeze: true,
				freeze_message: __('Connecting to Client ERP & Introspecting Schema...'),
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						frm.client_fields = r.message;
						frm.set_value('fetched_schema_json', JSON.stringify(r.message));
						frappe.show_alert({
							message: __('Successfully fetched {0} schema fields.').replace('{0}', r.message.length),
							indicator: 'green'
						});
						update_client_field_suggestions(frm);
					} else {
						frappe.msgprint({
							title: __('Warning'),
							indicator: 'orange',
							message: __('No active fields returned for DocType <strong>{0}</strong>. Check connection settings or if DocType exists.').replace('{1}', frm.doc.client_doctype)
						});
					}
				}
			});
		}, __('Actions'));

		// 2. Test Tool Call / Run Manually button
		frm.add_custom_button(__('Test Tool Call'), function() {
			const params = frm.doc.input_parameters || [];
			if (params.length === 0) {
				// Execute immediately
				frm.trigger('execute_test_call', {});
			} else {
				// Prompt for each parameter
				const fields = params.map(p => {
					let fieldtype = 'Data';
					if (p.type === 'integer') fieldtype = 'Int';
					else if (p.type === 'number') fieldtype = 'Float';
					else if (p.type === 'boolean') fieldtype = 'Check';
					
					return {
						fieldname: p.parameter_name,
						label: p.parameter_name,
						fieldtype: fieldtype,
						reqd: p.required ? 1 : 0,
						description: p.description || ''
					};
				});

				frappe.prompt(fields, function(values) {
					frm.trigger('execute_test_call', { arguments: values });
				}, __('Provide Tool Arguments'), __('Run Tool'));
			}
		}, __('Actions'));
	},
	
	execute_test_call: function(frm, args) {
		frappe.call({
			method: 'confluence_ai.api.mcp.test_tool_call',
			args: {
				tool_name: frm.doc.name,
				arguments: args.arguments || {}
			},
			freeze: true,
			freeze_message: __('Executing Tool Call...'),
			callback: function(r) {
				if (r.message && r.message.status === 'success') {
					frappe.msgprint({
						title: __('Success'),
						indicator: 'green',
						message: `<pre style="max-height: 350px; overflow-y: auto; text-align: left; padding: 10px; background-color: #f5f7fa; border-radius: 4px;"><code>${JSON.stringify(r.message.result, null, 2)}</code></pre>`
					});
				} else {
					frappe.msgprint({
						title: __('Error'),
						indicator: 'red',
						message: r.message ? r.message.message : __('Unknown error occurred.')
					});
				}
			}
		});
	},

	load_schema_cache: function(frm) {
		if (frm.doc.fetched_schema_json) {
			try {
				frm.client_fields = JSON.parse(frm.doc.fetched_schema_json);
			} catch (e) {
				frm.client_fields = [];
			}
		}
	},
	
	operation_type: function(frm) {
		frm.trigger('toggle_fields');
		frm.trigger('update_curl_and_json');
	},
	server: function(frm) {
		frm.trigger('update_curl_and_json');
		frm.trigger('update_client_doctypes');
	},
	endpoint_url: function(frm) {
		frm.trigger('update_curl_and_json');
	},
	http_method: function(frm) {
		frm.trigger('update_curl_and_json');
	},
	client_doctype: function(frm) {
		frm.trigger('update_curl_and_json');
	},

	toggle_fields: function(frm) {
		const op = frm.doc.operation_type || 'Read';
		
		// Toggle grid displays based on Operation Type
		frm.toggle_display('match_filters', op === 'Read' || op === 'Update');
		frm.toggle_display('fields_to_read', op === 'Read');
		frm.toggle_display('fields_to_write', op === 'Create' || op === 'Update');
		
		// Adjust writing/update grid label dynamically
		if (op === 'Update') {
			frm.set_df_property('fields_to_write', 'label', __('Fields to Update'));
			frm.set_df_property('fields_to_write', 'description', __('Define field values to update on the matched records.'));
		} else {
			frm.set_df_property('fields_to_write', 'label', __('Fields to Populate'));
			frm.set_df_property('fields_to_write', 'description', __('Define field values to create on the client ERP.'));
		}
	},

	update_curl_and_json: function(frm) {
		// 1. Compile Expected JSON Format
		const args = {};
		(frm.doc.input_parameters || []).forEach(p => {
			args[p.parameter_name] = p.type || 'string';
		});
		frm.set_value('expected_json', JSON.stringify(args, null, 2));

		// 2. Fetch Generated cURL Command
		if (frm.doc.name && !frm.doc.__islocal) {
			frappe.call({
				method: 'confluence_ai.api.mcp.get_generated_curl',
				args: { tool_name: frm.doc.name },
				callback: function(r) {
					if (r.message) {
						frm.set_value('generated_curl', r.message);
					}
				}
			});
		} else {
			frm.set_value('generated_curl', __('Save the tool to generate the manual cURL command...'));
		}
	},

	update_client_doctypes: function(frm) {
		frappe.call({
			method: 'confluence_ai.api.mcp.fetch_client_doctypes',
			args: {
				server: frm.doc.server || null
			},
			callback: function(r) {
				const choices = r.message || [];
				frm.set_df_property('client_doctype', 'options', choices.join('\n'));
				if (frm.fields_dict['client_doctype'] && frm.fields_dict['client_doctype'].set_data) {
					frm.fields_dict['client_doctype'].set_data(choices);
				}
			}
		});
	}
});

// Update input_parameters child table triggers to update dynamically
frappe.ui.form.on('AI MCP Tool Parameter', {
	parameter_name: function(frm) {
		update_client_field_suggestions(frm);
		frm.trigger('update_curl_and_json');
	},
	input_parameters_remove: function(frm) {
		update_client_field_suggestions(frm);
		frm.trigger('update_curl_and_json');
	},
	input_parameters_add: function(frm) {
		update_client_field_suggestions(frm);
		frm.trigger('update_curl_and_json');
	}
});

// Update match_filters & fields_to_write child table triggers to update cURL dynamically
frappe.ui.form.on('AI MCP Tool Field', {
	client_field: function(frm) { frm.trigger('update_curl_and_json'); },
	value_source: function(frm) { frm.trigger('update_curl_and_json'); },
	source_value: function(frm) { frm.trigger('update_curl_and_json'); },
	match_filters_remove: function(frm) { frm.trigger('update_curl_and_json'); },
	match_filters_add: function(frm) { frm.trigger('update_curl_and_json'); },
	fields_to_write_remove: function(frm) { frm.trigger('update_curl_and_json'); },
	fields_to_write_add: function(frm) { frm.trigger('update_curl_and_json'); }
});

// Update fields_to_read child table triggers to update cURL dynamically
frappe.ui.form.on('AI MCP Tool Read Field', {
	client_field: function(frm) { frm.trigger('update_curl_and_json'); },
	fields_to_read_remove: function(frm) { frm.trigger('update_curl_and_json'); },
	fields_to_read_add: function(frm) { frm.trigger('update_curl_and_json'); }
});

function update_client_field_suggestions(frm) {
	// 1. Suggest ERP fields
	const erp_choices = (frm.client_fields || []).map(f => f.fieldname);
	
	// 2. Suggest Tool Argument parameters
	const param_choices = (frm.doc.input_parameters || []).map(p => p.parameter_name).filter(Boolean);

	// Helper to update grid column options and active controls
	const update_grid = (grid_name, fieldname, choices) => {
		if (frm.fields_dict[grid_name] && frm.fields_dict[grid_name].grid) {
			const grid = frm.fields_dict[grid_name].grid;
			grid.update_docfield_property(fieldname, 'options', choices.join('\n'));
			
			// Update active row controls
			if (grid.grid_rows) {
				grid.grid_rows.forEach(row => {
					if (row.fields_dict && row.fields_dict[fieldname]) {
						const ctrl = row.fields_dict[fieldname];
						if (ctrl && ctrl.set_data) {
							ctrl.set_data(choices);
						}
					}
				});
			}
			
			// Update grid form popup dialog control
			if (grid.form && grid.form.fields_dict && grid.form.fields_dict[fieldname]) {
				const ctrl = grid.form.fields_dict[fieldname];
				if (ctrl && ctrl.set_data) {
					ctrl.set_data(choices);
				}
			}
		}
	};

	// Update match_filters
	update_grid('match_filters', 'client_field', erp_choices);
	update_grid('match_filters', 'source_value', param_choices);

	// Update fields_to_read
	update_grid('fields_to_read', 'client_field', erp_choices);

	// Update fields_to_write
	update_grid('fields_to_write', 'client_field', erp_choices);
	update_grid('fields_to_write', 'source_value', param_choices);
}
