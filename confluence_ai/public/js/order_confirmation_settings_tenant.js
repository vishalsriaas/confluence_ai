(function () {
  const STORAGE_KEY = 'confluence_ai_company';
  const TENANT_DOCTYPES = new Set([
    'AI Agent',
    'AI Channel Account',
    'AI Task Template',
    'Chat Channel Account'
  ]);
  const GLOBAL_FIELDS = [
    'enabled',
    'default_company',
    'default_whatsapp_wait_minutes',
    'default_retry_delay_minutes',
    'voice_call_timeout_minutes',
    'max_voice_attempts',
    'level_3_issue_tag',
    'channel_account',
    'wa_chat_channel_account',
    'voice_agent',
    'whatsapp_task_template',
    'voice_task_template',
    'send_first_whatsapp_as_template',
    'whatsapp_template_name',
    'whatsapp_template_language',
    'confirm_mcp_tool_name',
    'issue_mcp_tool_name',
    'whatsapp_prompt_template'
  ];
  let tenantInfo = null;

  function getTenantInfo(callback) {
    if (tenantInfo) {
      callback(tenantInfo);
      return;
    }

    frappe.call({
      method: 'confluence_ai.tenant.get_company_options',
      callback(response) {
        const data = response.message || {};
        const company = data.current_company || window.localStorage.getItem(STORAGE_KEY) || '';
        if (company) {
          window.localStorage.setItem(STORAGE_KEY, company);
        }
        tenantInfo = {
          company,
          locked: !!data.locked
        };
        callback(tenantInfo);
      }
    });
  }

  function tenantLinkQuery(company) {
    return () => ({
      filters: company ? { company } : {}
    });
  }

  function applyChildGridFilters(frm, company) {
    if (!frm.fields_dict.company_settings || !frm.fields_dict.company_settings.grid) return;

    const grid = frm.fields_dict.company_settings.grid;
    frm.set_query('company', 'company_settings', () => ({
      filters: { enabled: 1, name: company }
    }));

    (grid.docfields || []).forEach((field) => {
      if (field.fieldname === 'company') {
        grid.update_docfield_property('company', 'read_only', 1);
      }
      if (field.fieldtype === 'Link' && TENANT_DOCTYPES.has(field.options)) {
        frm.set_query(field.fieldname, 'company_settings', tenantLinkQuery(company));
      }
    });
  }

  function showOnlyAssignedCompanyRow(frm, company) {
    const rows = frm.doc.company_settings || [];
    frm.doc.company_settings = rows.filter((row) => !row.company || row.company === company);

    if (!frm.doc.company_settings.length) {
      const row = frm.add_child('company_settings');
      row.enabled = 1;
      row.company = company;
    }

    frm.doc.company_settings.forEach((row) => {
      row.company = company;
    });
    frm.refresh_field('company_settings');
  }

  function applyTenantLock(frm) {
    getTenantInfo((info) => {
      const company = info.company || '';
      const locked = !!info.locked;
      if (!company) return;

      frm.set_query('default_company', () => ({
        filters: locked ? { enabled: 1, name: company } : { enabled: 1 }
      }));

      if (!locked) return;

      GLOBAL_FIELDS.forEach((fieldname) => {
        if (frm.fields_dict[fieldname]) {
          frm.set_df_property(fieldname, 'read_only', 1);
        }
      });

      applyChildGridFilters(frm, company);
      showOnlyAssignedCompanyRow(frm, company);
    });
  }

  frappe.ui.form.on('Order Confirmation Settings', {
    setup(frm) {
      applyTenantLock(frm);
    },
    onload(frm) {
      applyTenantLock(frm);
    },
    refresh(frm) {
      applyTenantLock(frm);
    }
  });
})();
