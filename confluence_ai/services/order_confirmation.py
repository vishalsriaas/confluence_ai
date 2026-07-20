from __future__ import annotations

import json
import os
import re
from typing import Any

import frappe
import requests
from frappe.utils import add_to_date, now_datetime
from frappe.utils.synchronization import filelock

from confluence_ai.api.mcp import execute_mcp_tool
from confluence_ai.services.dispatcher import enqueue_task_execution, refresh_batch_counts
from confluence_ai.services.utils import as_json, create_error, parse_json_object, record_provider_event

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
				"company": settings.default_company or "",
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
			"company": _resolve_ai_company(row.get("company")) if row else _resolve_ai_company(settings.default_company),
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


def _resolve_ai_company(value) -> str:
	value = str(value or "").strip()
	if not value or not frappe.db.exists("DocType", "AI Company"):
		return ""
	if frappe.db.exists("AI Company", value):
		return value
	company = frappe.db.get_value("AI Company", {"company_name": value}, "name")
	if company:
		return company
	company_key = _normalize_match_key(value).replace(" ", "_")
	return frappe.db.get_value("AI Company", {"company_key": company_key}, "name") or ""


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
			"company": _resolve_ai_company(context.get("company")) or settings.company or _resolve_ai_company(settings.default_company),
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
			_request_correction_details(doc, message or "")
			return {"status": "correction_required", "workflow": doc.name}
		return create_issue_for_workflow(
			doc.name,
			_correction_detail(doc, message or change_details or ""),
			reply_on_whatsapp=True,
		)

	direct_correction = _direct_correction_detail(doc, change_details or message or "")
	if direct_correction:
		return create_issue_for_workflow(doc.name, direct_correction, reply_on_whatsapp=True)

	if resolved == "confirmed":
		return confirm_workflow(doc.name, source="WhatsApp")
	if resolved == "issue":
		if _needs_more_correction_details(message or ""):
			doc.status = "Correction Pending"
			doc.change_details = f"Pending correction: {message or 'Customer said order details are wrong.'}"
			doc.save(ignore_permissions=True)
			_request_correction_details(doc, message or "")
			return {"status": "awaiting_correction", "workflow": doc.name}
		return create_issue_for_workflow(
			doc.name,
			change_details or message or "Customer requested change on WhatsApp.",
			reply_on_whatsapp=True,
		)
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
	frappe.enqueue(
		"confluence_ai.services.order_confirmation.process_whatsapp_reply_message",
		queue="short",
		message_name=doc.name,
		enqueue_after_commit=True,
		job_id=f"order_confirmation_reply_{doc.name}",
		deduplicate=True,
	)


def process_whatsapp_reply_message(message_name: str) -> dict:
	if not message_name or not frappe.db.exists("Chat Message", message_name):
		return {"status": "ignored", "reason": "missing_message"}
	message = frappe.get_doc("Chat Message", message_name)
	if message.direction != "Inbound":
		return {"status": "ignored", "reason": "not_inbound"}
	if (message.sender_type or "").strip() in {"AI", "System", "Bot"}:
		return {"status": "ignored", "reason": "system_sender"}
	if not message.conversation:
		return {"status": "ignored", "reason": "missing_conversation"}

	with _workflow_conversation_lock(message.conversation):
		if not frappe.db.exists(
			WORKFLOW,
			{"chat_conversation": message.conversation, "status": ["not in", list(FINAL_STATES)]},
		):
			return {"status": "ignored", "reason": "no_active_workflow"}
		try:
			return handle_whatsapp_reply(
				conversation=message.conversation,
				message=message.body or "",
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Order Confirmation WhatsApp Reply Failed")
			return {"status": "failed", "message": message_name}


def _workflow_conversation_lock(conversation: str):
	return filelock(f"order_confirmation_conversation_{conversation}", timeout=60)


def _workflow_lock(workflow_name: str):
	return filelock(f"order_confirmation_workflow_{workflow_name}", timeout=60)


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
			if queue_voice_call(name).get("status") == "queued":
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
				if queue_voice_call(name).get("status") == "queued":
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
	with _workflow_lock(workflow_name):
		workflow = frappe.get_doc(WORKFLOW, workflow_name)
		if workflow.status not in {"Level 1 WhatsApp Sent", "Level 3 Retry Queued"}:
			return {"status": "skipped", "reason": "workflow_not_due", "workflow": workflow.name}
		if workflow.status == "Level 3 Retry Queued" and workflow.next_call_time:
			if frappe.utils.get_datetime(workflow.next_call_time) > now_datetime():
				return {"status": "skipped", "reason": "retry_not_due", "workflow": workflow.name}
		if workflow.voice_task:
			task_status = frappe.db.get_value("AI Task", workflow.voice_task, "status")
			if task_status in {"Queued", "Waiting", "Running"}:
				return {"status": "skipped", "reason": "voice_task_already_active", "workflow": workflow.name, "task": workflow.voice_task}

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
		enqueue_task_execution(task.name, "Voice", enqueue_after_commit=False)
		return {"status": "queued", "workflow": workflow.name, "task": task.name, "attempt": workflow.retry_count}


def handle_voice_result(
	workflow: str | None = None,
	task: str | None = None,
	outcome: str | None = None,
	notes: str | None = None,
) -> dict:
	doc = _find_active_workflow(workflow=workflow, task=task)
	if not doc:
		return {"status": "ignored", "reason": "no_active_workflow"}

	if _looks_like_transcript(notes or "") and not outcome:
		decision = judge_voice_order_confirmation(doc, notes or "", task=task)
		if decision.get("customer_requested_change") or decision.get("customer_cancelled"):
			return create_issue_for_workflow(
				doc.name,
				_voice_review_issue_detail(doc, notes or "", decision, "Customer requested change/cancel during voice confirmation."),
			)
		if decision.get("order_details_read_fully") and decision.get("customer_confirmed_after_details") and float(decision.get("confidence") or 0) >= 0.8:
			return confirm_workflow(doc.name, source="Voice", extra_notes=f"Voice confirmation judge:\n{as_json(decision)}")
		return create_issue_for_workflow(
			doc.name,
			_voice_review_issue_detail(doc, notes or "", decision, "Voice confirmation was unclear; manual review required."),
		)

	resolved = _classify_voice_result(notes or "", outcome)
	if resolved == "confirmed":
		return confirm_workflow(doc.name, source="Voice")
	if resolved == "issue":
		return create_issue_for_workflow(doc.name, notes or "Customer requested change on voice call.")
	if resolved == "missed":
		return mark_call_missed(doc.name, notes)

	return mark_call_missed(doc.name, notes or "Call picked but order was not clearly confirmed.")


def wait_for_voice_transcript(workflow_name: str, notes: str | None = None) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	if workflow.status in FINAL_STATES:
		return {"status": "ignored", "reason": "final_state", "workflow": workflow.name}
	if notes:
		workflow.confirmation_notes = notes
	workflow.status = "Level 3 Retry Queued"
	workflow.next_call_time = add_to_date(
		now_datetime(),
		minutes=int(settings.default_retry_delay_minutes or 60),
		as_datetime=True,
	)
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "waiting_for_transcript", "workflow": workflow.name, "next_call_time": workflow.next_call_time}


def judge_voice_order_confirmation(workflow, transcript: str, task: str | None = None) -> dict:
	"""Use an LLM to decide whether a voice transcript truly confirms an order."""
	base_decision = {
		"order_details_read_fully": False,
		"customer_confirmed_after_details": False,
		"customer_requested_change": False,
		"customer_cancelled": False,
		"confidence": 0.0,
		"reason": "No judge decision.",
	}
	agent = frappe.get_doc("AI Agent", workflow.agent) if workflow.agent and frappe.db.exists("AI Agent", workflow.agent) else None
	request_payload = _voice_judge_request_payload(workflow, transcript)
	try:
		result = _run_voice_confirmation_judge(agent, request_payload, task=task)
	except Exception as exc:
		create_error(
			"Voice Confirmation Judge",
			str(exc),
			source="order_confirmation",
			task=task,
			agent=agent.name if agent else None,
			payload={"workflow": workflow.name},
			exc=exc,
		)
		return {**base_decision, "reason": f"Judge failed: {exc}"}

	decision = _normalize_voice_judge_decision(result)
	record_provider_event(
		provider="Voice Confirmation Judge",
		operation="order_confirmation_decision",
		status="Succeeded",
		agent=agent.name if agent else None,
		task=task,
		request=request_payload,
		response=decision,
	)
	return decision


def _voice_judge_request_payload(workflow, transcript: str) -> dict:
	return {
		"instructions": (
			"You are auditing an order-confirmation voice call. Return ONLY JSON. "
			"Confirm only if the agent read the available order details before final confirmation, "
			"and the customer confirmed after that. If the customer only said yes to hearing details, "
			"that is not confirmation. If the agent claimed updated/confirmed before customer confirmation, "
			"do not confirm. If any correction/cancel is requested, mark it."
		),
		"required_schema": {
			"order_details_read_fully": "boolean",
			"customer_confirmed_after_details": "boolean",
			"customer_requested_change": "boolean",
			"customer_cancelled": "boolean",
			"confidence": "number from 0 to 1",
			"reason": "short string",
		},
		"order_details": {
			"patient_name": workflow.patient_name,
			"patient_mobile": workflow.patient_mobile,
			"address": workflow.address,
			"product": workflow.product,
			"medicine_details": workflow.medicine_details,
			"doctor_details": workflow.doctor_details,
			"order_summary": workflow.order_summary,
		},
		"transcript": transcript,
	}


def _run_voice_confirmation_judge(agent, payload: dict, task: str | None = None) -> dict:
	providers = [agent.primary_provider if agent else "OpenAI", agent.fallback_provider if agent else None]
	last_error = None
	for provider in [provider for provider in providers if provider]:
		try:
			return _run_voice_confirmation_judge_provider(provider, agent, payload, task=task)
		except Exception as exc:
			last_error = exc
			record_provider_event(
				provider=provider,
				operation="voice_confirmation_judge",
				status="Failed",
				agent=agent.name if agent else None,
				task=task,
				request=payload,
				error=str(exc),
			)
	if last_error:
		raise last_error
	frappe.throw("No LLM provider configured for voice confirmation judge.")


def _run_voice_confirmation_judge_provider(provider: str, agent, payload: dict, task: str | None = None) -> dict:
	model_config = parse_json_object(agent.model_config, "Model Config") if agent else {}
	provider_config = model_config.get(provider) or model_config.get(str(provider).lower()) or {}
	provider_name = str(provider or "").lower()
	if provider_name == "openai":
		return _call_openai_voice_judge(provider_config, payload, agent=agent, task=task)
	return _call_custom_voice_judge(provider, provider_config, payload, agent=agent, task=task)


def _call_openai_voice_judge(provider_config: dict, payload: dict, *, agent=None, task: str | None = None) -> dict:
	api_key = (
		provider_config.get("api_key")
		or frappe.conf.get("openai_api_key")
		or frappe.conf.get("OPENAI_API_KEY")
		or os.getenv("OPENAI_API_KEY")
	)
	if not api_key:
		frappe.throw("OpenAI API key is not configured for voice confirmation judge.")
	base_url = (provider_config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
	url = f"{base_url}/{(provider_config.get('path') or 'chat/completions').lstrip('/')}"
	body = {
		"model": provider_config.get("model") or "gpt-4.1-mini",
		"temperature": 0,
		"response_format": {"type": "json_object"},
		"messages": [
			{"role": "system", "content": "Return only valid JSON for the requested schema."},
			{"role": "user", "content": as_json(payload)},
		],
	}
	response = requests.post(
		url,
		headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
		data=as_json(body),
		timeout=int(provider_config.get("timeout") or 45),
	)
	result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:4000]}
	record_provider_event(
		provider="OpenAI",
		operation="voice_confirmation_judge",
		status="Succeeded" if response.ok else "Failed",
		agent=agent.name if agent else None,
		task=task,
		request={**payload, "model": body["model"]},
		response=result,
	)
	if not response.ok:
		frappe.throw(f"OpenAI voice judge failed with HTTP {response.status_code}: {response.text[:500]}")
	data = response.json()
	content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
	return parse_json_object(content, "Voice Confirmation Judge Response")


def _call_custom_voice_judge(provider: str, provider_config: dict, payload: dict, *, agent=None, task: str | None = None) -> dict:
	base_url = provider_config.get("base_url")
	if not base_url:
		frappe.throw(f"{provider} base_url is not configured for voice confirmation judge.")
	url = base_url.rstrip("/") + "/" + (provider_config.get("path") or "").lstrip("/")
	headers = {"Content-Type": "application/json"}
	if provider_config.get("api_key"):
		headers["Authorization"] = f"Bearer {provider_config.get('api_key')}"
	body = {"model": provider_config.get("model"), "task": "voice_order_confirmation_judge", "payload": payload}
	response = requests.post(url, headers=headers, data=as_json(body), timeout=int(provider_config.get("timeout") or 45))
	result = {"status_code": response.status_code, "ok": response.ok, "body": response.text[:4000]}
	record_provider_event(
		provider=provider,
		operation="voice_confirmation_judge",
		status="Succeeded" if response.ok else "Failed",
		agent=agent.name if agent else None,
		task=task,
		request=body,
		response=result,
	)
	if not response.ok:
		frappe.throw(f"{provider} voice judge failed with HTTP {response.status_code}: {response.text[:500]}")
	data = response.json()
	if isinstance(data, dict) and isinstance(data.get("decision"), dict):
		return data["decision"]
	if isinstance(data, dict):
		return data
	frappe.throw(f"{provider} voice judge returned non-object JSON.")


def _normalize_voice_judge_decision(value: dict) -> dict:
	def flag(key: str) -> bool:
		return value.get(key) in (True, 1, "1", "true", "True", "yes", "Yes")

	try:
		confidence = float(value.get("confidence") or 0)
	except (TypeError, ValueError):
		confidence = 0.0
	return {
		"order_details_read_fully": flag("order_details_read_fully"),
		"customer_confirmed_after_details": flag("customer_confirmed_after_details"),
		"customer_requested_change": flag("customer_requested_change"),
		"customer_cancelled": flag("customer_cancelled"),
		"confidence": max(0.0, min(1.0, confidence)),
		"reason": str(value.get("reason") or "").strip()[:1000],
	}


def _voice_review_issue_detail(workflow, transcript: str, decision: dict, reason: str) -> str:
	return (
		f"{reason}\n\n"
		f"Voice judge decision:\n{as_json(decision)}\n\n"
		f"Patient Name: {workflow.patient_name or ''}\n"
		f"Patient Mobile: {workflow.patient_mobile or ''}\n"
		f"Patient Encounter: {workflow.patient_encounter or ''}\n"
		f"Address: {workflow.address or ''}\n"
		f"Order Summary: {workflow.order_summary or ''}\n\n"
		f"Transcript:\n{str(transcript or '')[:6000]}"
	)


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


def confirm_workflow(workflow_name: str, source: str = "Unknown", extra_notes: str | None = None) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	if workflow.status in STOP_CONFIRMATION_STATES or _has_pending_correction(workflow):
		return {"status": "blocked", "reason": "correction_or_issue_exists", "workflow": workflow.name}
	notes = _confirmation_notes(workflow, source)
	if extra_notes:
		notes = f"{notes}\n\n{extra_notes}"
	args = {
		"encounter_id": workflow.patient_encounter,
		"patient_encounter": workflow.patient_encounter,
		"order_confirmation_notes": notes,
		"sr_notes": notes,
		"sr_encounter_status": "PRX Pending",
		"checklist_confirmed": True,
		"customer_final_confirmation": "yes_after_full_checklist",
	}
	try:
		result = _call_named_mcp(_workflow_settings(workflow).confirm_mcp_tool_name, args, workflow=workflow)
	except Exception as exc:
		workflow.status = "Failed"
		workflow.confirmation_notes = notes
		workflow.last_error = f"Order confirmation MCP failed after customer confirmation: {exc}"
		workflow.mcp_result_json = as_json(
			{
				"status": "failed",
				"action": "confirm_order",
				"error": str(exc),
				"customer_confirmation_preserved": True,
			}
		)
		workflow.save(ignore_permissions=True)
		frappe.db.commit()
		if source == "WhatsApp":
			_reply_mcp_failed(workflow, "confirmation")
		raise
	workflow.status = "Confirmed"
	workflow.confirmation_notes = notes
	workflow.mcp_result_json = as_json(result)
	workflow.last_error = ""
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	if source == "WhatsApp":
		_reply_confirmed(workflow)
	return {"status": "confirmed", "workflow": workflow.name, "mcp_result": result}


def create_issue_for_workflow(
	workflow_name: str,
	change_details: str,
	*,
	level_3: bool = False,
	reply_on_whatsapp: bool = False,
) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	settings = _workflow_settings(workflow)
	args = _issue_args(workflow, change_details, settings.level_3_issue_tag if level_3 else None)
	try:
		result = _call_named_mcp(settings.issue_mcp_tool_name, args, workflow=workflow)
	except Exception:
		if reply_on_whatsapp:
			_reply_mcp_failed(workflow, "correction")
		raise
	workflow.status = "Level 3 Ticket Created" if level_3 else "Issue Created"
	workflow.change_details = change_details
	workflow.level_3_issue_created = 1 if level_3 else workflow.level_3_issue_created
	workflow.erp_issue = _extract_issue_name(result)
	workflow.mcp_result_json = as_json(result)
	workflow.last_error = ""
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	if reply_on_whatsapp:
		_reply_issue_created(workflow)
	return {"status": workflow.status, "workflow": workflow.name, "mcp_result": result}


def create_level_3_issue(workflow_name: str) -> dict:
	workflow = frappe.get_doc(WORKFLOW, workflow_name)
	summary = "Customer did not pick the second order confirmation call."
	return create_issue_for_workflow(workflow.name, summary, level_3=True)


def _create_task(workflow, channel: str, task_template: str, context: dict):
	company = workflow.company or context.get("company") or ""
	batch = frappe.new_doc("AI Task Batch")
	batch.update(
		{
			"company": company,
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
			"company": company,
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
		"discount",
		"price",
		"rate",
		"quantity",
		"qty",
		"cod",
		"cash",
		"online",
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


def _reply_confirmed(workflow) -> None:
	_send_workflow_reply(
		workflow,
		"Thank you. Aapki order confirmation receive ho gayi hai. Humne Patient Encounter update kar diya hai.",
		"Order Confirmation Success Reply",
	)


def _reply_issue_created(workflow) -> None:
	_send_workflow_reply(
		workflow,
		"Correction receive ho gayi hai. Team ke liye issue create kar diya hai, aur order abhi approve nahi kiya gaya hai.",
		"Order Confirmation Issue Reply",
	)


def _reply_mcp_failed(workflow, action: str) -> None:
	message = (
		"Aapka reply receive ho gaya hai. Backend update mein technical issue aa raha hai, "
		"isliye team isko manually check karegi."
	)
	if action == "correction":
		message = (
			"Aapki correction receive ho gayi hai. Backend issue create karne mein technical issue aa raha hai, "
			"isliye team isko manually check karegi. Order abhi approve nahi kiya gaya hai."
		)
	_send_workflow_reply(workflow, message, "Order Confirmation MCP Failure Reply")


def _send_workflow_reply(workflow, text: str, title: str) -> None:
	settings = _workflow_settings(workflow)
	try:
		_send_wa_chat_message(settings, workflow, text)
	except Exception as exc:
		create_error(
			title,
			str(exc),
			source="order_confirmation",
			payload={"workflow": workflow.name},
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


def _classify_voice_result(notes: str, outcome: str | None = None) -> str:
	if outcome:
		return _classify_reply(notes, outcome)
	if _looks_like_transcript(notes):
		return _classify_voice_transcript(notes)
	return _classify_reply(notes)


def _looks_like_transcript(text: str) -> bool:
	return "[CUSTOMER]" in (text or "").upper() or "[AGENT]" in (text or "").upper()


def _classify_voice_transcript(transcript: str) -> str:
	customer_lines = _transcript_lines(transcript, "CUSTOMER")
	agent_lines = _transcript_lines(transcript, "AGENT")
	if not customer_lines:
		return "not_confirmed"

	customer_text = _normalize_reply_text(" ".join(customer_lines))
	if _has_correction_intent(customer_text):
		return "issue"
	if any(token in customer_text for token in ("cancel", "cancelled", "canceled", "nahi chahiye", "not ordered")):
		return "issue"

	if _agent_claimed_update_without_customer_confirmation(transcript):
		return "not_confirmed"

	if not _agent_asked_final_confirmation(agent_lines):
		return "not_confirmed"

	for line in customer_lines:
		normalized = _normalize_reply_text(line)
		if _is_confirmation_text(normalized) and not _has_correction_intent(normalized):
			return "confirmed"
	return "not_confirmed"


def _transcript_lines(transcript: str, speaker: str) -> list[str]:
	lines = []
	prefix = f"[{speaker.upper()}]:"
	for line in str(transcript or "").splitlines():
		text = line.strip()
		if text.upper().startswith(prefix):
			lines.append(text.split(":", 1)[1].strip())
	return lines


def _agent_asked_final_confirmation(agent_lines: list[str]) -> bool:
	agent_text = _normalize_reply_text(" ".join(agent_lines))
	return any(
		token in agent_text
		for token in (
			"sabhi order details sahi",
			"sab details sahi",
			"sari details sahi",
			"all details correct",
			"details correct",
			"order confirm kar",
			"kya ye sahi",
			"kya ye correct",
			"kya ye sari information",
			"kya ye sab",
		)
	)


def _agent_claimed_update_without_customer_confirmation(transcript: str) -> bool:
	lines = str(transcript or "").splitlines()
	for index, line in enumerate(lines):
		text = _normalize_reply_text(line)
		if not line.strip().upper().startswith("[AGENT]:"):
			continue
		if not any(token in text for token in ("update kar di", "confirm kar diya", "confirmed", "order confirm")):
			continue
		previous_customer = [
			_normalize_reply_text(prev.split(":", 1)[1])
			for prev in lines[:index]
			if prev.strip().upper().startswith("[CUSTOMER]:") and ":" in prev
		]
		if not any(_is_confirmation_text(prev) and not _has_correction_intent(prev) for prev in previous_customer):
			return True
	return False


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
		"ji ji",
		"ji okay",
		"ji ok",
		"theek hai",
		"thik hai",
		"theek h",
		"thik h",
		"sahi hai",
		"sahi h",
		"हाँ",
		"हां",
		"हा",
		"जी",
		"जी जी",
		"ठीक है",
		"ठिक है",
		"सही है",
		"ओके",
		"यस",
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
		"जी",
		"हाँ",
		"हां",
		"हा",
		"ठीक",
		"ठिक",
		"सही",
		"ओके",
		"यस",
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
	if words.intersection({"theek", "thik"}) and words.intersection({"yes", "haan", "ha", "han", "haa", "ji", "ok", "okay", "confirm", "sab", "bilkul"}):
		return True
	if words.intersection({"ठीक", "ठिक", "सही"}) and words.intersection({"हाँ", "हां", "हा", "जी", "ओके", "यस", "सब", "बिलकुल"}):
		return True
	if words.intersection({"जी"}) and words.intersection({"ओके", "ठीक", "ठिक", "सही", "हाँ", "हां", "हा", "यस"}):
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
			"haan theek",
			"han theek",
			"haa theek",
			"haan thik",
			"han thik",
			"haa thik",
			"theek hai sab",
			"thik hai sab",
			"sab theek hai",
			"sab thik hai",
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
			"जी जी ओके",
			"हाँ जी",
			"हां जी",
			"जी हाँ",
			"जी हां",
			"ठीक है सब",
			"सब ठीक है",
			"सही है",
			"सब सही",
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
	context = _workflow_context(workflow)
	source_payload = parse_json_object(workflow.source_payload_json, "Workflow Source Payload JSON") if workflow.get("source_payload_json") else {}
	payload_details = source_payload.get("payload_json") if isinstance(source_payload.get("payload_json"), dict) else {}
	source_data = {**payload_details, **source_payload}
	customer = _first_value(
		context,
		source_data,
		"customer_id",
		"customer",
		"customer_name",
		"patient",
		"patient_name",
		"order_patient_name",
	)
	raised_by = _first_value(
		context,
		source_data,
		"created_by_agent",
		"created_by",
		"owner",
		"raised_by",
		"email",
		"contact_email",
	)
	patient_label = workflow.patient_name or workflow.patient_mobile or workflow.name
	subject = f"{patient_label} - Order confirmation issue"
	if tag:
		subject = f"{patient_label} - {tag}"
	return {
		"subject": subject,
		"description": detail,
		"company": workflow.company,
		"customer": customer,
		"customer_name": workflow.patient_name,
		"customer_phone": workflow.patient_mobile,
		"raised_by": raised_by,
		"created_by_agent": raised_by,
		"source_system": context.get("source_system") or workflow.company,
		"event": context.get("event"),
		"total_amount": context.get("total_amount"),
		"total_advance_paid": context.get("total_advance_paid"),
		"remaining_amount": context.get("remaining_amount"),
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
