from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import add_to_date, now_datetime

from confluence_ai.api.mcp import execute_mcp_tool
from confluence_ai.services.dispatcher import enqueue_task_execution, refresh_batch_counts
from confluence_ai.services.utils import as_json, create_error, parse_json_object

WORKFLOW = "Order Confirmation Workflow"
SETTINGS = "Order Confirmation Settings"

FINAL_STATES = {"Confirmed", "Issue Created", "Level 3 Ticket Created", "Failed", "Cancelled"}
STOP_CONFIRMATION_STATES = {"Correction Pending", "Issue Created", "Level 3 Ticket Created", "Failed", "Cancelled"}


def ensure_defaults() -> None:
	"""Create baseline settings and task templates for the reusable order confirmation flow."""
	_ensure_task_template("order_confirmation_whatsapp", "Order Confirmation WhatsApp", "WhatsApp")
	_ensure_task_template("order_confirmation_voice", "Order Confirmation Voice", "Voice")

	settings = frappe.get_single(SETTINGS)
	changed = False
	defaults = {
		"enabled": 1,
		"default_whatsapp_wait_minutes": 10,
		"default_retry_delay_minutes": 60,
		"voice_call_timeout_minutes": 5,
		"max_voice_attempts": 2,
		"level_3_issue_tag": "Level 3 Call Missed Order confirmation",
		"send_first_whatsapp_as_template": 1,
		"whatsapp_template_language": "en",
		"confirm_mcp_tool_name": "update_patient_encounter_order_confirmation",
		"issue_mcp_tool_name": "create_order_confirmation_issue",
		"whatsapp_task_template": _template_by_key("order_confirmation_whatsapp"),
		"voice_task_template": _template_by_key("order_confirmation_voice"),
	}
	for fieldname, value in defaults.items():
		if settings.get(fieldname) in (None, ""):
			settings.set(fieldname, value)
			changed = True

	if not settings.whatsapp_prompt_template:
		settings.whatsapp_prompt_template = (
			"Patient: {patient_name}\n"
			"Order date: {order_date}\n"
			"Items: {medicine_details}\n"
			"Payment: {payment_summary}\n"
			"Total: {total_amount}\n"
			"Advance paid: {total_advance_paid}\n"
			"Remaining: {remaining_amount}\n"
			"Address: {address}\n\n"
			"If all details are correct, reply exactly: YES\n"
			"If anything is wrong, reply with the correction, for example: Address wrong, correct address is ...\n\n"
			"Note: If you reply YES, we will mark the order confirmed. If you send a correction, we will create an issue for the team."
		)
		changed = True

	if not settings.get("company_settings"):
		settings.append(
			"company_settings",
			{
				"enabled": 1,
				"is_default": 1,
				"company": settings.default_company or "Default",
				"matching_keys": settings.default_company or "",
				"whatsapp_wait_minutes": settings.default_whatsapp_wait_minutes,
				"retry_delay_minutes": settings.default_retry_delay_minutes,
				"voice_call_timeout_minutes": settings.voice_call_timeout_minutes,
				"max_voice_attempts": settings.max_voice_attempts,
				"level_3_issue_tag": settings.level_3_issue_tag,
				"channel_account": settings.channel_account,
				"wa_chat_channel_account": settings.wa_chat_channel_account,
				"voice_agent": settings.voice_agent,
				"whatsapp_task_template": settings.whatsapp_task_template,
				"voice_task_template": settings.voice_task_template,
				"send_first_whatsapp_as_template": settings.send_first_whatsapp_as_template,
				"whatsapp_template_name": settings.whatsapp_template_name,
				"whatsapp_template_language": settings.whatsapp_template_language,
				"confirm_mcp_tool_name": settings.confirm_mcp_tool_name,
				"issue_mcp_tool_name": settings.issue_mcp_tool_name,
				"whatsapp_prompt_template": settings.whatsapp_prompt_template,
			},
		)
		changed = True

	if changed:
		settings.save(ignore_permissions=True)


def _settings_for_context(context: dict) -> frappe._dict:
	settings = frappe.get_single(SETTINGS)
	company_key = _normalize_match_key(
		context.get("company") or context.get("brand") or context.get("source_system") or context.get("event")
	)
	matched_row = None
	for row in settings.get("company_settings") or []:
		if _company_setting_matches(row, company_key):
			matched_row = row
			break
	if not matched_row:
		matched_row = _default_company_setting(settings)
	return _merge_settings(settings, matched_row)


def _workflow_settings(workflow) -> frappe._dict:
	context = parse_json_object(workflow.context_json, "Workflow Context JSON") if workflow.get("context_json") else {}
	context["company"] = workflow.company
	return _settings_for_context(context)


def _merge_settings(settings, row=None) -> frappe._dict:
	def value(fieldname: str, row_fieldname: str | None = None):
		row_fieldname = row_fieldname or fieldname
		if row and row.get(row_fieldname) not in (None, ""):
			return row.get(row_fieldname)
		return settings.get(fieldname)

	return frappe._dict(
		{
			"enabled": value("enabled"),
			"company": row.get("company") if row else settings.default_company,
			"default_company": settings.default_company,
			"default_whatsapp_wait_minutes": value("default_whatsapp_wait_minutes", "whatsapp_wait_minutes"),
			"default_retry_delay_minutes": value("default_retry_delay_minutes", "retry_delay_minutes"),
			"voice_call_timeout_minutes": value("voice_call_timeout_minutes"),
			"max_voice_attempts": value("max_voice_attempts"),
			"level_3_issue_tag": value("level_3_issue_tag"),
			"channel_account": value("channel_account"),
			"wa_chat_channel_account": value("wa_chat_channel_account"),
			"voice_agent": value("voice_agent"),
			"whatsapp_task_template": value("whatsapp_task_template"),
			"voice_task_template": value("voice_task_template"),
			"send_first_whatsapp_as_template": value("send_first_whatsapp_as_template"),
			"whatsapp_template_name": value("whatsapp_template_name"),
			"whatsapp_template_language": value("whatsapp_template_language"),
			"confirm_mcp_tool_name": value("confirm_mcp_tool_name"),
			"issue_mcp_tool_name": value("issue_mcp_tool_name"),
			"whatsapp_prompt_template": value("whatsapp_prompt_template"),
		}
	)


def _company_setting_matches(row, company_key: str) -> bool:
	if not row.get("enabled"):
		return False
	if not company_key:
		return False
	keys = [row.get("company") or ""]
	keys.extend(str(row.get("matching_keys") or "").split(","))
	return company_key in {_normalize_match_key(key) for key in keys if str(key).strip()}


def _default_company_setting(settings):
	for row in settings.get("company_settings") or []:
		if row.get("enabled") and row.get("is_default"):
			return row
	return None


def _normalize_match_key(value) -> str:
	return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def start_from_event(payload: dict) -> dict:
	"""Create a new workflow attempt and start Level 1 WhatsApp."""
	context = _normalize_payload(payload)
	idempotency_key = _idempotency_key(context)

	settings = _settings_for_context(context)
	if not settings.enabled:
		frappe.throw(f"Order Confirmation flow is disabled{f' for {settings.company}' if settings.company else ''}.")

	doc = frappe.new_doc(WORKFLOW)
	doc.update(
		{
			"status": "Draft",
			"company": context.get("company") or settings.company or settings.default_company,
			"external_order_id": context.get("external_order_id"),
			"idempotency_key": idempotency_key,
			"patient_encounter": context.get("patient_encounter"),
			"patient_name": context.get("patient_name"),
			"patient_mobile": context.get("patient_mobile"),
			"address": context.get("address"),
			"product": context.get("product"),
			"medicine_details": context.get("medicine_details"),
			"doctor_details": context.get("doctor_details"),
			"order_summary": context.get("order_summary"),
			"channel_account": settings.channel_account,
			"agent": settings.voice_agent,
			"max_retry_count": int(settings.max_voice_attempts or 2),
			"source_payload_json": as_json(payload),
			"context_json": as_json(context),
		}
	)
	doc.insert(ignore_permissions=True)
	send_level_1_whatsapp(doc.name)
	return {"status": "started", "workflow": doc.name}


def send_level_1_whatsapp(workflow_name: str) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	context = _workflow_context(workflow)
	body = _render(settings.whatsapp_prompt_template, context)

	task = _create_task(
		workflow=workflow,
		channel="WhatsApp",
		task_template=settings.whatsapp_task_template or _template_by_key("order_confirmation_whatsapp"),
		context={**context, "message": body, "workflow": workflow.name},
	)
	workflow.whatsapp_task = task.name
	workflow.task_batch = task.task_batch
	workflow.whatsapp_deadline = add_to_date(
		now_datetime(),
		minutes=int(settings.default_whatsapp_wait_minutes or 10),
		as_datetime=True,
	)
	workflow.status = "Level 1 WhatsApp Sent"

	try:
		if settings.send_first_whatsapp_as_template:
			template_result = _send_first_whatsapp_template(settings, workflow, body, task.name)
			task.status = "Completed"
			task.result_json = as_json(template_result)
			task.save(ignore_permissions=True)
		else:
			conversation = _send_wa_chat_message(settings, workflow, body)
			if conversation:
				workflow.chat_conversation = conversation
	except Exception as exc:
		workflow.last_error = str(exc)
		create_error("Order Confirmation WhatsApp", str(exc), source="order_confirmation", payload=context, exc=exc)

	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	if not settings.send_first_whatsapp_as_template:
		enqueue_task_execution(task.name, "WhatsApp")
	return {"workflow": workflow.name, "task": task.name}


def handle_whatsapp_reply(
	workflow: str | None = None,
	message: str | None = None,
	conversation: str | None = None,
	outcome: str | None = None,
	change_details: str | None = None,
) -> dict:
	doc = _find_active_workflow(workflow=workflow, conversation=conversation)
	if not doc:
		return {"status": "ignored", "reason": "no_active_workflow"}
	if doc.status in FINAL_STATES:
		return {"status": "ignored", "reason": "final_state", "workflow": doc.name}

	doc.status = "WhatsApp Replied"
	doc.last_customer_reply = message or ""
	doc.save(ignore_permissions=True)

	resolved = _classify_reply(message or "", outcome)
	if _has_pending_correction(doc):
		if resolved == "confirmed":
			return {"status": "correction_required", "workflow": doc.name}
		return create_issue_for_workflow(doc.name, _correction_detail(doc, message or change_details or ""))

	direct_correction = _direct_correction_detail(doc, change_details or message or "")
	if direct_correction:
		return create_issue_for_workflow(doc.name, direct_correction)

	if resolved == "confirmed":
		return confirm_workflow(doc.name, source="WhatsApp")
	if resolved == "issue":
		if _needs_more_correction_details(message or ""):
			doc.status = "Correction Pending"
			doc.change_details = f"Pending correction: {message or 'Customer said order details are wrong.'}"
			doc.save(ignore_permissions=True)
			_request_correction_details(doc, message or "")
			return {"status": "awaiting_correction", "workflow": doc.name}
		return create_issue_for_workflow(doc.name, change_details or message or "Customer requested change on WhatsApp.")
	_request_confirmation_or_correction(doc, message or "")
	return {"status": "awaiting_customer_confirmation", "workflow": doc.name, "outcome": resolved}


def on_chat_message_after_insert(doc, method=None) -> None:
	"""Receive customer replies from WA Chat Hub without making WA Chat Hub own workflow logic."""
	if getattr(doc, "direction", None) != "Inbound":
		return
	if getattr(doc, "sender_type", None) in {"AI", "System", "Bot"}:
		return
	if not getattr(doc, "conversation", None):
		return
	if not frappe.db.exists(WORKFLOW, {"chat_conversation": doc.conversation, "status": ["not in", list(FINAL_STATES)]}):
		return
	try:
		handle_whatsapp_reply(
			conversation=doc.conversation,
			message=doc.body or "",
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Order Confirmation WhatsApp Reply Failed")


def process_due_workflows() -> dict:
	"""Scheduler entrypoint: escalate WhatsApp no-replies and retry due missed calls."""
	now_value = now_datetime()
	queued_calls = 0
	final_issues = 0
	stale_calls_marked = 0
	missed_tasks_marked = 0

	for name in frappe.get_all(
		WORKFLOW,
		filters={"status": "Level 1 WhatsApp Sent", "whatsapp_deadline": ["<=", now_value]},
		pluck="name",
		limit=200,
	):
		try:
			queue_voice_call(name)
			queued_calls += 1
		except Exception as exc:
			_mark_failed(name, exc)

	for name in _stale_voice_workflows(now_value):
		try:
			started_at = _voice_task_started_at(frappe.get_doc(WORKFLOW, name))
			timeout_at = add_to_date(started_at or now_value, minutes=_voice_call_timeout_minutes(), as_datetime=True)
			mark_call_missed(name, "Voice call timed out without pickup/provider callback.", missed_at=timeout_at)
			stale_calls_marked += 1
		except Exception as exc:
			_mark_failed(name, exc)

	for name in _missed_voice_task_workflows():
		try:
			task_modified = _voice_task_modified_at(frappe.get_doc(WORKFLOW, name))
			mark_call_missed(name, "Voice task was marked deadline missed before provider callback.", missed_at=task_modified)
			missed_tasks_marked += 1
		except Exception as exc:
			_mark_failed(name, exc)

	for name in frappe.get_all(
		WORKFLOW,
		filters={"status": "Level 3 Retry Queued", "next_call_time": ["<=", now_value]},
		pluck="name",
		limit=200,
	):
		try:
			doc = frappe.get_doc(WORKFLOW, name)
			if int(doc.retry_count or 0) >= int(doc.max_retry_count or 2):
				create_level_3_issue(name)
				final_issues += 1
			else:
				queue_voice_call(name)
				queued_calls += 1
		except Exception as exc:
			_mark_failed(name, exc)

	return {
		"queued_calls": queued_calls,
		"stale_calls_marked": stale_calls_marked,
		"missed_tasks_marked": missed_tasks_marked,
		"final_issues": final_issues,
	}


def queue_voice_call(workflow_name: str) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	context = _workflow_context(workflow)
	context.update({"phone": _normalize_phone(workflow.patient_mobile), "to": _normalize_phone(workflow.patient_mobile), "workflow": workflow.name})
	task = _create_task(
		workflow=workflow,
		channel="Voice",
		task_template=settings.voice_task_template or _template_by_key("order_confirmation_voice"),
		context=context,
	)
	task.status = "Running"
	task.save(ignore_permissions=True)
	refresh_batch_counts(task.task_batch)
	workflow.retry_count = int(workflow.retry_count or 0) + 1
	workflow.voice_task = task.name
	workflow.task_batch = task.task_batch
	workflow.status = "Level 2 Call Queued" if workflow.retry_count <= 1 else "Level 3 Retry Queued"
	workflow.next_call_time = None
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	enqueue_task_execution(task.name, "Voice")
	return {"workflow": workflow.name, "task": task.name, "attempt": workflow.retry_count}


def handle_voice_result(
	workflow: str | None = None,
	task: str | None = None,
	outcome: str | None = None,
	notes: str | None = None,
) -> dict:
	doc = _find_active_workflow(workflow=workflow, task=task)
	if not doc:
		return {"status": "ignored", "reason": "no_active_workflow"}

	resolved = _classify_reply(notes or "", outcome)
	if resolved == "confirmed":
		return confirm_workflow(doc.name, source="Voice")
	if resolved == "issue":
		return create_issue_for_workflow(doc.name, notes or "Customer requested change on voice call.")
	if resolved == "missed":
		return mark_call_missed(doc.name, notes)

	return mark_call_missed(doc.name, notes or "Call picked but order was not clearly confirmed.")


def mark_call_missed(workflow_name: str, notes: str | None = None, *, missed_at=None) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	if notes:
		workflow.confirmation_notes = notes
	_mark_voice_task_missed(workflow.voice_task)
	if int(workflow.retry_count or 0) >= int(workflow.max_retry_count or settings.max_voice_attempts or 2):
		return create_level_3_issue(workflow.name)

	workflow.status = "Level 3 Retry Queued"
	workflow.next_call_time = add_to_date(
		missed_at or now_datetime(),
		minutes=int(settings.default_retry_delay_minutes or 60),
		as_datetime=True,
	)
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "retry_queued", "workflow": workflow.name, "next_call_time": workflow.next_call_time}


def confirm_workflow(workflow_name: str, source: str = "Unknown") -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	if workflow.status in STOP_CONFIRMATION_STATES or _has_pending_correction(workflow):
		return {"status": "blocked", "reason": "correction_or_issue_exists", "workflow": workflow.name}
	notes = _confirmation_notes(workflow, source)
	args = {
		"encounter_id": workflow.patient_encounter,
		"patient_encounter": workflow.patient_encounter,
		"order_confirmation_notes": notes,
		"sr_notes": notes,
		"sr_encounter_status": "PRX Pending",
		"checklist_confirmed": True,
		"customer_final_confirmation": "yes_after_full_checklist",
	}
	result = _call_named_mcp(_workflow_settings(workflow).confirm_mcp_tool_name, args, workflow=workflow)
	workflow.status = "Confirmed"
	workflow.confirmation_notes = notes
	workflow.mcp_result_json = as_json(result)
	workflow.last_error = ""
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "confirmed", "workflow": workflow.name, "mcp_result": result}


def create_issue_for_workflow(workflow_name: str, change_details: str, *, level_3: bool = False) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	args = _issue_args(workflow, change_details, settings.level_3_issue_tag if level_3 else None)
	result = _call_named_mcp(settings.issue_mcp_tool_name, args, workflow=workflow)
	workflow.status = "Level 3 Ticket Created" if level_3 else "Issue Created"
	workflow.change_details = change_details
	workflow.level_3_issue_created = 1 if level_3 else workflow.level_3_issue_created
	workflow.erp_issue = _extract_issue_name(result)
	workflow.mcp_result_json = as_json(result)
	workflow.last_error = ""
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": workflow.status, "workflow": workflow.name, "mcp_result": result}


def create_level_3_issue(workflow_name: str) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	summary = (
		"Customer did not pick the second order confirmation call.\n\n"
		f"Company: {workflow.company or ''}\n"
		f"Patient Name: {workflow.patient_name or ''}\n"
		f"Patient Mobile: {workflow.patient_mobile or ''}\n"
		f"Address: {workflow.address or ''}\n"
		f"Product: {workflow.product or ''}\n"
		f"Medicine Details: {workflow.medicine_details or ''}\n"
		f"Doctor Details: {workflow.doctor_details or ''}\n"
		f"Order Summary: {workflow.order_summary or ''}\n"
		"Status: Pending"
	)
	return create_issue_for_workflow(workflow.name, summary, level_3=True)


def _create_task(workflow, channel: str, task_template: str, context: dict):
	batch = frappe.new_doc("AI Task Batch")
	batch.update(
		{
			"status": "Queued",
			"source_system": workflow.company or "Order Confirmation",
			"batch_label": workflow.name,
			"idempotency_key": f"{workflow.name}:{channel}:{workflow.retry_count or 0}",
			"task_template": task_template,
			"target_agent": workflow.agent,
			"priority": "High" if channel == "Voice" else "Normal",
			"source_payload_json": workflow.source_payload_json,
		}
	)
	batch.insert(ignore_permissions=True)
	task = frappe.new_doc("AI Task")
	task.update(
		{
			"status": "Queued",
			"task_batch": batch.name,
			"task_template": task_template,
			"target_agent": workflow.agent,
			"assigned_agent": workflow.agent,
			"channel": channel,
			"priority": "High" if channel == "Voice" else "Normal",
			"external_record_id": workflow.name,
			"external_record_type": WORKFLOW,
			"idempotency_key": f"{workflow.name}:{channel}:{workflow.retry_count or 0}",
			"context_json": as_json(context),
		}
	)
	task.insert(ignore_permissions=True)
	refresh_batch_counts(batch.name)
	return task


def _send_wa_chat_message(settings, workflow, body: str) -> str | None:
	if not settings.wa_chat_channel_account:
		return None
	from wa_chat_hub.outbound import send_outbound_message
	from wa_chat_hub.security import set_ai_security_context, set_service_user_context
	from wa_chat_hub.services import append_message

	set_service_user_context("confluence_order_confirmation")
	set_ai_security_context(channel_account=settings.wa_chat_channel_account)
	result = append_message(
		{
			"channel_account": settings.wa_chat_channel_account,
			"phone_number": workflow.patient_mobile,
			"display_name": workflow.patient_name,
			"direction": "Outbound",
			"sender_type": "AI",
			"content_type": "Text",
			"body": body,
			"delivery_status": "Pending",
			"raw_payload": {"workflow": workflow.name},
		}
	)
	conversation = result.get("conversation")
	if conversation:
		send_outbound_message(conversation, body)
	return conversation


def _send_first_whatsapp_template(settings, workflow, body: str, task_name: str) -> dict:
	if not settings.wa_chat_channel_account:
		frappe.throw("WA Chat Channel Account is required to send the first WhatsApp template.")
	if not settings.whatsapp_template_name:
		frappe.throw("WhatsApp Template Name is required to send the first WhatsApp template.")
	template_body = _sanitize_template_value(body)

	from wa_chat_hub.outbound import send_interakt_template_message
	from wa_chat_hub.security import set_ai_security_context, set_service_user_context
	from wa_chat_hub.services import append_message

	set_service_user_context("confluence_order_confirmation")
	set_ai_security_context(channel_account=settings.wa_chat_channel_account)
	message_result = append_message(
		{
			"channel_account": settings.wa_chat_channel_account,
			"phone_number": workflow.patient_mobile,
			"display_name": workflow.patient_name,
			"direction": "Outbound",
			"sender_type": "AI",
			"content_type": "Template",
			"body": body,
			"delivery_status": "Pending",
			"raw_payload": {
				"workflow": workflow.name,
				"task": task_name,
				"template_name": settings.whatsapp_template_name,
				"body_values": [template_body],
			},
		}
	)
	conversation = message_result.get("conversation")
	if not conversation:
		frappe.throw("WA Chat Hub did not return a conversation for the template send.")

	send_result = send_interakt_template_message(
		conversation,
		{
			"template_name": settings.whatsapp_template_name,
			"language_code": settings.whatsapp_template_language or "en",
			"body_values": [template_body],
			"callback_data": as_json({"workflow": workflow.name, "task": task_name}),
		},
	)
	if message_result.get("message"):
		frappe.db.set_value(
			"Chat Message",
			message_result.get("message"),
			{
				"delivery_status": send_result.get("delivery_status") or "Sent",
				"channel_message_id": send_result.get("provider_message_id"),
				"raw_transport_payload": as_json(send_result.get("provider_response") or send_result),
			},
			update_modified=False,
		)
	workflow.chat_conversation = conversation
	return {"conversation": conversation, "message": message_result.get("message"), "send_result": send_result}


def _call_named_mcp(tool_name: str, arguments: dict, *, workflow) -> dict:
	tool_docname = frappe.db.get_value("AI MCP Tool", {"tool_name": tool_name, "enabled": 1}, "name")
	if not tool_docname:
		frappe.throw(f"Configured MCP tool not found or disabled: {tool_name}")
	try:
		return execute_mcp_tool(
			frappe.get_doc("AI MCP Tool", tool_docname),
			arguments,
			workflow.voice_task or workflow.whatsapp_task,
		)
	except Exception as exc:
		error_message = str(exc)
		frappe.db.set_value(
			WORKFLOW,
			workflow.name,
			{
				"last_error": error_message[:1000],
				"mcp_result_json": as_json(
					{
						"status": "failed",
						"tool_name": tool_name,
						"arguments": arguments,
						"error": error_message,
					}
				),
			},
			update_modified=True,
		)
		frappe.db.commit()
		raise


def _normalize_payload(payload: dict) -> dict:
	details = payload.get("payload_json") if isinstance(payload.get("payload_json"), dict) else {}
	patient_mobile = _first_value(payload, details, "patient_mobile", "customer_phone", "phone", "phone_number")
	merged = {**details, **payload}
	return {
		"company": _first_value(payload, details, "company", "company_name", "brand", "source_system"),
		"brand": _first_value(payload, details, "brand", "company", "company_name"),
		"source_system": _first_value(payload, details, "source_system"),
		"event": _first_value(payload, details, "event"),
		"external_order_id": _first_value(payload, details, "external_order_id", "order_id", "name"),
		"patient_encounter": _first_value(payload, details, "patient_encounter", "encounter_id"),
		"patient_name": _first_value(payload, details, "patient_name", "order_patient_name", "customer_name"),
		"patient_mobile": patient_mobile,
		"address": _first_value(payload, details, "address", "delivery_address"),
		"product": _format_details(_first_value(payload, details, "product", "item_summary", "items")),
		"medicine_details": _format_details(_first_value(payload, details, "medicine_details", "items", "item_summary")),
		"doctor_details": _format_details(_first_value(payload, details, "doctor_details", "doctor")),
		"order_date": _first_value(payload, details, "order_date", "date"),
		"payment_summary": _format_payment_summary(merged.get("payments")),
		"total_amount": _format_money(merged.get("total_amount")),
		"total_advance_paid": _format_money(merged.get("total_advance_paid")),
		"remaining_amount": _format_money(merged.get("remaining_amount")),
		"order_summary": _first_value(payload, details, "order_summary") or _build_order_summary(merged),
		"raw": merged,
	}


def _workflow_context(workflow) -> dict:
	context = parse_json_object(workflow.context_json, "Workflow Context JSON")
	context.update(
		{
			"workflow": workflow.name,
			"company": workflow.company,
			"patient_name": workflow.patient_name,
			"customer_name": workflow.patient_name,
			"patient_mobile": workflow.patient_mobile,
			"customer_phone": workflow.patient_mobile,
			"phone": _normalize_phone(workflow.patient_mobile),
			"phone_number": _normalize_phone(workflow.patient_mobile),
			"patient_encounter": workflow.patient_encounter,
			"encounter_id": workflow.patient_encounter,
			"address": workflow.address,
			"product": workflow.product,
			"medicine_details": workflow.medicine_details,
			"doctor_details": workflow.doctor_details,
			"order_summary": workflow.order_summary,
		}
	)
	raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
	context.update(
		{
			"order_date": context.get("order_date") or raw.get("order_date") or raw.get("date") or "",
			"payment_summary": context.get("payment_summary") or _format_payment_summary(raw.get("payments")),
			"total_amount": context.get("total_amount") or _format_money(raw.get("total_amount")),
			"total_advance_paid": context.get("total_advance_paid") or _format_money(raw.get("total_advance_paid")),
			"remaining_amount": context.get("remaining_amount") or _format_money(raw.get("remaining_amount")),
		}
	)
	return context


def _idempotency_key(context: dict) -> str:
	source_key = context.get("patient_encounter") or context.get("external_order_id") or "order-confirmation"
	return f"{source_key}-{frappe.generate_hash(length=10)}"


def _voice_call_timeout_minutes() -> int:
	settings = _settings_for_context({})
	return max(1, int(settings.get("voice_call_timeout_minutes") or 5))


def _stale_voice_workflows(now_value) -> list[str]:
	timeout_at = add_to_date(now_value, minutes=-_voice_call_timeout_minutes(), as_datetime=True)
	rows = frappe.db.sql(
		"""
		select workflow.name
		from `tabOrder Confirmation Workflow` workflow
		inner join `tabAI Task` task on task.name = workflow.voice_task
		where workflow.status in ('Level 2 Call Queued', 'Level 3 Retry Queued')
			and (workflow.next_call_time is null or workflow.next_call_time = '')
			and workflow.voice_task is not null
			and workflow.voice_task != ''
			and task.channel = 'Voice'
			and task.status = 'Running'
			and task.modified <= %s
		order by workflow.modified asc
		limit 200
		""",
		timeout_at,
		pluck=True,
	)
	return rows or []


def _missed_voice_task_workflows() -> list[str]:
	rows = frappe.db.sql(
		"""
		select workflow.name
		from `tabOrder Confirmation Workflow` workflow
		inner join `tabAI Task` task on task.name = workflow.voice_task
		where workflow.status in ('Level 2 Call Queued', 'Level 3 Retry Queued')
			and (workflow.next_call_time is null or workflow.next_call_time = '')
			and workflow.voice_task is not null
			and workflow.voice_task != ''
			and task.channel = 'Voice'
			and task.status = 'Deadline Missed'
		order by workflow.modified asc
		limit 200
		""",
		pluck=True,
	)
	return rows or []


def _voice_task_started_at(workflow):
	if not workflow.voice_task:
		return None
	started_at = frappe.db.get_value(
		"AI Task Attempt",
		{"task": workflow.voice_task},
		"started_at",
		order_by="creation desc",
	)
	if started_at:
		return started_at
	task_values = frappe.db.get_value("AI Task", workflow.voice_task, ["modified", "creation"], as_dict=True)
	return (task_values or {}).get("modified") or (task_values or {}).get("creation")


def _voice_task_modified_at(workflow):
	if not workflow.voice_task:
		return None
	task_values = frappe.db.get_value("AI Task", workflow.voice_task, ["modified", "creation"], as_dict=True)
	return (task_values or {}).get("modified") or (task_values or {}).get("creation") or now_datetime()


def _mark_voice_task_missed(task_name: str | None) -> None:
	if not task_name or not frappe.db.exists("AI Task", task_name):
		return
	frappe.db.set_value(
		"AI Task",
		task_name,
		{"status": "Deadline Missed", "last_error": "Voice call timed out without pickup/provider callback."},
		update_modified=True,
	)
	attempt_name = frappe.db.get_value(
		"AI Task Attempt",
		{"task": task_name, "status": "Started"},
		"name",
		order_by="creation desc",
	)
	if attempt_name:
		frappe.db.set_value(
			"AI Task Attempt",
			attempt_name,
			{"status": "Failed", "error_message": "Voice call timed out without pickup/provider callback.", "ended_at": now_datetime()},
			update_modified=True,
		)


def _build_order_summary(payload: dict) -> str:
	parts = []
	if payload.get("order_date"):
		parts.append(f"Order date: {payload.get('order_date')}")
	if payload.get("items"):
		parts.append(f"Items: {_format_details(payload.get('items'))}")
	if payload.get("payments"):
		parts.append(f"Payment: {_format_payment_summary(payload.get('payments'))}")
	for key, label in (
		("total_amount", "Total"),
		("total_advance_paid", "Advance paid"),
		("remaining_amount", "Remaining"),
	):
		if payload.get(key) not in (None, "", [], {}):
			parts.append(f"{label}: {_format_money(payload.get(key))}")
	return "\n".join(parts)


def _first_value(primary: dict, secondary: dict, *keys: str):
	for source in (primary, secondary):
		for key in keys:
			value = source.get(key)
			if value not in (None, "", [], {}):
				return value
	return None


def _format_details(value) -> str:
	if isinstance(value, list):
		lines = [_format_item_line(row) for row in value]
		return "; ".join(line for line in lines if line)
	if isinstance(value, dict):
		return ", ".join(f"{_label(key)}: {value}" for key, value in value.items() if value not in (None, "", [], {}))
	return "" if value is None else str(value)


def _format_item_line(row) -> str:
	if not isinstance(row, dict):
		return _format_details(row)
	name = row.get("name") or row.get("item_name") or row.get("product") or row.get("medicine") or "Item"
	qty = row.get("qty") or row.get("quantity")
	amount = row.get("amount") if row.get("amount") not in (None, "") else row.get("rate")
	parts = [str(name)]
	if qty not in (None, "", 0):
		parts.append(f"x{qty}")
	if amount not in (None, ""):
		parts.append(f"- {_format_money(amount)}")
	return " ".join(parts)


def _format_payment_summary(payments) -> str:
	if not payments:
		return ""
	if not isinstance(payments, list):
		return _format_details(payments)
	lines = []
	for row in payments:
		if isinstance(row, dict):
			mode = row.get("mode") or row.get("payment_mode") or row.get("type") or "Payment"
			amount = row.get("amount")
			lines.append(f"{mode}: {_format_money(amount)}" if amount not in (None, "") else str(mode))
		else:
			lines.append(str(row))
	return "; ".join(lines)


def _format_money(value) -> str:
	if value in (None, ""):
		return ""
	try:
		number = float(value)
	except (TypeError, ValueError):
		return str(value)
	if number.is_integer():
		number = int(number)
	return f"INR {number}"


def _label(value: str) -> str:
	return str(value).replace("_", " ").strip().title()


def _render(template: str | None, context: dict) -> str:
	text = template or "{order_summary}"
	try:
		return text.format(**{k: "" if v is None else v for k, v in context.items()})
	except Exception:
		try:
			return frappe.render_template(text, context)
		except Exception:
			return text


def _sanitize_template_value(value: str) -> str:
	"""Interakt rejects template variables containing newlines, tabs, or long space runs."""
	text = "" if value is None else str(value)
	text = text.replace("\t", " ")
	text = re.sub(r"[\r\n]+", " | ", text)
	text = re.sub(r" {2,}", " ", text)
	return text.strip()


def _has_pending_correction(workflow) -> bool:
	return str(workflow.change_details or "").strip().lower().startswith("pending correction:")


def _needs_more_correction_details(message: str) -> bool:
	normalized = " ".join((message or "").strip().lower().replace(".", " ").replace(",", " ").split())
	if not normalized:
		return True

	vague_phrases = (
		"address wrong",
		"wrong address",
		"address galat",
		"galat address",
		"address change",
		"change address",
		"change my address",
		"address update",
		"delivery address wrong",
		"delivery address galat",
		"nahi address galat",
		"address is wrong",
	)
	if any(phrase in normalized for phrase in vague_phrases):
		# If there are digits or a longer address-like value after the complaint,
		# treat it as a real correction instead of asking again.
		has_possible_value = bool(re.search(r"\d", normalized)) or len(normalized.split()) >= 8
		return not has_possible_value

	if normalized in {"wrong", "galat", "change", "update", "correction", "cancel", "nahi"}:
		return True
	return False


def _request_correction_details(workflow, message: str) -> None:
	settings = _workflow_settings(workflow)
	text = "Samajh gaya. Kripya sahi detail bhejein, taaki main team ke liye correction issue bana sakoon."
	normalized = " ".join((message or "").strip().lower().split())
	if "address" in normalized:
		text = "Samajh gaya. Kripya sahi address bhejein, taaki main old aur new address ke saath issue bana sakoon."
	try:
		_send_wa_chat_message(settings, workflow, text)
	except Exception as exc:
		create_error(
			"Order Confirmation Correction Prompt",
			str(exc),
			source="order_confirmation",
			payload={"workflow": workflow.name},
			exc=exc,
		)


def _correction_detail(workflow, correction: str) -> str:
	pending = re.sub(r"^pending correction:\s*", "", str(workflow.change_details or ""), flags=re.I).strip()
	correction = str(correction or "").strip()
	parts = ["Customer requested correction."]
	if pending:
		parts.append(f"Initial customer message: {pending}")
	if correction:
		parts.append(f"Customer corrected detail: {correction}")
	if "address" in f"{pending} {correction}".lower():
		parts.append(f"Old Address: {workflow.address or ''}")
		parts.append(f"New / Corrected Address: {correction}")
	else:
		parts.append(f"Old Order Details: {workflow.order_summary or ''}")
		parts.append(f"New / Corrected Details: {correction}")
	return "\n".join(parts)


def _direct_correction_detail(workflow, message: str) -> str | None:
	message = str(message or "").strip()
	if not message:
		return None

	normalized = _normalize_reply_text(message)
	if _is_confirmation_text(normalized):
		return None

	digits = re.sub(r"\D", "", message)
	current_mobile = re.sub(r"\D", "", str(workflow.patient_mobile or ""))
	if digits and len(digits) >= 8 and digits != current_mobile and len(normalized.split()) <= 4:
		return "\n".join(
			[
				"Customer requested correction.",
				"Correction Type: Phone / contact number",
				f"Old Phone: {workflow.patient_mobile or ''}",
				f"New / Corrected Phone: {message}",
				f"Old Order Details: {workflow.order_summary or ''}",
			]
		)

	correction_tokens = (
		"wrong",
		"galat",
		"change",
		"changed",
		"correction",
		"correct karo",
		"update",
		"replace",
		"modify",
		"cancel",
		"cancelled",
		"nahi chahiye",
		"not ordered",
		"address",
		"phone",
		"mobile",
		"number",
		"item",
		"product",
		"medicine",
		"payment",
		"amount",
	)
	if not any(token in normalized for token in correction_tokens):
		return None

	parts = ["Customer requested correction.", f"Customer Message: {message}"]
	if "address" in normalized or _looks_like_address(normalized):
		parts.append(f"Old Address: {workflow.address or ''}")
		parts.append(f"New / Corrected Address: {message}")
	elif any(token in normalized for token in ("phone", "mobile", "number")):
		parts.append(f"Old Phone: {workflow.patient_mobile or ''}")
		parts.append(f"New / Corrected Phone: {message}")
	else:
		parts.append(f"Old Order Details: {workflow.order_summary or ''}")
		parts.append(f"New / Corrected Details: {message}")
	return "\n".join(parts)


def _request_confirmation_or_correction(workflow, message: str) -> None:
	settings = _workflow_settings(workflow)
	text = (
		"Samajh gaya. Order confirm karne se pehle please batayein: "
		"kya sab details correct hain aur main order confirm kar doon? "
		"Agar address, phone, item ya payment mein correction hai to woh detail bhej dein."
	)
	try:
		_send_wa_chat_message(settings, workflow, text)
	except Exception as exc:
		create_error(
			"Order Confirmation Follow-up Prompt",
			str(exc),
			source="order_confirmation",
			payload={"workflow": workflow.name, "customer_message": message},
			exc=exc,
		)


def _classify_reply(message: str, outcome: str | None = None) -> str:
	if outcome:
		value = outcome.strip().lower().replace(" ", "_")
		if value in {"confirmed", "confirm", "yes"}:
			return "confirmed"
		if value in {"issue", "change", "changed", "cancel", "cancelled", "problem"}:
			return "issue"
		if value in {"missed", "no_answer", "not_answered", "busy", "failed"}:
			return "missed"
	normalized = _normalize_reply_text(message)
	if _is_confirmation_text(normalized) and not _has_correction_intent(normalized):
		return "confirmed"
	if any(
		token in normalized
		for token in (
			"wrong",
			"galat",
			"change",
			"changes",
			"changed",
			"correct karo",
			"correction",
			"update",
			"replace",
			"modify",
			"cancel",
			"cancelled",
			"nahi chahiye",
			"phone wrong",
			"not ordered",
			"new address",
			"change address",
			"change my address",
			"address change",
			"address galat",
			"wrong address",
			"address wrong",
			"address update",
			"delivery address",
			"payment issue",
			"item change",
			"medicine change",
		)
	):
		return "issue"
	if _is_confirmation_text(normalized):
		return "confirmed"
	return "not_confirmed"


def _normalize_reply_text(message: str) -> str:
	text = (message or "").strip().lower()
	text = re.sub(r"[^\w\s+/-]", " ", text)
	return " ".join(text.split())


def _is_confirmation_text(normalized: str) -> bool:
	if not normalized:
		return False
	confirmed_exact = {
		"yes",
		"y",
		"ok",
		"okay",
		"k",
		"confirm",
		"confirmed",
		"correct",
		"all correct",
		"yes correct",
		"haan",
		"ha",
		"han",
		"haa",
		"ji",
		"haan ji",
		"ha ji",
		"hanji",
		"theek hai",
		"thik hai",
		"theek h",
		"thik h",
		"sahi hai",
		"sahi h",
	}
	if normalized in confirmed_exact:
		return True
	confirmation_words = {
		"yes",
		"y",
		"ok",
		"okay",
		"confirm",
		"confirmed",
		"correct",
		"haan",
		"ha",
		"han",
		"haa",
		"ji",
		"theek",
		"thik",
		"sahi",
		"bilkul",
	}
	words = set(normalized.split())
	if "confirm" in words and words.intersection(confirmation_words - {"confirm"}):
		return True
	if "correct" in words and words.intersection({"yes", "haan", "ha", "han", "haa", "ji", "ok", "okay", "confirm"}):
		return True
	if "sahi" in words and words.intersection({"yes", "haan", "ha", "han", "haa", "ji", "ok", "okay", "confirm", "bilkul"}):
		return True
	return any(
		token in normalized
		for token in (
			"yes correct",
			"correct confirm",
			"confirmed correct",
			"confirm correct",
			"haan correct",
			"han correct",
			"haa correct",
			"yes confirm",
			"haan confirm",
			"han confirm",
			"haa confirm",
			"confirm kar do",
			"confirm kardo",
			"order confirm",
			"order laga do",
			"order lagado",
			"bhej do",
			"send kar do",
			"proceed",
			"go ahead",
			"looks good",
			"all good",
			"sab correct",
			"sab sahi",
			"sab theek",
			"bilkul sahi",
			"correct hai",
			"details correct",
			"all details correct",
			"main confirm karta",
			"kar do",
			"kardo",
		)
	)


def _has_correction_intent(normalized: str) -> bool:
	return any(
		token in normalized
		for token in (
			"wrong",
			"galat",
			"change",
			"changed",
			"correction",
			"update",
			"replace",
			"modify",
			"cancel",
			"cancelled",
			"nahi chahiye",
			"not ordered",
		)
	)


def _looks_like_address(normalized: str) -> bool:
	if not normalized:
		return False
	address_words = ("house", "flat", "sector", "street", "road", "nagar", "colony", "gurugram", "haryana", "india")
	return bool(re.search(r"\d", normalized)) and (len(normalized.split()) >= 6 or any(word in normalized for word in address_words))


def _find_active_workflow(workflow: str | None = None, conversation: str | None = None, task: str | None = None):
	if workflow and frappe.db.exists(WORKFLOW, workflow):
		return frappe.get_doc(WORKFLOW, workflow)
	if conversation:
		rows = frappe.get_all(WORKFLOW, filters={"chat_conversation": conversation}, order_by="modified desc", limit=1)
		return frappe.get_doc(WORKFLOW, rows[0].name) if rows else None
	elif task:
		name = frappe.db.get_value(WORKFLOW, {"voice_task": task}, "name") or frappe.db.get_value(WORKFLOW, {"whatsapp_task": task}, "name")
		return frappe.get_doc(WORKFLOW, name) if name else None
	else:
		return None


def _template_by_key(key: str) -> str | None:
	return frappe.db.get_value("AI Task Template", {"template_key": key}, "name")


def _ensure_task_template(key: str, name: str, channel: str) -> None:
	if frappe.db.exists("AI Task Template", {"template_key": key}):
		return
	doc = frappe.new_doc("AI Task Template")
	doc.update(
		{
			"enabled": 1,
			"template_key": key,
			"template_name": name,
			"task_type": "Order Confirmation",
			"objective_prompt": name,
			"default_channel": channel,
			"default_priority": "High" if channel == "Voice" else "Normal",
			"default_timeout_seconds": 900,
		}
	)
	doc.insert(ignore_permissions=True)


def _confirmation_notes(workflow, source: str) -> str:
	return (
		f"Order confirmed via {source}.\n"
		f"Company: {workflow.company or ''}\n"
		f"Patient Name: {workflow.patient_name or ''}\n"
		f"Phone: {workflow.patient_mobile or ''}\n"
		f"Patient Encounter: {workflow.patient_encounter or ''}\n"
		f"Address: {workflow.address or ''}\n"
		f"Product: {workflow.product or ''}\n"
		f"Medicine Details: {workflow.medicine_details or ''}\n"
		f"Doctor Details: {workflow.doctor_details or ''}\n"
		f"Order Summary: {workflow.order_summary or ''}\n"
		"Customer verbally confirmed all order details."
	)


def _issue_args(workflow, detail: str, tag: str | None = None) -> dict:
	subject = f"Order confirmation issue - {workflow.patient_name or workflow.patient_mobile or workflow.name}"
	if tag:
		subject = f"{tag} - {workflow.patient_name or workflow.patient_mobile or workflow.name}"
	description = (
		f"{detail}\n\n"
		f"Company: {workflow.company or ''}\n"
		f"Patient Name: {workflow.patient_name or ''}\n"
		f"Patient Mobile: {workflow.patient_mobile or ''}\n"
		f"Patient Encounter: {workflow.patient_encounter or ''}\n"
		f"Address: {workflow.address or ''}\n"
		f"Product: {workflow.product or ''}\n"
		f"Medicine Details: {workflow.medicine_details or ''}\n"
		f"Doctor Details: {workflow.doctor_details or ''}\n"
		f"Order Summary: {workflow.order_summary or ''}\n"
		f"Status: Pending\n"
		f"Tag: {tag or ''}"
	)
	return {
		"subject": subject,
		"description": description,
		"company": workflow.company,
		"customer_name": workflow.patient_name,
		"customer_phone": workflow.patient_mobile,
		"patient_name": workflow.patient_name,
		"patient_mobile": workflow.patient_mobile,
		"patient_encounter": workflow.patient_encounter,
		"address": workflow.address,
		"product": workflow.product,
		"medicine_details": workflow.medicine_details,
		"doctor_details": workflow.doctor_details,
		"order_summary": workflow.order_summary,
		"issue_summary": detail,
		"status": "Pending",
		"tag": tag,
	}


def _extract_issue_name(result: dict) -> str | None:
	data = result.get("data") if isinstance(result, dict) else None
	if isinstance(data, dict):
		return data.get("name")
	body = result.get("body") if isinstance(result, dict) else None
	if isinstance(body, dict):
		return body.get("name")
	return None


def _normalize_phone(value: Any) -> str:
	text = str(value or "").strip()
	if not text:
		return ""
	digits = "".join(ch for ch in text if ch.isdigit())
	if len(digits) == 10:
		return f"+91{digits}"
	if len(digits) == 12 and digits.startswith("91"):
		return f"+{digits}"
	return text


def _mark_failed(name: str, exc: Exception) -> None:
	doc = frappe.get_doc(WORKFLOW, name)
	doc.status = "Failed"
	doc.last_error = str(exc)
	doc.save(ignore_permissions=True)
	create_error("Order Confirmation Workflow", str(exc), source="order_confirmation", payload={"workflow": name}, exc=exc)
	frappe.db.commit()
