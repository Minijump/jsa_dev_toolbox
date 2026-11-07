"""The basic Odoo keywords: what a user does in Odoo, written with the words shown on the screen.

A test is a list of keywords, one per line, with their values separated by at least two spaces:

    Confirm a quotation
        Login As    admin
        Open Menu    Sales / Orders / Quotations
        Create Record
        Set Field    Customer    Gemini Furniture
        Add Line    Product=Office Chair    Quantity=2
        Click Button    Confirm
        Status Should Be    Sales Order

These are the public keywords. Your own keywords go in uitests/ (see the examples there): they are built
with the same toolkit (odoo_toolkit.py) and can use these ones.
"""
from robot.api.deco import keyword, library

from odoo_toolkit import odoo

YES = {"yes", "true", "1", "on", "checked"}


@library(scope="GLOBAL", auto_keywords=False)
class OdooKeywords:

    @keyword
    def login_as(self, user):
        """Open Odoo in a new browser and log in as ``user``.

        Example: ``Login As    admin``
        """
        url = odoo.variable("ODOO_URL", "http://localhost:8069").rstrip("/")
        database = odoo.variable("ODOO_DB", "")
        odoo.run("Close Browser", "ALL")
        odoo.run("New Browser", "chromium", f"headless={odoo.variable('HEADLESS', True)}",
                 f"slowMo={odoo.variable('SLOWMO', '0s')}")
        odoo.run("New Context", "viewport={'width': 1400, 'height': 900}")
        odoo.run("New Page", f"{url}/web/login" + (f"?db={database}" if database else ""))
        odoo.run("Fill Text", odoo.selector("login"), user)
        odoo.run("Fill Secret", odoo.selector("password"), "$ODOO_PASSWORD")
        odoo.run("Click", odoo.selector("login_submit"))
        odoo.run("Wait For Elements State", odoo.selector("logged_in"), "visible", "timeout=30s")
        odoo.ready()

    @keyword
    def open_menu(self, path):
        """Go to a menu, written as in Odoo: the app, then the menus, separated by ``/``.

        Examples: ``Open Menu    Contacts`` or ``Open Menu    Sales / Orders / Quotations``
        """
        app, *menus = [part.strip() for part in path.split("/")]
        # enterprise: the apps may already be on the screen (the home menu, shown after the login)
        if not odoo.count(odoo.selector("home_menu")):
            odoo.run("Click", odoo.selector("apps_toggle"))
        odoo.run("Click", odoo.selector("app", app))
        odoo.ready()
        for position, menu in enumerate(menus):
            odoo.run("Click", odoo.selector("menu_section" if position == 0 else "menu_item", menu))
            odoo.ready()
        odoo.run("Wait For Elements State", odoo.selector("view") + " >> nth=0", "visible", "timeout=30s")

    @keyword
    def create_record(self):
        """Click the ``New`` button of the list (or of the form) to create a record."""
        odoo.run("Click", odoo.selector("new_record"))
        odoo.run("Wait For Elements State", odoo.selector("form"), "visible", "timeout=30s")
        odoo.ready()

    @keyword
    def open_record(self, text):
        """Open the first record of the list (or kanban) showing ``text``.

        Example: ``Open Record    Gemini Furniture``
        """
        odoo.run("Click", odoo.selector("record", text))
        odoo.run("Wait For Elements State", odoo.selector("form"), "visible", "timeout=30s")
        odoo.ready()

    @keyword
    def search_for(self, text):
        """Type ``text`` in the search bar of the list and press Enter.

        Example: ``Search For    Gemini``
        """
        odoo.run("Fill Text", odoo.selector("search"), text)
        odoo.run("Keyboard Key", "press", "Enter")
        odoo.ready()

    @keyword
    def set_field(self, label, value):
        """Fill a field of the form. ``label`` is the name written next to the field (or the grey text inside it).

        Works for text and numbers, lists of choices, records of another list (customer, product...) and checkboxes
        (``yes`` / ``no``).

        Examples: ``Set Field    Customer    Gemini Furniture`` or ``Set Field    Phone    0123``
        """
        field = odoo.field(label)
        selector, kind = f"id={field['id']}", field["kind"]
        if kind == "many2one":
            odoo.choose(selector, value)
        elif kind == "selection":
            odoo.run("Select Options By", selector, "label", value)
        elif kind == "boolean":
            odoo.run("Check Checkbox" if str(value).strip().lower() in YES else "Uncheck Checkbox", selector)
        elif kind == "text":
            odoo.run("Fill Text", selector, value)
            odoo.run("Keyboard Key", "press", "Tab")
        else:
            odoo.fail(f'The field "{label}" cannot be filled by a test yet ({kind} field)')
        odoo.ready()

    @keyword
    def add_line(self, **values):
        """Add a line in the form (order lines, invoice lines...) and fill its columns, named as their titles.

        Example: ``Add Line    Product=Office Chair    Quantity=2``
        """
        odoo.run("Click", odoo.selector("add_line"))
        for column, value in values.items():
            name = odoo.evaluate(odoo.screens.FIND_COLUMN, column)["name"]
            cell = odoo.selector("line_cell", name)
            odoo.run("Click", cell)
            entry = f"{cell} >> :is(input, textarea) >> nth=0"
            if "o-autocomplete--input" in (odoo.run("Get Attribute", entry, "class") or ""):
                odoo.choose(entry, value)
            else:
                odoo.run("Fill Text", entry, value)
                odoo.ready()

    @keyword
    def click_button(self, label):
        """Click a button of the page (or of the window opened on top of it), named as written on it.

        Example: ``Click Button    Confirm``
        """
        dialog = odoo.selector("dialog")
        scope = f"{dialog} >> " if odoo.count(dialog) else ""
        odoo.run("Click", scope + odoo.selector("button", label))
        odoo.ready()

    @keyword
    def save_record(self):
        """Save the form."""
        if odoo.count(odoo.selector("form_save")):
            odoo.run("Click", odoo.selector("form_save"))
        odoo.ready()

    @keyword
    def field_should_be(self, label, expected):
        """Check the value of a field of the form.

        Example: ``Field Should Be    Customer    Gemini Furniture``
        """
        odoo.expect(f'The field "{label}"', expected, lambda: odoo.field(label)["value"])

    @keyword
    def status_should_be(self, expected):
        """Check the status shown at the top right of the form.

        Example: ``Status Should Be    Sales Order``
        """
        odoo.expect("The status", expected, lambda: odoo.run("Get Text", odoo.selector("status")))

    @keyword
    def should_see(self, text):
        """Check that ``text`` is written somewhere on the page.

        Example: ``Should See    Gemini Furniture``
        """
        odoo.run("Wait For Elements State", f'text="{text}" >> nth=0', "visible", "timeout=10s")
