// Copyright (c) 2026, Google DeepMind and contributors
// For license information, please see license.txt

frappe.ui.form.on('AI Event Route', {
	onload: function(frm) {
		render_webhook_url(frm);
	},
	refresh: function(frm) {
		render_webhook_url(frm);
	},
	sample_json: function(frm) {
		if (!frm.doc.sample_json) {
			return;
		}
		try {
			const data = JSON.parse(frm.doc.sample_json);
			if (typeof data !== 'object' || data === null) {
				return;
			}
			
			// Flatten nested JSON objects (e.g. { "shipping": { "phone": "123" } } -> "shipping.phone": "123")
			const flattened = {};
			function flatten(obj, prefix = '') {
				for (const key in obj) {
					if (Object.prototype.hasOwnProperty.call(obj, key)) {
						const val = obj[key];
						const fullKey = prefix ? `${prefix}.${key}` : key;
						if (val !== null && typeof val === 'object' && !Array.isArray(val)) {
							flatten(val, fullKey);
						} else {
							flattened[fullKey] = val;
						}
					}
				}
			}
			flatten(data);
			
			// Track existing mappings by source_field
			const existing_mappings = {};
			(frm.doc.field_mappings || []).forEach(row => {
				if (row.source_field) {
					existing_mappings[row.source_field] = row;
				}
			});
			
			// Smart mapping suggestions mapping substrings to standard target fields
			const smart_mapping_suggestions = {
				'phone': 'phone',
				'mobile': 'phone',
				'customer_phone': 'phone',
				'customer_mobile': 'phone',
				'to_number': 'phone',
				'name': 'patient_name',
				'customer_name': 'patient_name',
				'patient_name': 'patient_name',
				'address': 'address',
				'delivery_address': 'address',
				'shipping_address': 'address',
				'amount': 'amount_str',
				'payable_amount': 'amount_str',
				'amount_payable': 'amount_str',
				'price': 'amount_str'
			};
			
			let changed = false;
			
			for (const source_field in flattened) {
				const sample_val = String(flattened[source_field]);
				
				if (existing_mappings[source_field]) {
					const row = existing_mappings[source_field];
					if (row.sample_value !== sample_val) {
						frappe.model.set_value(row.doctype, row.name, 'sample_value', sample_val);
						changed = true;
					}
				} else {
					const new_row = frm.add_child('field_mappings');
					new_row.value_type = 'From Payload';
					new_row.source_field = source_field;
					new_row.sample_value = sample_val;
					new_row.transformation = 'None';
					
					// Auto-suggest target_field based on source field name substring
					const lower_key = source_field.toLowerCase().trim();
					for (const suggest_key in smart_mapping_suggestions) {
						if (lower_key.includes(suggest_key)) {
							new_row.target_field = smart_mapping_suggestions[suggest_key];
							break;
						}
					}
					changed = true;
				}
			}
			
			if (changed) {
				frm.refresh_field('field_mappings');
			}
		} catch (e) {
			// Silently ignore parsing errors while the user is actively typing
		}
	}
});

function render_webhook_url(frm) {
	const webhook_url = window.location.origin + "/api/method/confluence_ai.api.webhook.receive_event";
	
	const html_content = `
		<div style="background-color: var(--bg-light-gray, #f8f9fa); border: 1px solid var(--border-color, #d1d8dd); border-radius: 8px; padding: 15px; margin-bottom: 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
			<div style="font-weight: bold; margin-bottom: 8px; font-size: var(--text-md, 14px); color: var(--text-color, #1f2937);">Target Webhook URL</div>
			<div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
				<input type="text" value="${webhook_url}" readonly id="webhook-target-url-input" 
					style="flex: 1; padding: 8px 12px; font-family: monospace; font-size: 13px; border: 1px solid var(--border-color, #d1d8dd); border-radius: 6px; background-color: var(--control-bg, #ffffff); color: var(--text-color, #1f2937);" />
				<button type="button" class="btn btn-default btn-xs" id="btn-copy-webhook-url" 
					style="padding: 8px 16px; font-weight: 500; font-size: 13px; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 4px;">
					Copy URL
				</button>
			</div>
			<div style="font-size: var(--text-xs, 12px); color: var(--text-muted, #6b7280); line-height: 1.5;">
				Configure your external CRM or ERP system to send a <strong>POST</strong> request to this URL when an event occurs.<br/>
				<strong>Required Details:</strong>
				<ul style="margin-top: 4px; padding-left: 20px; margin-bottom: 0;">
					<li><code>Content-Type</code>: <code>application/json</code></li>
					<li>Header <code>X-Webhook-Secret</code>: (Enter the value of <em>Webhook Secret Key</em> below if auth is enabled)</li>
					<li>Body structure: Send a JSON payload containing the event parameter matching the <em>Event Key Field</em> (default: <code>event</code>) and <em>Event Value</em>.</li>
				</ul>
			</div>
		</div>
	`;
	
	frm.get_field("webhook_url_help").$wrapper.html(html_content);
	
	// Bind click event to copy button
	frm.get_field("webhook_url_help").$wrapper.find("#btn-copy-webhook-url").on("click", function() {
		const input = frm.get_field("webhook_url_help").$wrapper.find("#webhook-target-url-input");
		input.select();
		document.execCommand("copy");
		
		const btn = $(this);
		const orig_text = btn.text();
		btn.text("Copied!").addClass("btn-success");
		setTimeout(() => {
			btn.text(orig_text).removeClass("btn-success");
		}, 2000);
	});
}
