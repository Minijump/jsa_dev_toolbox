"""Where things are on the screens of Odoo 19 (community and enterprise web clients).

Everything that depends on the Odoo version is here: to support another version, copy this file to odoo_<version>.py
and adapt it. The keywords (OdooKeywords.py) use the most recent file that is not newer than the tested Odoo.
`{text}` is replaced by the label given in the test.
"""

# exact labels are matched with xpath normalize-space(): the text of a button or menu is often inside an inner element
SELECTORS = {
    # login page
    "login": "input[name=login]",
    "password": "input[name=password]",
    "login_submit": "form.oe_login_form button[type=submit]",
    "logged_in": ".o_main_navbar",

    # menus. The apps are in a dropdown of the top bar (community), or on a page of their own, the home menu
    # (enterprise): it is the page shown after the login, and the button of the top bar opens it from anywhere else
    "home_menu": ".o_home_menu",
    "apps_toggle": ":is(.o_navbar_apps_menu button, .o_main_navbar .o_menu_toggle) >> nth=0",
    "app": ':is(.o-dropdown--menu, .o_home_menu) >> xpath=.//a[contains(@class, "o_app")][normalize-space(.)="{text}"]',
    "menu_section": '.o_menu_sections >> xpath=./*[normalize-space(.)="{text}"]',
    "menu_item": '.o-dropdown--menu >> xpath=.//*[contains(@class, "dropdown-item")][normalize-space(.)="{text}"] >> nth=0',
    "view": ".o_action_manager .o_view_controller",

    # the page is busy while one of these is on screen
    "busy": ".o_loading_indicator, .o_blockUI, .o_ui_blocked",

    # lists, kanbans and forms
    "new_record": ".o_control_panel :is(button.o_list_button_add, button.o_form_button_create, button.o-kanban-button-new):visible >> nth=0",
    "form": ".o_form_view",
    "form_save": ".o_form_view .o_form_button_save:visible",
    "form_dirty": ".o_form_view .o_form_status_indicator_buttons:not(.invisible)",
    "record": ':is(.o_data_row, .o_kanban_record):has-text("{text}") >> nth=0',
    "search": ".o_control_panel .o_searchview_input",
    "button": 'xpath=//*[self::button or self::a[contains(@class, "btn")]][normalize-space(.)="{text}"] >> visible=true >> nth=0',
    "dialog": ".modal.show",
    "status": ".o_statusbar_status button.o_arrow_button_current",

    # many2one dropdown
    "choice_exact": '.o-autocomplete--dropdown-menu >> xpath=.//*[contains(@class, "o-autocomplete--dropdown-item")][normalize-space(.)="{text}"] >> nth=0',
    "choice": '.o-autocomplete--dropdown-menu .o-autocomplete--dropdown-item:has-text("{text}") >> nth=0',

    # lines of a form (one2many list)
    "lines": ".o_form_view .o_field_x2many_list:visible >> nth=0",
    "add_line": ".o_form_view .o_field_x2many_list:visible >> nth=0 >> .o_field_x2many_list_row_add a >> nth=0",
    "line_cell": '.o_form_view .o_data_row.o_selected_row td[name="{text}"]',
}

# find a field of the form on screen from the words the user sees: its label, else its placeholder, else its technical name
FIND_FIELD = r"""(label) => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*\?$/, '');
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const root = document.querySelector('.modal.show .o_form_view') || document.querySelector('.o_form_view');
  if (!root) return {error: 'there is no form on the screen'};
  let target = null;
  for (const l of root.querySelectorAll('label.o_form_label')) {
    if (!visible(l) || norm(l.textContent) !== label || !l.getAttribute('for')) continue;
    // a read-only field has no element with that id: take the field itself ("partner_id_0" is the field "partner_id")
    target = document.getElementById(l.getAttribute('for')) || root.querySelector('.o_field_widget[name="' + l.getAttribute('for').replace(/_\d+$/, '') + '"]');
    if (target) break;
  }
  if (!target) target = [...root.querySelectorAll('input[placeholder], textarea[placeholder]')].find(e => visible(e) && norm(e.placeholder) === label);
  if (!target) { const w = root.querySelector('.o_field_widget[name="' + label + '"]'); if (w) target = w.querySelector('input, textarea, select') || w; }
  if (!target) {
    const labels = [...root.querySelectorAll('label.o_form_label')].filter(visible).map(l => norm(l.textContent)).filter(Boolean);
    const hints = [...root.querySelectorAll('input[placeholder], textarea[placeholder]')].filter(visible).map(e => norm(e.placeholder)).filter(Boolean);
    return {error: 'no field "' + label + '" on this form. Fields: ' + [...new Set([...labels, ...hints])].join(', ')};
  }
  const widget = target.closest('.o_field_widget') || target, cls = widget.className;
  const editable = ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
  let kind = 'text';
  if (target.tagName === 'SELECT') kind = 'selection';
  else if (target.type === 'checkbox') kind = 'boolean';
  else if (/many2one/.test(cls) && editable) kind = 'many2one';  // other fields with proposals (e.g. a contact's name) are plain text
  else if (/o_field_date/.test(cls)) kind = 'date';
  else if (!editable || target.readOnly || target.disabled) kind = 'readonly';
  if (!target.id) target.id = 'jsa_field_' + Date.now();
  let value = norm(widget.textContent);
  if (kind === 'readonly' && /many2one/.test(cls)) value = norm((widget.innerText || '').split('\n')[0]);  // the name, not the address under it
  if (target.tagName === 'SELECT') value = norm((target.selectedOptions[0] || {}).textContent);
  else if (target.type === 'checkbox') value = target.checked ? 'yes' : 'no';
  else if (editable) value = norm(target.value);
  return {id: target.id, kind, value};
}"""

# technical name of the column of the form's lines from its title
FIND_COLUMN = r"""(label) => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const lists = [...document.querySelectorAll('.o_form_view .o_field_x2many_list')].filter(e => e.offsetParent);
  if (!lists.length) return {error: 'this form has no lines'};
  const titles = [...lists[0].querySelectorAll('thead th[data-name]')];
  const th = titles.find(t => norm(t.textContent) === label);
  if (!th) return {error: 'no column "' + label + '" in the lines. Columns: ' + titles.map(t => norm(t.textContent)).filter(Boolean).join(', ')};
  return {name: th.dataset.name};
}"""
