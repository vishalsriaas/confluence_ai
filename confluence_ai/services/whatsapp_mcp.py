from __future__ import annotations

import json
from typing import Any

import frappe

from confluence_ai.services.utils import as_json, record_provider_event


def send_whatsapp_message(arguments: dict[str, Any], task_id: str | None = None) -> dict:
	"""Send a WhatsApp message from an MCP tool through WA Chat Hub."""
	from wa_chat_hub.outbound import send_interakt_template_message, send_outbound_message
	from wa_chat_hub.security import set_ai_security_context, set_service_user_context
	from wa_chat_hub.services import append_message

	args = frappe._dict(arguments or {})
	phone = args.get("phone_number") or args.get("customer_phone") or args.get("to")
	body = args.get("message") or args.get("body") or args.get("text")
	template_name = args.get("template_name")
	channel_account = args.get("channel_account") or _default_chat_channel_account()
	if not phone:
		frappe.throw("phone_number is required")
	if not channel_account:
		frappe.throw("channel_account is required or an active Chat Channel Account must exist")
	if not body and not template_name:
		frappe.throw("message/body or template_name is required")

	set_service_user_context("confluence_mcp_whatsapp")
	set_ai_security_context(channel_account=channel_account, operation="confluence_mcp_whatsapp")

	result = append_message(
		{
			"channel_account": channel_account,
			"phone_number": phone,
			"display_name": args.get("customer_name") or args.get("display_name"),
			"direction": "Outbound",
			"sender_type": "AI",
			"content_type": "Template" if template_name else args.get("content_type") or "Text",
			"body": body or f"Template: {template_name}",
			"delivery_status": "Pending",
			"raw_payload": {
				"source": "confluence_mcp",
				"task": task_id,
				"template_name": template_name,
			},
		}
	)
	conversation = result.get("conversation")
	if not conversation:
		frappe.throw("WA Chat Hub did not return a conversation")

	if template_name:
		outbound = send_interakt_template_message(
			conversation,
			{
				"template_name": template_name,
				"language_code": args.get("language_code") or "en",
				"header_values": _list_arg(args.get("header_values")),
				"body_values": _list_arg(args.get("body_values")),
				"button_values": _dict_arg(args.get("button_values")),
				"button_payload": _dict_arg(args.get("button_payload")),
				"file_name": args.get("file_name"),
				"callback_data": args.get("callback_data") or as_json({"task": task_id, "source": "confluence_mcp"}),
				"campaign_id": args.get("campaign_id"),
				"template_category": args.get("template_category"),
			},
		)
	else:
		outbound = send_outbound_message(
			conversation,
			body,
			args.get("content_type") or "Text",
			args.get("media_url"),
			file_name=args.get("file_name"),
		)

	updates = {
		"delivery_status": outbound.get("delivery_status") or "Sent",
		"raw_transport_payload": as_json(outbound),
	}
	provider_message_id = outbound.get("provider_message_id")
	if provider_message_id and frappe.get_meta("Chat Message").has_field("channel_message_id"):
		updates["channel_message_id"] = provider_message_id
	frappe.db.set_value("Chat Message", result.get("message"), updates, update_modified=True)

	response = {
		"status": "success",
		"conversation": conversation,
		"message": result.get("message"),
		"delivery_status": updates["delivery_status"],
		"provider_message_id": provider_message_id,
	}
	record_provider_event(
		provider="MCP",
		operation="send_whatsapp_message",
		status="Succeeded",
		task=task_id,
		request={k: v for k, v in args.items() if "key" not in k.lower() and "token" not in k.lower()},
		response=response,
	)
	return response


def _default_chat_channel_account() -> str | None:
	return frappe.db.get_value("Chat Channel Account", {"is_active": 1}, "name", order_by="modified desc")


def _list_arg(value) -> list:
	if value in (None, ""):
		return []
	if isinstance(value, list):
		return value
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
			return parsed if isinstance(parsed, list) else [value]
		except Exception:
			return [value]
	return [value]


def _dict_arg(value) -> dict:
	if value in (None, ""):
		return {}
	if isinstance(value, dict):
		return value
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
			return parsed if isinstance(parsed, dict) else {}
		except Exception:
			return {}
	return {}
