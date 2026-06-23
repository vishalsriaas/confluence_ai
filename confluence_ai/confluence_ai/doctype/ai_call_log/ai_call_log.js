frappe.ui.form.on('AI Call Log', {
    refresh(frm) {
        const url = frm.doc.recording_url || frm.doc.external_recording_url;
        const wrapper = frm.fields_dict.audio_player && frm.fields_dict.audio_player.$wrapper;
        if (!wrapper) return;
        wrapper.empty();
        if (!url) {
            wrapper.html('<div class="text-muted">No recording available.</div>');
            return;
        }
        const escaped = frappe.utils.escape_html(url);
        wrapper.html(`
            <div class="mb-2">
                <audio controls preload="metadata" style="width: 100%; max-width: 720px;">
                    <source src="${escaped}">
                    Your browser does not support audio playback.
                </audio>
            </div>
            <a href="${escaped}" target="_blank" rel="noopener">Open recording</a>
        `);
    }
});
