(function () {
  const STORAGE_KEY = 'confluence_ai_company';
  const TENANT_DOCTYPES = new Set([
    'AI Access Token',
    'AI ACP Event',
    'AI Agent',
    'AI Agent Group',
    'AI Call Log',
    'AI Channel Account',
    'AI Error Log',
    'AI Event Route',
    'AI Knowledge Category',
    'AI Knowledge Chunk',
    'AI Knowledge Document',
    'AI MCP Server',
    'AI MCP Tool',
    'AI Provider Event',
    'AI Sales Call Outcome',
    'AI Sales Disease Route',
    'AI Sales Follow Up',
    'AI Sales Lead',
    'AI Task',
    'AI Task Attempt',
    'AI Task Batch',
    'AI Task Template',
    'AI Tool Permission',
    'AI Webhook Event',
    'AI WhatsApp Template Map',
    'Order Confirmation Workflow',
    'Chat Action Log',
    'Chat AI Suggestion',
    'Chat Assignment Rule',
    'Chat Channel Account',
    'Chat Channel Session',
    'Chat Contact',
    'Chat Contact Channel Profile',
    'Chat Conversation',
    'Chat Message',
    'Chat Queue Event',
    'WA AI Knowledge Base',
    'WA AI Tool Permission',
    'WA Channel Context',
    'WA Channel Pipeline Map',
    'WA Lead AI Insight',
    'WA Lead OCR Result',
    'WA LLM Provider',
    'WA MCP Server',
    'WA MCP Tool Endpoint'
  ]);
  let tenantInfo = null;

  function currentCompany(callback) {
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

  function applyCompany(frm) {
    if (!frm || !frm.fields_dict || !frm.fields_dict.company) return;

    currentCompany((info) => {
      const company = info.company || '';
      const locked = !!info.locked;

      frm.set_query('company', () => ({
        filters: company ? { enabled: 1, name: company } : { enabled: 1 }
      }));

      if (company && (!frm.doc.company || (locked && frm.doc.company !== company))) {
        frm.set_value('company', company);
      }

      frm.set_df_property('company', 'read_only', locked ? 1 : 0);
      applyTenantLinkFilters(frm, company);
      frm.refresh_field('company');
    });
  }

  function tenantLinkQuery(company) {
    return () => ({
      filters: company ? { company } : {}
    });
  }

  function applyTenantLinkFilters(frm, company) {
    if (!frm || !frm.meta || !company) return;

    (frm.meta.fields || []).forEach((field) => {
      if (field.fieldtype === 'Link' && field.fieldname !== 'company' && TENANT_DOCTYPES.has(field.options)) {
        frm.set_query(field.fieldname, tenantLinkQuery(company));
      }

      if (field.fieldtype === 'Table' && frm.fields_dict[field.fieldname] && frm.fields_dict[field.fieldname].grid) {
        const grid = frm.fields_dict[field.fieldname].grid;
        (grid.docfields || []).forEach((childField) => {
          if (childField.fieldtype === 'Link' && TENANT_DOCTYPES.has(childField.options)) {
            frm.set_query(childField.fieldname, field.fieldname, tenantLinkQuery(company));
          }
        });
      }
    });
  }

  const route = frappe.get_route ? frappe.get_route() : [];
  const doctype = window.cur_frm && window.cur_frm.doctype ? window.cur_frm.doctype : route[1];

  if (!doctype) return;

  frappe.ui.form.on(doctype, {
    setup(frm) {
      applyCompany(frm);
    },
    onload(frm) {
      applyCompany(frm);
    },
    refresh(frm) {
      applyCompany(frm);
    }
  });
})();
