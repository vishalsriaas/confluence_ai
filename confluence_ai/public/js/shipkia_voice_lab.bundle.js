import { Room, RoomEvent, Track } from "livekit-client";

frappe.provide("confluence_ai.voice_lab");

let activeRoom = null;
let pollTimer = null;
let activeRunId = null;
let activePromptVersion = null;
let root = null;
const transcriptTurns = [];

const apiCall = (method, args = {}, type = "POST") =>
	frappe.call({ method: `confluence_ai.api.shipkia_voice.${method}`, args, type });

function markup() {
	return `
		<style>
			.shipkia-lab { max-width: 1180px; margin: 0 auto; color: var(--text-color); }
			.shipkia-lab__notice { padding: 12px 14px; border: 1px solid var(--border-color);
				border-radius: 8px; background: var(--bg-light-gray); margin-bottom: 14px; }
			.shipkia-lab__grid { display: grid; grid-template-columns: minmax(300px, .8fr) minmax(400px, 1.2fr);
				gap: 16px; }
			.shipkia-lab__card { border: 1px solid var(--border-color); border-radius: 10px;
				background: var(--card-bg); padding: 18px; }
			.shipkia-lab__card h3 { margin: 0 0 14px; }
			.shipkia-lab label { display: block; font-size: 12px; font-weight: 600; margin: 12px 0 5px; }
			.shipkia-lab input, .shipkia-lab select, .shipkia-lab textarea { width: 100%; border: 1px solid
				var(--border-color); border-radius: 6px; padding: 8px 10px; background: var(--control-bg); }
			.shipkia-lab__check { display: flex !important; align-items: center; gap: 8px; font-weight: 400 !important; }
			.shipkia-lab__check input { width: auto; }
			.shipkia-lab__actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
			.shipkia-lab__status { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
			.shipkia-lab__dot { width: 9px; height: 9px; border-radius: 50%; background: var(--gray-500); }
			.shipkia-lab__dot.live { background: var(--green-500); box-shadow: 0 0 0 4px var(--green-100); }
			.shipkia-lab__transcript { min-height: 310px; max-height: 480px; overflow: auto; padding: 12px;
				border-radius: 8px; background: var(--subtle-fg); white-space: pre-wrap; }
			.shipkia-lab__metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }
			.shipkia-lab__metric { padding: 8px; border-radius: 6px; background: var(--subtle-fg); font-size: 12px; }
			@media (max-width: 800px) { .shipkia-lab__grid { grid-template-columns: 1fr; } }
		</style>
		<div class="shipkia-lab">
			<div class="shipkia-lab__notice">
				This lab stores redacted final text turns and performance metrics only—never audio or LiveKit tokens.
				Sandbox is enabled by default and makes zero CRM or follow-up changes.
			</div>
			<div class="shipkia-lab__grid">
				<section class="shipkia-lab__card">
					<h3>Test setup</h3>
					<label>Scenario</label>
					<select data-field="test_case_id"><option value="">Free conversation</option></select>
					<label>Customer name</label>
					<input data-field="customer_name" value="Voice Lab Customer" />
					<label>Customer phone</label>
					<input data-field="customer_phone" value="+919999999999" />
					<label class="shipkia-lab__check">
						<input type="checkbox" data-field="sandbox" checked /> Sandbox—simulate all writes
					</label>
					<label class="shipkia-lab__check" data-integration-row hidden>
						<input type="checkbox" data-field="confirm_integration_writes" />
						I explicitly confirm this integration test may change CRM and create follow-ups
					</label>
					<div class="shipkia-lab__actions">
						<button class="btn btn-primary" data-action="start">Start microphone test</button>
						<button class="btn btn-default" data-action="restart" disabled>Restart Agent</button>
						<button class="btn btn-default" data-action="disconnect" disabled>Disconnect</button>
					</div>
					<hr />
					<h3>Review</h3>
					<label>Verdict</label>
					<select data-field="verdict"><option>Pass</option><option>Fail</option><option>Needs Work</option></select>
					<label>Issue tags (comma-separated)</label>
					<input data-field="issue_tags" placeholder="repeated-question, unsafe-claim" />
					<label>Notes</label>
					<textarea data-field="notes" rows="4"></textarea>
					<button class="btn btn-secondary btn-sm mt-3" data-action="feedback" disabled>Save feedback</button>
				</section>
				<section class="shipkia-lab__card">
					<div class="shipkia-lab__status"><span class="shipkia-lab__dot"></span>
						<strong data-status>Not connected</strong></div>
					<div class="shipkia-lab__metrics">
						<div class="shipkia-lab__metric">Response P95<br><strong data-metric="response">—</strong></div>
						<div class="shipkia-lab__metric">Tool P95<br><strong data-metric="tool">—</strong></div>
						<div class="shipkia-lab__metric">Reconnects<br><strong data-metric="reconnects">0</strong></div>
					</div>
					<div class="shipkia-lab__transcript" data-transcript>No final turns yet.</div>
					<div data-audio></div>
				</section>
			</div>
		</div>`;
}

async function loadCases() {
	const response = await apiCall("list_voice_test_cases", {}, "GET");
	activePromptVersion = response.message.prompt_version;
	const select = root.querySelector('[data-field="test_case_id"]');
	for (const testCase of response.message.cases || []) {
		const option = document.createElement("option");
		option.value = testCase.id;
		option.textContent = `${testCase.id} — ${testCase.title}`;
		select.appendChild(option);
	}
}

function setStatus(message, live = false) {
	root.querySelector("[data-status]").textContent = message;
	root.querySelector(".shipkia-lab__dot").classList.toggle("live", live);
}

function addTurn(role, text, id) {
	if (!text || transcriptTurns.some((turn) => turn.id === id)) return;
	transcriptTurns.push({ id, role, text });
	root.querySelector("[data-transcript]").textContent = transcriptTurns
		.map((turn) => `${turn.role}: ${turn.text}`)
		.join("\n");
}

async function connectRoom(session) {
	const token = session.participant_token;
	session.participant_token = null;
	activeRoom = new Room({ adaptiveStream: true, dynacast: true });
	activeRoom.on(RoomEvent.ConnectionStateChanged, (state) => setStatus(state, state === "connected"));
	activeRoom.on(RoomEvent.Reconnecting, () => setStatus("Reconnecting…"));
	activeRoom.on(RoomEvent.Reconnected, () => setStatus("Connected", true));
	activeRoom.on(RoomEvent.Disconnected, () => setStatus("Disconnected"));
	activeRoom.on(RoomEvent.TrackSubscribed, (track) => {
		if (track.kind === Track.Kind.Audio) {
			const element = track.attach();
			element.autoplay = true;
			root.querySelector("[data-audio]").appendChild(element);
		}
	});
	activeRoom.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
		for (const segment of segments || []) {
			if (!segment.final) continue;
			const role = participant?.isLocal ? "CUSTOMER" : "AGENT";
			addTurn(role, segment.text, `${participant?.identity || role}:${segment.id}`);
		}
	});
	await activeRoom.connect(session.server_url, token, { autoSubscribe: true });
	await activeRoom.localParticipant.setMicrophoneEnabled(true);
	setStatus("Connected—speak naturally", true);
}

async function start() {
	const sandbox = root.querySelector('[data-field="sandbox"]').checked;
	const confirmed = root.querySelector('[data-field="confirm_integration_writes"]').checked;
	if (!sandbox && !confirmed) {
		frappe.msgprint(__("Confirm integration writes or enable Sandbox."));
		return;
	}
	await disconnect();
	setStatus("Creating room…");
	const response = await apiCall("create_local_test_session", {
		customer_name: root.querySelector('[data-field="customer_name"]').value,
		customer_phone: root.querySelector('[data-field="customer_phone"]').value,
		test_case_id: root.querySelector('[data-field="test_case_id"]').value,
		prompt_version: activePromptVersion,
		sandbox: sandbox ? 1 : 0,
		confirm_integration_writes: confirmed ? 1 : 0,
	});
	activeRunId = response.message.run_id;
	await connectRoom(response.message);
	toggleRunActions(true);
	startPolling();
}

async function restart() {
	if (!activeRunId) return;
	await disconnect(false);
	setStatus("Restarting agent…");
	const response = await apiCall("restart_voice_test_session", { run_id: activeRunId });
	activeRunId = response.message.run_id;
	await connectRoom(response.message);
	toggleRunActions(true);
	startPolling();
}

async function disconnect(clearRun = false) {
	if (clearRun && pollTimer) window.clearInterval(pollTimer);
	if (clearRun) pollTimer = null;
	if (activeRoom) {
		await activeRoom.disconnect();
		activeRoom = null;
	}
	if (clearRun) activeRunId = null;
	else if (activeRunId) startPolling();
	if (root) toggleRunActions(Boolean(activeRunId));
}

function toggleRunActions(hasRun) {
	root.querySelector('[data-action="restart"]').disabled = !hasRun;
	root.querySelector('[data-action="disconnect"]').disabled = !activeRoom;
	root.querySelector('[data-action="feedback"]').disabled = !hasRun;
}

async function pollRun() {
	if (!activeRunId) return;
	const response = await apiCall("get_voice_test_run", { run_id: activeRunId }, "GET");
	const run = response.message.run;
	if (run.transcript) root.querySelector("[data-transcript]").textContent = run.transcript;
	root.querySelector('[data-metric="response"]').textContent = run.response_p95_ms ? `${run.response_p95_ms} ms` : "—";
	root.querySelector('[data-metric="tool"]').textContent = run.tool_p95_ms ? `${run.tool_p95_ms} ms` : "—";
	root.querySelector('[data-metric="reconnects"]').textContent = run.reconnect_count || 0;
	if (run.status === "Failed") setStatus(`Agent needs restart: ${run.failure_code || "unknown error"}`);
	else if (["Completed", "Passed", "Needs Review"].includes(run.status)) setStatus(`Run ${run.status}`);
}

function startPolling() {
	if (pollTimer) window.clearInterval(pollTimer);
	pollTimer = window.setInterval(() => pollRun().catch(() => {}), 2000);
}

async function saveFeedback() {
	const tags = root.querySelector('[data-field="issue_tags"]').value
		.split(",").map((tag) => tag.trim()).filter(Boolean);
	await apiCall("submit_voice_test_feedback", {
		run_id: activeRunId,
		verdict: root.querySelector('[data-field="verdict"]').value,
		scores: {},
		issue_tags: tags,
		notes: root.querySelector('[data-field="notes"]').value,
	});
	frappe.show_alert({ message: __("Voice Lab feedback saved"), indicator: "green" });
}

function bindEvents() {
	root.querySelector('[data-action="start"]').addEventListener("click", () => start().catch(showError));
	root.querySelector('[data-action="restart"]').addEventListener("click", () => restart().catch(showError));
	root.querySelector('[data-action="disconnect"]').addEventListener("click", () => disconnect().catch(showError));
	root.querySelector('[data-action="feedback"]').addEventListener("click", () => saveFeedback().catch(showError));
	root.querySelector('[data-field="sandbox"]').addEventListener("change", (event) => {
		root.querySelector("[data-integration-row]").hidden = event.target.checked;
	});
}

function showError(error) {
	setStatus("Error");
	frappe.msgprint({ title: __("Voice Lab error"), message: error.message || String(error), indicator: "red" });
}

confluence_ai.voice_lab.mount = async function (element) {
	await confluence_ai.voice_lab.unmount();
	root = element;
	root.innerHTML = markup();
	bindEvents();
	await loadCases();
};

confluence_ai.voice_lab.unmount = async function () {
	await disconnect(true);
	if (root) root.innerHTML = "";
	root = null;
};
