import unittest
import frappe
import json
from confluence_ai.services import event_router

class TestEventRouter(unittest.TestCase):
    def setUp(self):
        # Create a dummy agent if not present
        existing_agent = frappe.db.get_value("AI Agent", {"agent_name": "Test Agent"}, "name")
        if existing_agent:
            self.agent_name = existing_agent
        else:
            agent = frappe.new_doc("AI Agent")
            agent.agent_name = "Test Agent"
            agent.channel_type = "Voice"
            agent.system_prompt = "You are a helpful assistant."
            agent.insert(ignore_permissions=True)
            self.agent_name = agent.name
            
        # Create a dummy template if not present
        existing_tmpl = frappe.db.get_value("AI Task Template", {"template_key": "test_event_router_template_key"}, "name")
        if existing_tmpl:
            self.template_name = existing_tmpl
        else:
            tmpl = frappe.new_doc("AI Task Template")
            tmpl.template_name = "Test Template"
            tmpl.template_key = "test_event_router_template_key"
            tmpl.objective_prompt = "Test Objective Prompt"
            tmpl.insert(ignore_permissions=True)
            self.template_name = tmpl.name

    def tearDown(self):
        # Cleanup created event routes and tasks to avoid test pollution
        frappe.db.delete("AI Event Route")
        frappe.db.delete("AI Task")
        frappe.db.delete("AI Task Batch")
        frappe.db.commit()

    def test_find_matching_route(self):
        # Create an event route
        route = frappe.new_doc("AI Event Route")
        route.route_name = "Test Immediate Route"
        route.enabled = 1
        route.event_key_field = "event"
        route.event_value = "order_confirmed"
        route.task_template = self.template_name
        route.target_agent = self.agent_name
        route.dispatch_mode = "Immediate"
        route.insert(ignore_permissions=True)

        payload = {"event": "order_confirmed", "order_id": "123"}
        matched = event_router.find_matching_route(payload)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.route_name, "Test Immediate Route")

        # Test non-matching event
        payload_wrong = {"event": "order_cancelled", "order_id": "123"}
        matched_wrong = event_router.find_matching_route(payload_wrong)
        self.assertIsNone(matched_wrong)

        # Test source_system filter
        route_source = frappe.new_doc("AI Event Route")
        route_source.route_name = "Test Source Route"
        route_source.enabled = 1
        route_source.event_key_field = "event"
        route_source.event_value = "order_confirmed"
        route_source.source_system = "ERPNext"
        route_source.task_template = self.template_name
        route_source.target_agent = self.agent_name
        route_source.dispatch_mode = "Immediate"
        route_source.insert(ignore_permissions=True)

        # Match with no source system (should match the first one, which is the non-source route)
        matched_no_source = event_router.find_matching_route(payload)
        self.assertEqual(matched_no_source.route_name, "Test Immediate Route")

        # Match with source system
        matched_source = event_router.find_matching_route(payload, source_system="ERPNext")
        self.assertEqual(matched_source.route_name, "Test Source Route")

    def test_validate_route_auth(self):
        route = frappe.new_doc("AI Event Route")
        route.route_name = "Test Auth Route"
        route.enabled = 1
        route.event_key_field = "event"
        route.event_value = "order_confirmed"
        route.task_template = self.template_name
        route.target_agent = self.agent_name
        route.dispatch_mode = "Immediate"
        route.webhook_secret = "super_secret_key"
        route.insert(ignore_permissions=True)

        # No header
        self.assertFalse(event_router.validate_route_auth(route, {}))
        # Wrong header
        self.assertFalse(event_router.validate_route_auth(route, {"X-Webhook-Secret": "wrong"}))
        # Correct header
        self.assertTrue(event_router.validate_route_auth(route, {"X-Webhook-Secret": "super_secret_key"}))

    def test_build_context_with_mappings(self):
        route = frappe.new_doc("AI Event Route")
        route.route_name = "Test Context Route"
        route.enabled = 1
        route.event_key_field = "event"
        route.event_value = "order_confirmed"
        route.task_template = self.template_name
        route.target_agent = self.agent_name
        route.dispatch_mode = "Immediate"
        
        # Add field mappings
        route.append("field_mappings", {
            "source_field": "order_id",
            "target_field": "id",
            "value_type": "From Payload",
            "transformation": "None"
        })
        route.append("field_mappings", {
            "source_field": "customer_name",
            "target_field": "name",
            "value_type": "From Payload",
            "transformation": "Uppercase"
        })
        route.append("field_mappings", {
            "target_field": "is_test",
            "value_type": "Static Value",
            "static_value": "yes",
            "transformation": "None"
        })
        route.insert(ignore_permissions=True)

        payload = {
            "event": "order_confirmed",
            "order_id": "ORD-1234",
            "customer_name": "john doe"
        }
        context = event_router.build_context(route, payload)
        self.assertEqual(context.get("id"), "ORD-1234")
        self.assertEqual(context.get("name"), "JOHN DOE")
        self.assertEqual(context.get("is_test"), "yes")

    def test_dispatch_immediate(self):
        route = frappe.new_doc("AI Event Route")
        route.route_name = "Test Immediate Dispatch"
        route.enabled = 1
        route.event_key_field = "event"
        route.event_value = "order_confirmed"
        route.task_template = self.template_name
        route.target_agent = self.agent_name
        route.dispatch_mode = "Immediate"
        route.priority = "High"
        route.idempotency_key_field = "order_id"
        route.insert(ignore_permissions=True)

        payload = {
            "event": "order_confirmed",
            "order_id": "ORD-555"
        }

        # Dispatch first time
        res = event_router.dispatch_from_route(route, payload)
        self.assertEqual(res.get("status"), "queued")
        self.assertEqual(res.get("priority"), "High")
        self.assertIsNotNone(res.get("task"))

        # Verify task is created in DB
        task = frappe.get_doc("AI Task", res["task"])
        self.assertEqual(task.status, "Queued")
        self.assertEqual(task.priority, "High")
        self.assertEqual(task.idempotency_key, f"{route.name}:ORD-555")

        # Dispatch second time (duplicate protection)
        res_dup = event_router.dispatch_from_route(route, payload)
        self.assertEqual(res_dup.get("status"), "duplicate")
        self.assertEqual(res_dup.get("task"), res["task"])

    def test_dispatch_batch(self):
        route = frappe.new_doc("AI Event Route")
        route.route_name = "Test Batch Dispatch"
        route.enabled = 1
        route.event_key_field = "event"
        route.event_value = "followup_batch"
        route.task_template = self.template_name
        route.target_agent = self.agent_name
        route.dispatch_mode = "Batch"
        route.batch_records_field = "records"
        route.batch_label = "Monday Followups"
        route.priority = "Normal"
        route.insert(ignore_permissions=True)

        payload = {
            "event": "followup_batch",
            "records": [
                {"phone": "+910000000001", "id": "rec-1"},
                {"phone": "+910000000002", "id": "rec-2"}
            ]
        }

        res = event_router.dispatch_from_route(route, payload)
        self.assertEqual(res.get("status"), "queued")
        self.assertEqual(res.get("records"), 2)
        self.assertEqual(res.get("batch_label"), "Monday Followups")

        # Verify batch and tasks in DB
        batch_name = res["batch"]
        batch = frappe.get_doc("AI Task Batch", batch_name)
        self.assertEqual(batch.batch_label, "Monday Followups")

        tasks = frappe.get_all("AI Task", filters={"task_batch": batch_name})
        self.assertEqual(len(tasks), 2)

