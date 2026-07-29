frappe.pages["shipkia-voice-lab"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({
		parent: wrapper,
		title: __("ShipKia Voice Lab"),
		single_column: true,
	});
};

frappe.pages["shipkia-voice-lab"].on_page_show = async function (wrapper) {
	const $parent = $(wrapper).find(".layout-main-section");
	$parent.empty();
	await frappe.require("/assets/confluence_ai/dist/js/shipkia_voice_lab.bundle.js");
	window.confluence_ai.voice_lab.mount($parent.get(0));
};

frappe.pages["shipkia-voice-lab"].on_page_hide = function () {
	window.confluence_ai?.voice_lab?.unmount();
};
