from __future__ import annotations

import json

import frappe


TENANT_FIELD = "company"
TENANT_DEFAULT_KEY = "confluence_ai_company"
USER_COMPANY_FIELD = "confluence_ai_company"
COMPANY_KEYWORDS = {
	"eternity": ("eternity",),
	"bharat": ("bharat",),
	"sriaas": ("sriaas", "sriaasai", "sr iaas", "shreyas"),
}

TENANT_DOCTYPES = {
	"AI Access Token",
	"AI ACP Event",
	"AI Agent",
	"AI Agent Group",
	"AI Call Log",
	"AI Channel Account",
	"AI Error Log",
	"AI Event Route",
	"AI Knowledge Category",
	"AI Knowledge Chunk",
	"AI Knowledge Document",
	"AI MCP Server",
	"AI MCP Tool",
	"AI Provider Event",
	"AI Sales Call Outcome",
	"AI Sales Disease Route",
	"AI Sales Follow Up",
	"AI Sales Lead",
	"AI Task",
	"AI Task Attempt",
	"AI Task Batch",
	"AI Task Template",
	"AI Tool Permission",
	"AI Webhook Event",
	"AI WhatsApp Template Map",
	"Order Confirmation Workflow",
	"Chat Action Log",
	"Chat AI Suggestion",
	"Chat Assignment Rule",
	"Chat Channel Account",
	"Chat Channel Session",
	"Chat Contact",
	"Chat Contact Channel Profile",
	"Chat Conversation",
	"Chat Message",
	"Chat Queue Event",
	"WA AI Knowledge Base",
	"WA AI Tool Permission",
	"WA Channel Context",
	"WA Channel Pipeline Map",
	"WA Lead AI Insight",
	"WA Lead OCR Result",
	"WA LLM Provider",
	"WA MCP Server",
	"WA MCP Tool Endpoint",
}


def get_current_company(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user in {"Guest", None}:
		return ""
	assigned_company = get_user_assigned_company(user)
	if assigned_company:
		return assigned_company
	return frappe.defaults.get_user_default(TENANT_DEFAULT_KEY, user=user) or ""


def set_current_company(company: str | None) -> None:
	company = (company or "").strip()
	assigned_company = get_user_assigned_company()
	if assigned_company:
		if company and company != assigned_company:
			frappe.throw("You are assigned to one company only and cannot switch company.")
		frappe.defaults.set_user_default(TENANT_DEFAULT_KEY, assigned_company)
		return
	if company:
		if not frappe.db.exists("AI Company", {"name": company, "enabled": 1}):
			frappe.throw(f"AI Company {company} does not exist or is disabled.")
		frappe.defaults.set_user_default(TENANT_DEFAULT_KEY, company)
	else:
		frappe.defaults.clear_user_default(TENANT_DEFAULT_KEY)


def default_company() -> str:
	return get_current_company()


def apply_company_to_doc(doc, method: str | None = None) -> None:
	if doc.doctype not in TENANT_DOCTYPES:
		return
	if not getattr(doc.meta, "has_field", lambda fieldname: False)(TENANT_FIELD):
		return
	if doc.get(TENANT_FIELD):
		return
	company = default_company()
	if company:
		doc.set(TENANT_FIELD, company)


def apply_company_to_blank_doc(doc, method: str | None = None) -> None:
	if doc.doctype not in TENANT_DOCTYPES:
		return
	if not getattr(doc.meta, "has_field", lambda fieldname: False)(TENANT_FIELD):
		return
	assigned_company = get_user_assigned_company()
	if assigned_company:
		if doc.get(TENANT_FIELD) and doc.get(TENANT_FIELD) != assigned_company:
			frappe.throw("You cannot change this record to another company.")
		doc.set(TENANT_FIELD, assigned_company)
		_validate_tenant_links_for_company(doc, assigned_company)
		return
	if doc.get(TENANT_FIELD):
		_validate_tenant_links_for_company(doc, doc.get(TENANT_FIELD))
		return
	company = default_company()
	if company:
		doc.set(TENANT_FIELD, company)
		_validate_tenant_links_for_company(doc, company)


def _tenant_link_fields(meta) -> list:
	return [
		field
		for field in meta.fields
		if field.fieldtype == "Link" and field.options in TENANT_DOCTYPES
	]


def _validate_tenant_links_for_company(doc, company: str) -> None:
	if not company:
		return
	for field in _tenant_link_fields(doc.meta):
		value = doc.get(field.fieldname)
		if not value:
			continue
		linked_company = frappe.db.get_value(field.options, value, TENANT_FIELD)
		if linked_company and linked_company != company:
			frappe.throw(
				f"{field.label or field.fieldname} must belong to company {company}."
			)


def company_query_condition(user: str | None = None) -> str:
	user = user or frappe.session.user
	company = get_current_company(user)
	if not company:
		if user != "Administrator" and "System Manager" not in frappe.get_roles(user):
			return "1 = 0"
		return ""
	escaped = frappe.db.escape(company)
	return f"`tab{{doctype}}`.`company` = {escaped}"


def make_query_condition(doctype: str):
	def _condition(user: str | None = None) -> str:
		condition = company_query_condition(user)
		return condition.replace("{doctype}", doctype) if condition else ""

	return _condition


def _condition_for(doctype: str, user: str | None = None) -> str:
	condition = company_query_condition(user)
	return condition.replace("{doctype}", doctype) if condition else ""


def ai_company_query_condition(user: str | None = None) -> str:
	user = user or frappe.session.user
	assigned_company = get_user_assigned_company(user)
	if assigned_company:
		return f"`tabAI Company`.`name` = {frappe.db.escape(assigned_company)}"
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return ""
	return "1 = 0"


def ai_company_has_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool | None:
	user = user or frappe.session.user
	assigned_company = get_user_assigned_company(user)
	if assigned_company:
		return permission_type == "read" and doc.name == assigned_company
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return True
	return False


def ai_access_token_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Access Token", user)


def ai_acp_event_query_condition(user: str | None = None) -> str:
	return _condition_for("AI ACP Event", user)


def ai_agent_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Agent", user)


def ai_agent_group_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Agent Group", user)


def ai_call_log_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Call Log", user)


def ai_channel_account_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Channel Account", user)


def ai_error_log_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Error Log", user)


def ai_event_route_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Event Route", user)


def ai_knowledge_category_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Knowledge Category", user)


def ai_knowledge_chunk_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Knowledge Chunk", user)


def ai_knowledge_document_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Knowledge Document", user)


def ai_mcp_server_query_condition(user: str | None = None) -> str:
	return _condition_for("AI MCP Server", user)


def ai_mcp_tool_query_condition(user: str | None = None) -> str:
	return _condition_for("AI MCP Tool", user)


def ai_provider_event_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Provider Event", user)


def ai_sales_call_outcome_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Sales Call Outcome", user)


def ai_sales_disease_route_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Sales Disease Route", user)


def ai_sales_follow_up_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Sales Follow Up", user)


def ai_sales_lead_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Sales Lead", user)


def ai_task_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Task", user)


def ai_task_attempt_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Task Attempt", user)


def ai_task_batch_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Task Batch", user)


def ai_task_template_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Task Template", user)


def ai_tool_permission_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Tool Permission", user)


def ai_webhook_event_query_condition(user: str | None = None) -> str:
	return _condition_for("AI Webhook Event", user)


def ai_whatsapp_template_map_query_condition(user: str | None = None) -> str:
	return _condition_for("AI WhatsApp Template Map", user)


def order_confirmation_workflow_query_condition(user: str | None = None) -> str:
	return _condition_for("Order Confirmation Workflow", user)


ORDER_CONFIRMATION_CHILD_DOCTYPE = "Order Confirmation Company Setting"
ORDER_CONFIRMATION_SETTINGS_DOCTYPE = "Order Confirmation Settings"
ORDER_CONFIRMATION_LINK_FIELDS = {
	"channel_account": "AI Channel Account",
	"wa_chat_channel_account": "Chat Channel Account",
	"voice_agent": "AI Agent",
	"whatsapp_task_template": "AI Task Template",
	"voice_task_template": "AI Task Template",
}


def _order_settings_scalar_fields() -> list[str]:
	meta = frappe.get_meta(ORDER_CONFIRMATION_SETTINGS_DOCTYPE)
	return [
		field.fieldname
		for field in meta.fields
		if field.fieldname
		and field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect"}
	]


def _order_settings_child_fields() -> list[str]:
	meta = frappe.get_meta(ORDER_CONFIRMATION_CHILD_DOCTYPE)
	return [
		field.fieldname
		for field in meta.fields
		if field.fieldname
		and field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect"}
	]


def _existing_order_settings_values() -> dict:
	rows = frappe.db.sql(
		"""
		select field, value
		from `tabSingles`
		where doctype = %s
		""",
		(ORDER_CONFIRMATION_SETTINGS_DOCTYPE,),
		as_dict=True,
	)
	return {row.field: row.value for row in rows}


def _existing_order_company_rows() -> list[dict]:
	fields = ["name", "idx", *_order_settings_child_fields()]
	return frappe.get_all(
		ORDER_CONFIRMATION_CHILD_DOCTYPE,
		filters={"parent": ORDER_CONFIRMATION_SETTINGS_DOCTYPE},
		fields=list(dict.fromkeys(fields)),
		order_by="idx asc",
		limit_page_length=1000,
	)


def _validate_order_setting_links(row, company: str) -> None:
	for fieldname, linked_doctype in ORDER_CONFIRMATION_LINK_FIELDS.items():
		value = row.get(fieldname)
		if not value:
			continue
		linked_company = frappe.db.get_value(linked_doctype, value, TENANT_FIELD)
		if linked_company and linked_company != company:
			frappe.throw(f"{fieldname.replace('_', ' ').title()} must belong to company {company}.")


def protect_order_confirmation_settings(doc, method: str | None = None) -> None:
	assigned_company = get_user_assigned_company()
	if not assigned_company:
		return

	old_values = _existing_order_settings_values()
	for fieldname in _order_settings_scalar_fields():
		if fieldname in old_values:
			doc.set(fieldname, old_values[fieldname])

	child_fields = _order_settings_child_fields()
	existing_rows = _existing_order_company_rows()
	other_rows = [row for row in existing_rows if row.get(TENANT_FIELD) != assigned_company]
	incoming_own_rows = [
		row for row in (doc.get("company_settings") or []) if not row.get(TENANT_FIELD) or row.get(TENANT_FIELD) == assigned_company
	]
	existing_own_rows = [row for row in existing_rows if row.get(TENANT_FIELD) == assigned_company]

	if not incoming_own_rows and existing_own_rows:
		incoming_own_rows = existing_own_rows

	doc.set("company_settings", [])
	for row in other_rows:
		doc.append(
			"company_settings",
			{fieldname: row.get(fieldname) for fieldname in child_fields if fieldname in row},
		)

	for row in incoming_own_rows:
		row_data = {fieldname: row.get(fieldname) for fieldname in child_fields}
		row_data[TENANT_FIELD] = assigned_company
		_validate_order_setting_links(row_data, assigned_company)
		doc.append("company_settings", row_data)


@frappe.whitelist()
def get_company_options() -> dict:
	assigned_company = get_user_assigned_company()
	filters = {"enabled": 1}
	if assigned_company:
		filters["name"] = assigned_company

	companies = frappe.get_all(
		"AI Company",
		filters=filters,
		fields=["name", "company_name", "company_key"],
		order_by="company_name asc",
	)
	return {
		"current_company": get_current_company(),
		"companies": companies,
		"locked": bool(assigned_company),
	}


@frappe.whitelist()
def set_user_company(company: str | None = None) -> dict:
	set_current_company(company)
	return {"current_company": get_current_company(), "locked": bool(get_user_assigned_company())}


def get_user_assigned_company(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user in {"Guest", None}:
		return ""
	if not frappe.db.has_column("User", USER_COMPANY_FIELD):
		return ""
	return frappe.db.get_value("User", user, USER_COMPANY_FIELD) or ""


def set_user_assigned_company(user: str, company: str) -> dict:
	if not user:
		frappe.throw("User is required.")
	if not frappe.db.exists("User", user):
		frappe.throw(f"User {user} does not exist.")
	if not frappe.db.exists("AI Company", {"name": company, "enabled": 1}):
		frappe.throw(f"AI Company {company} does not exist or is disabled.")
	frappe.db.set_value("User", user, USER_COMPANY_FIELD, company)
	frappe.defaults.set_user_default(TENANT_DEFAULT_KEY, company, user=user)
	frappe.clear_cache(user=user)
	frappe.db.commit()
	return {"user": user, "company": company}


def _has_tenant_field(doctype: str) -> bool:
	if not frappe.db.exists("DocType", doctype):
		return False
	meta = frappe.get_meta(doctype)
	return bool(meta.has_field(TENANT_FIELD))


def _table_name(doctype: str) -> str:
	return f"`tab{doctype.replace('`', '')}`"


def backfill_blank_company(company: str = "sriaas", dry_run: int = 0) -> dict:
	company = (company or "").strip()
	if not company:
		frappe.throw("Company is required.")
	if not frappe.db.exists("AI Company", {"name": company, "enabled": 1}):
		frappe.throw(f"AI Company {company} does not exist or is disabled.")

	summary = []
	total_blank = 0
	total_updated = 0

	for doctype in sorted(TENANT_DOCTYPES):
		if not _has_tenant_field(doctype):
			continue

		table = _table_name(doctype)
		blank_count = frappe.db.sql(
			f"select count(*) from {table} where coalesce({TENANT_FIELD}, '') = ''"
		)[0][0]
		total_blank += blank_count

		updated = 0
		if blank_count and not int(dry_run):
			frappe.db.sql(
				f"""
				update {table}
				set {TENANT_FIELD} = %s
				where coalesce({TENANT_FIELD}, '') = ''
				""",
				(company,),
			)
			updated = blank_count
			total_updated += updated

		summary.append({"doctype": doctype, "blank": blank_count, "updated": updated})

	if not int(dry_run):
		if frappe.db.exists("DocType", "Order Confirmation Settings"):
			settings = frappe.get_single("Order Confirmation Settings")
			if hasattr(settings, "default_company") and not settings.default_company:
				settings.default_company = company
				settings.save(ignore_permissions=True)

		frappe.db.commit()
		frappe.clear_cache()

	return {
		"company": company,
		"dry_run": bool(int(dry_run)),
		"total_blank": total_blank,
		"total_updated": total_updated,
		"details": summary,
	}


def _company_search_fields(doctype: str) -> list[str]:
	meta = frappe.get_meta(doctype)
	searchable_types = {
		"Data",
		"Small Text",
		"Text",
		"Long Text",
		"Text Editor",
		"Code",
		"Link",
		"Dynamic Link",
		"Select",
		"Read Only",
	}
	fields = ["name"]
	for field in meta.fields:
		if field.fieldname == TENANT_FIELD:
			continue
		if field.fieldtype in searchable_types:
			fields.append(field.fieldname)
	return list(dict.fromkeys(fields))


def _infer_company_from_row(row: dict, fields: list[str]) -> str:
	haystack = " ".join(str(row.get(fieldname) or "") for fieldname in fields).lower()
	for company, keywords in COMPANY_KEYWORDS.items():
		if any(keyword in haystack for keyword in keywords):
			return company
	return ""


def align_records_by_company(default_company: str = "sriaas", dry_run: int = 0) -> dict:
	default_company = (default_company or "").strip()
	if not frappe.db.exists("AI Company", {"name": default_company, "enabled": 1}):
		frappe.throw(f"AI Company {default_company} does not exist or is disabled.")

	total_checked = 0
	total_changed = 0
	by_company = {company: 0 for company in COMPANY_KEYWORDS}
	by_company.setdefault(default_company, 0)
	details = []

	for doctype in sorted(TENANT_DOCTYPES):
		if not _has_tenant_field(doctype):
			continue

		fields = _company_search_fields(doctype)
		rows = frappe.get_all(
			doctype,
			fields=list(dict.fromkeys(fields + [TENANT_FIELD])),
			limit_page_length=100000,
		)
		changed = 0
		inferred_counts = {company: 0 for company in COMPANY_KEYWORDS}
		inferred_counts.setdefault(default_company, 0)

		for row in rows:
			total_checked += 1
			inferred = _infer_company_from_row(row, fields) or default_company
			inferred_counts[inferred] = inferred_counts.get(inferred, 0) + 1
			by_company[inferred] = by_company.get(inferred, 0) + 1

			if row.get(TENANT_FIELD) == inferred:
				continue

			changed += 1
			total_changed += 1
			if not int(dry_run):
				frappe.db.set_value(doctype, row.name, TENANT_FIELD, inferred, update_modified=False)

		details.append(
			{
				"doctype": doctype,
				"checked": len(rows),
				"changed": changed,
				"inferred": inferred_counts,
			}
		)

	if not int(dry_run):
		frappe.db.commit()
		frappe.clear_cache()

	return {
		"default_company": default_company,
		"dry_run": bool(int(dry_run)),
		"total_checked": total_checked,
		"total_changed": total_changed,
		"assigned_by_company": by_company,
		"details": details,
	}


def install_company_switcher_workspace() -> dict:
	block_name = "Company Switcher"
	updated = []
	for workspace_name in frappe.get_all(
		"Workspace",
		filters={"label": ["in", ["Confluence AI", "WhatsApp"]]},
		pluck="name",
	):
		workspace = frappe.get_doc("Workspace", workspace_name)
		before_blocks = len(workspace.custom_blocks or [])
		workspace.custom_blocks = [
			row for row in (workspace.custom_blocks or []) if row.custom_block_name != block_name
		]

		content = json.loads(workspace.content or "[]")
		content = [
			item
			for item in content
			if item.get("type") != "custom_block"
			or item.get("data", {}).get("custom_block_name") != block_name
		]
		if len(workspace.custom_blocks or []) != before_blocks or workspace.content != json.dumps(content):
			workspace.content = json.dumps(content)
			workspace.save(ignore_permissions=True)
			updated.append(workspace_name)

	if frappe.db.exists("Custom HTML Block", block_name):
		frappe.db.set_value("Custom HTML Block", block_name, "private", 1, update_modified=False)

	frappe.clear_cache()
	return {"removed_block": block_name, "workspaces": updated}

	html = """
<div class="company-switcher-card">
  <div>
    <div class="company-switcher-label">Company</div>
    <div class="company-switcher-help">Select active company for Confluence AI and WhatsApp data.</div>
  </div>
  <select class="company-switcher-select">
    <option value="">All Companies</option>
  </select>
</div>
""".strip()
	style = """
.company-switcher-card {
  align-items: center;
  background: #fff;
  border: 1px solid #d8dfe6;
  border-radius: 8px;
  display: none;
  gap: 16px;
  justify-content: space-between;
  margin: 0 0 14px;
  padding: 12px 14px;
}
.company-switcher-label {
  color: #1f272e;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.2;
}
.company-switcher-help {
  color: #6b7280;
  font-size: 12px;
  margin-top: 2px;
}
.company-switcher-select {
  background: #f8fafc;
  border: 1px solid #cfd7df;
  border-radius: 6px;
  color: #1f272e;
  font-size: 13px;
  height: 34px;
  min-width: 220px;
  padding: 0 10px;
}
""".strip()
	script = """
const select = root_element.querySelector('.company-switcher-select');
const storageKey = 'confluence_ai_company';
let headerSelect = null;

function headerTarget() {
  const searchInput = document.querySelector(
    '.navbar input[type="search"], .navbar input[placeholder*="Search"], input[placeholder*="Search or type"]'
  );
  const searchBox = searchInput && (
    searchInput.closest('.search-bar') ||
    searchInput.closest('.awesomplete') ||
    searchInput.closest('.input-group') ||
    searchInput.parentElement
  );
  if (searchBox && searchBox.parentElement) {
    return { parent: searchBox.parentElement, before: searchBox };
  }

  const navbar = document.querySelector('.navbar, header .navbar');
  return { parent: navbar || document.body, before: null };
}

function ensureHeaderSelect() {
  let wrapper = document.querySelector('.tenant-company-header-switcher');
  if (!wrapper) {
    wrapper = document.createElement('div');
    wrapper.className = 'tenant-company-header-switcher';
    wrapper.style.display = 'inline-flex';
    wrapper.style.alignItems = 'center';
    wrapper.style.gap = '8px';
    wrapper.style.marginLeft = '8px';
    wrapper.style.marginRight = '8px';
    wrapper.style.padding = '4px 8px';
    wrapper.style.border = '1px solid #d8dfe6';
    wrapper.style.borderRadius = '8px';
    wrapper.style.background = '#fff';
    wrapper.style.zIndex = '30';

    const label = document.createElement('span');
    label.textContent = 'Company';
    label.style.fontSize = '12px';
    label.style.fontWeight = '650';
    label.style.color = '#374151';

    headerSelect = document.createElement('select');
    headerSelect.className = 'tenant-company-header-select';
    headerSelect.style.height = '30px';
    headerSelect.style.minWidth = '170px';
    headerSelect.style.border = '1px solid #cfd7df';
    headerSelect.style.borderRadius = '6px';
    headerSelect.style.background = '#f8fafc';
    headerSelect.style.fontSize = '13px';
    headerSelect.style.padding = '0 8px';

    wrapper.appendChild(label);
    wrapper.appendChild(headerSelect);
    const target = headerTarget();
    target.parent.insertBefore(wrapper, target.before);
  } else {
    headerSelect = wrapper.querySelector('.tenant-company-header-select');
  }
  return headerSelect;
}

function renderOptions(data) {
  const companies = data.companies || [];
  const current = data.current_company || '';
  const locked = !!data.locked;
  select.innerHTML = '<option value="">All Companies</option>';
  const header = ensureHeaderSelect();
  header.innerHTML = '<option value="">All Companies</option>';
  companies.forEach((company) => {
    const option = document.createElement('option');
    option.value = company.name;
    option.textContent = company.company_name || company.name;
    select.appendChild(option);
    header.appendChild(option.cloneNode(true));
  });
  select.value = current;
  header.value = current;
  select.disabled = locked;
  header.disabled = locked;
  header.title = locked ? 'Company access is assigned by admin' : '';
  if (current) {
    window.localStorage.setItem(storageKey, current);
  }
}

frappe.call({
  method: 'confluence_ai.tenant.get_company_options',
  callback(response) {
    renderOptions(response.message || {});
  }
});

function switchCompany(value, label) {
  frappe.call({
    method: 'confluence_ai.tenant.set_user_company',
    args: { company: value },
    callback() {
      window.localStorage.setItem(storageKey, value || '');
      frappe.show_alert({
        message: value ? `Company switched to ${label}` : 'Company filter cleared',
        indicator: 'green'
      });
      const route = frappe.get_route ? frappe.get_route() : [];
      if (window.cur_list && cur_list.refresh) {
        cur_list.refresh();
      } else if (route[0] === 'List') {
        frappe.set_route(route);
      } else if (window.cur_frm && cur_frm.doc && !cur_frm.is_dirty()) {
        cur_frm.reload_doc();
      }
    }
  });
}

select.addEventListener('change', () => {
  switchCompany(select.value, select.options[select.selectedIndex].text);
});

setTimeout(() => {
  const header = ensureHeaderSelect();
  header.addEventListener('change', () => {
    switchCompany(header.value, header.options[header.selectedIndex].text);
  });
}, 0);

setTimeout(() => {
  const header = ensureHeaderSelect();
  if (header && select.options.length) {
    header.innerHTML = select.innerHTML;
    header.value = select.value;
  }
}, 600);
""".strip()

	if frappe.db.exists("Custom HTML Block", block_name):
		block = frappe.get_doc("Custom HTML Block", block_name)
		block.html = html
		block.style = style
		block.script = script
		block.private = 0
		block.save(ignore_permissions=True)
	else:
		block = frappe.get_doc(
			{
				"doctype": "Custom HTML Block",
				"name": block_name,
				"html": html,
				"style": style,
				"script": script,
				"private": 0,
			}
		)
		block.insert(ignore_permissions=True)

	updated = []
	for workspace_name in frappe.get_all(
		"Workspace",
		filters={"label": ["in", ["Confluence AI", "WhatsApp"]]},
		pluck="name",
	):
		workspace = frappe.get_doc("Workspace", workspace_name)
		if not any(row.custom_block_name == block_name for row in workspace.custom_blocks):
			workspace.append("custom_blocks", {"custom_block_name": block_name, "label": block_name})

		content = json.loads(workspace.content or "[]")
		content = [
			item
			for item in content
			if item.get("type") != "custom_block"
			or item.get("data", {}).get("custom_block_name") != block_name
		]
		content.insert(0, {"type": "custom_block", "data": {"custom_block_name": block_name}})
		workspace.content = json.dumps(content)
		workspace.save(ignore_permissions=True)
		updated.append(workspace_name)

	frappe.clear_cache()
	return {"block": block_name, "workspaces": updated}

def chat_action_log_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Action Log", user)


def chat_ai_suggestion_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat AI Suggestion", user)


def chat_assignment_rule_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Assignment Rule", user)


def chat_channel_account_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Channel Account", user)


def chat_channel_session_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Channel Session", user)


def chat_contact_channel_profile_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Contact Channel Profile", user)


def chat_queue_event_query_condition(user: str | None = None) -> str:
	return _condition_for("Chat Queue Event", user)


def wa_ai_knowledge_base_query_condition(user: str | None = None) -> str:
	return _condition_for("WA AI Knowledge Base", user)


def wa_ai_tool_permission_query_condition(user: str | None = None) -> str:
	return _condition_for("WA AI Tool Permission", user)


def wa_channel_context_query_condition(user: str | None = None) -> str:
	return _condition_for("WA Channel Context", user)


def wa_channel_pipeline_map_query_condition(user: str | None = None) -> str:
	return _condition_for("WA Channel Pipeline Map", user)


def wa_lead_ai_insight_query_condition(user: str | None = None) -> str:
	return _condition_for("WA Lead AI Insight", user)


def wa_lead_ocr_result_query_condition(user: str | None = None) -> str:
	return _condition_for("WA Lead OCR Result", user)


def wa_llm_provider_query_condition(user: str | None = None) -> str:
	return _condition_for("WA LLM Provider", user)


def wa_mcp_server_query_condition(user: str | None = None) -> str:
	return _condition_for("WA MCP Server", user)


def wa_mcp_tool_endpoint_query_condition(user: str | None = None) -> str:
	return _condition_for("WA MCP Tool Endpoint", user)
