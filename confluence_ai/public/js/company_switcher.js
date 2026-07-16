
(() => {
  const TENANT_DOCTYPES = [
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
  ];

  const STORAGE_KEY = 'confluence_ai_company';
  let currentCompany = window.localStorage.getItem(STORAGE_KEY) || '';
  let companies = [];
  let companyLocked = false;

  function isTenantDoctype(doctype) {
    return TENANT_DOCTYPES.includes(doctype);
  }

  function optionLabel(company) {
    return company.company_name || company.name;
  }

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value || '';
    return div.innerHTML;
  }

  function setRouteCompanyFilter(doctype) {
    if (!currentCompany || !isTenantDoctype(doctype)) return;
    frappe.route_options = frappe.route_options || {};
    frappe.route_options.company = currentCompany;
  }

  function refreshCurrentView() {
    const route = frappe.get_route();
    if (route[0] === 'List' && isTenantDoctype(route[1])) {
      setRouteCompanyFilter(route[1]);
      frappe.set_route('List', route[1], route[2] || 'List');
      setTimeout(() => cur_list && cur_list.refresh(), 250);
      return;
    }
    if (cur_frm && isTenantDoctype(cur_frm.doctype)) {
      if (cur_frm.is_new() && currentCompany && !cur_frm.doc.company) {
        cur_frm.set_value('company', currentCompany);
      }
      if (!cur_frm.is_dirty()) {
        cur_frm.reload_doc();
      }
    }
  }

  function installListSettings() {
    TENANT_DOCTYPES.forEach((doctype) => {
      frappe.listview_settings[doctype] = frappe.listview_settings[doctype] || {};
      const existingOnload = frappe.listview_settings[doctype].onload;
      frappe.listview_settings[doctype].onload = function(listview) {
        if (currentCompany) {
          listview.filter_area.add([[doctype, 'company', '=', currentCompany]]);
        }
        if (existingOnload) existingOnload(listview);
      };
    });
  }

  function installFormDefaults() {
    TENANT_DOCTYPES.forEach((doctype) => {
      frappe.ui.form.on(doctype, {
        setup(frm) {
          if (frm.fields_dict.company) {
            frm.set_query('company', () => ({ filters: { enabled: 1 } }));
          }
        },
        onload(frm) {
          if (frm.is_new() && currentCompany && frm.fields_dict.company && !frm.doc.company) {
            frm.set_value('company', currentCompany);
          }
        }
      });
    });
  }

  function currentCompanyLabel() {
    const selected = companies.find((company) => company.name === currentCompany);
    return selected ? optionLabel(selected) : 'All Companies';
  }

  function chooseCompany(company) {
    currentCompany = company || '';
    window.localStorage.setItem(STORAGE_KEY, currentCompany);
    frappe.call({
      method: 'confluence_ai.tenant.set_user_company',
      args: { company: currentCompany },
      callback() {
        frappe.show_alert({
          message: currentCompany ? `Company switched to ${currentCompanyLabel()}` : 'Company filter cleared',
          indicator: 'green'
        });
        document.querySelectorAll('.confluence-ai-company-switcher').forEach((node) => node.remove());
        renderSelector();
        refreshCurrentView();
      }
    });
  }

  function workspaceTarget() {
    const route = frappe.get_route();
    const isTenantWorkspace =
      route.includes('Confluence AI') ||
      route.includes('WhatsApp') ||
      document.body.innerText.includes('Confluence AI Management') ||
      document.body.innerText.includes('WhatsApp Operations Hub');
    if (!isTenantWorkspace) return null;

    const candidates = [
      '.layout-main-section .widget',
      '.layout-main-section .workspace-section',
      '.layout-main-section .ce-block',
      '.layout-main-section',
      '.page-content',
    ];
    for (const selector of candidates) {
      const node = document.querySelector(selector);
      if (node) return node;
    }
    return null;
  }

  function navbarTarget() {
    const searchInput = document.querySelector(
      '.navbar input[type="search"], .navbar input[placeholder*="Search"], input[placeholder*="Search or type"]'
    );
    const searchBox = searchInput && (
      searchInput.closest('.search-bar') ||
      searchInput.closest('.awesomplete') ||
      searchInput.closest('.input-group') ||
      searchInput.parentElement
    );
    if (searchBox && searchBox.parentElement) {
      return { parent: searchBox.parentElement, before: searchBox };
    }

    const navbar = document.querySelector('.navbar, header .navbar');
    return navbar ? { parent: navbar, before: null } : null;
  }

  function renderSelector() {
    if (!frappe.boot || frappe.boot.user === 'Guest') return;
    document.querySelectorAll('.confluence-ai-company-switcher').forEach((node) => node.remove());

    const wrapper = document.createElement('div');
    wrapper.className = 'confluence-ai-company-switcher';
    wrapper.style.display = 'flex';
    wrapper.style.alignItems = 'center';
    wrapper.style.justifyContent = 'flex-end';
    wrapper.style.gap = '8px';
    wrapper.style.zIndex = '20';

    const group = document.createElement('div');
    group.className = 'dropdown';
    group.style.position = 'relative';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-default btn-sm dropdown-toggle';
    button.setAttribute('data-toggle', 'dropdown');
    button.setAttribute('aria-expanded', 'false');
    button.disabled = companyLocked;
    button.title = companyLocked ? 'Company access is assigned by admin' : '';
    button.style.minWidth = '190px';
    button.style.height = '34px';
    button.style.display = 'inline-flex';
    button.style.alignItems = 'center';
    button.style.justifyContent = 'space-between';
    button.style.gap = '8px';
    button.innerHTML = `<span>Company: <b>${escapeHtml(currentCompanyLabel())}</b></span><span class="caret"></span>`;

    const menu = document.createElement('ul');
    menu.className = 'dropdown-menu dropdown-menu-right';
    menu.style.maxHeight = '280px';
    menu.style.overflowY = 'auto';
    menu.style.minWidth = '220px';

    const allItem = document.createElement('li');
    allItem.innerHTML = `<a class="dropdown-item" href="#">All Companies</a>`;
    allItem.addEventListener('click', (event) => {
      event.preventDefault();
      chooseCompany('');
    });
    menu.appendChild(allItem);

    companies.forEach((company) => {
      const item = document.createElement('li');
      const active = company.name === currentCompany ? ' ✓' : '';
      item.innerHTML = `<a class="dropdown-item" href="#">${escapeHtml(optionLabel(company))}${active}</a>`;
      item.addEventListener('click', (event) => {
        event.preventDefault();
        chooseCompany(company.name);
      });
      menu.appendChild(item);
    });

    group.appendChild(button);
    group.appendChild(menu);
    wrapper.appendChild(group);

    const navTarget = navbarTarget();
    if (navTarget) {
      wrapper.style.marginLeft = '8px';
      wrapper.style.marginRight = '12px';
      wrapper.style.position = 'relative';
      wrapper.style.flex = '0 0 auto';
      navTarget.parent.insertBefore(wrapper, navTarget.before);
      return;
    }

    const target = workspaceTarget();
    if (target) {
      target.style.position = target.style.position || 'relative';
      wrapper.style.position = 'absolute';
      wrapper.style.top = '18px';
      wrapper.style.right = '26px';
      wrapper.style.background = 'var(--card-bg, #fff)';
      wrapper.style.padding = '4px';
      wrapper.style.borderRadius = '8px';
      target.appendChild(wrapper);
      return;
    }

    wrapper.style.position = 'fixed';
    wrapper.style.top = '72px';
    wrapper.style.right = '72px';
    wrapper.style.background = '#fff';
    wrapper.style.padding = '6px 8px';
    wrapper.style.border = '1px solid var(--border-color, #d1d8dd)';
    wrapper.style.borderRadius = '8px';
    wrapper.style.boxShadow = '0 4px 14px rgba(0, 0, 0, 0.08)';
    document.body.appendChild(wrapper);
  }

  function loadCompanies() {
    frappe.call({
      method: 'confluence_ai.tenant.get_company_options',
      callback(response) {
        const data = response.message || {};
        companies = data.companies || [];
        currentCompany = data.current_company || currentCompany || '';
        companyLocked = !!data.locked;
        window.localStorage.setItem(STORAGE_KEY, currentCompany);
        renderSelector();
      }
    });
  }

  let initialized = false;

  function initCompanySwitcher() {
    if (initialized || !window.frappe || !frappe.boot) return;
    initialized = true;
    installListSettings();
    installFormDefaults();
    loadCompanies();
    setTimeout(renderSelector, 1000);
    setTimeout(renderSelector, 2500);

    frappe.router.on('change', () => {
      const route = frappe.get_route();
      if (route[0] === 'List') setRouteCompanyFilter(route[1]);
      setTimeout(renderSelector, 200);
    });
  }

  if (window.frappe && frappe.ready) {
    frappe.ready(initCompanySwitcher);
  }
  setTimeout(initCompanySwitcher, 100);
  setTimeout(initCompanySwitcher, 800);
})();
