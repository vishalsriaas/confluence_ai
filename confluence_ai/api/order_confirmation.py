from __future__ import annotations

import json

import frappe

from confluence_ai.services import order_confirmation


def _request_payload() -> dict:
	if frappe.request and frappe.request.data:
		try:
			return json.loads(frappe.request.data.decode("utf-8"))
		except Exception:
			pass
	return frappe.local.form_dict or {}


@frappe.whitelist()
def start(**kwargs) -> dict:
	"""Start the reusable order confirmation flow from an API/event payload."""
	payload = kwargs or _request_payload()
	return order_confirmation.start_from_event(dict(payload))


@frappe.whitelist()
def receive(**kwargs) -> dict:
	"""Alias used by event systems that post order-confirmation payloads."""
	return start(**kwargs)


@frappe.whitelist()
def whatsapp_reply(
	workflow: str | None = None,
	message: str | None = None,
	conversation: str | None = None,
	outcome: str | None = None,
	change_details: str | None = None,
) -> dict:
	payload = _request_payload()
	return order_confirmation.handle_whatsapp_reply(
		workflow=workflow or payload.get("workflow"),
		message=message or payload.get("message") or payload.get("body"),
		conversation=conversation or payload.get("conversation"),
		outcome=outcome or payload.get("outcome"),
		change_details=change_details or payload.get("change_details"),
	)


@frappe.whitelist()
def voice_result(
	workflow: str | None = None,
	task: str | None = None,
	outcome: str | None = None,
	notes: str | None = None,
) -> dict:
	payload = _request_payload()
	return order_confirmation.handle_voice_result(
		workflow=workflow or payload.get("workflow"),
		task=task or payload.get("task"),
		outcome=outcome or payload.get("outcome"),
		notes=notes or payload.get("notes") or payload.get("transcript") or payload.get("summary"),
	)


@frappe.whitelist()
def mark_call_missed(workflow: str, notes: str | None = None) -> dict:
	return order_confirmation.mark_call_missed(workflow, notes)


@frappe.whitelist()
def process_due() -> dict:
	return order_confirmation.process_due_workflows()
