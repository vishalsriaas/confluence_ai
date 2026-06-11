import frappe
from confluence_ai.services.dispatcher import enqueue_ready_batches as run_dispatcher

def test_concurrency():
    frappe.init(site="mysite.localhost")
    frappe.connect()

    print("--- Setting up test data ---")
    
    # 1. Create Channel Account
    if not frappe.db.exists("AI Channel Account", {"account_name": "Test LiveKit"}):
        doc = frappe.new_doc("AI Channel Account")
        doc.account_name = "Test LiveKit"
        doc.channel_type = "LiveKit"
        doc.provider_type = "LiveKit"
        doc.base_url = "https://test.livekit.cloud"
        doc.endpoint_paths_json = '{"sip_trunk_id": "st_test123"}'
        doc.insert(ignore_permissions=True)
    
    account_id = frappe.db.get_value("AI Channel Account", {"account_name": "Test LiveKit"}, "name")

    # 2. Create Agent
    if not frappe.db.exists("AI Agent", {"agent_name": "Test Concurrency Agent"}):
        doc = frappe.new_doc("AI Agent")
        doc.agent_name = "Test Concurrency Agent"
        doc.system_prompt = "You are a test agent for {{ name }}."
        doc.max_concurrency = 2
        doc.allowed_channel_account = account_id
        doc.insert(ignore_permissions=True)

    agent_id = frappe.db.get_value("AI Agent", {"agent_name": "Test Concurrency Agent"}, "name")

    # Clear old tasks for this agent
    frappe.db.sql("DELETE FROM `tabAI Task` WHERE assigned_agent = %s", (agent_id,))

    if not frappe.db.exists("AI Task Template", {"template_name": "TestTemplate"}):
        tpl = frappe.new_doc("AI Task Template")
        tpl.template_name = "TestTemplate"
        tpl.template_name = "TestTemplate"
        tpl.template_key = "test_template"
        tpl.objective_prompt = "Test Objective"
        tpl.target_agent = agent_id
        tpl.insert(ignore_permissions=True)
    
    tpl_id = frappe.db.get_value("AI Task Template", {"template_key": "test_template"}, "name")
    
    # 3. Create Task Batch
    batch = frappe.new_doc("AI Task Batch")
    batch.source_system = "TestSystem"
    batch.task_template = tpl_id
    batch.insert(ignore_permissions=True)
    batch_id = batch.name

    # 4. Create 5 Tasks
    tasks = []
    for i in range(1, 6):
        task = frappe.new_doc("AI Task")
        task.task_batch = batch_id
        task.task_template = tpl_id
        task.assigned_agent = agent_id
        task.status = "Queued"
        task.payload_json = frappe.as_json({"phone": f"+91000000000{i}", "customer_name": f"User {i}"})
        task.insert(ignore_permissions=True)
        tasks.append(task.name)
    
    frappe.db.commit()
    print(f"Created 5 tasks: {tasks}")

    # 5. Run dispatcher cycle 1
    print("\n--- Running Dispatcher Cycle 1 ---")
    run_dispatcher()
    frappe.db.commit()
    
    # Wait a tiny bit for DB to sync state
    import time
    time.sleep(1)

    print("Status after Cycle 1 (Should be 2 Waiting, 3 Queued):")
    for t in tasks:
        print(f"Task {t}: {frappe.db.get_value('AI Task', t, 'status')}")

    print("\nWaiting 5 seconds for Frappe background workers to execute the 2 Waiting tasks...")
    print("(They will fail because the LiveKit URL is dummy)")
    time.sleep(5)
    
    print("Status after Background Execution:")
    for t in tasks:
        print(f"Task {t}: {frappe.db.get_value('AI Task', t, 'status')}")

    # 6. Run dispatcher cycle 2
    print("\n--- Running Dispatcher Cycle 2 ---")
    run_dispatcher()
    frappe.db.commit()
    
    print("Status after Cycle 2 (Should be 2 Failed, 2 Waiting, 1 Queued):")
    for t in tasks:
        print(f"Task {t}: {frappe.db.get_value('AI Task', t, 'status')}")
        
    print("\n--- Concurrency Test Complete ---")
