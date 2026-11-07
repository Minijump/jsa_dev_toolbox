"""Makes the keywords available in every test file, so that a test file only contains tests
(no technical "Settings" section to write): the basic Odoo keywords, then your own keyword files."""
import os

from robot.api import SuiteVisitor

# your keyword files, given by 'jsa uitests run' (uitests/ and the folders of [uitests] keywords_dirs)
KEYWORD_FILES = "JSA_UITESTS_KEYWORDS"


class AddOdooKeywords(SuiteVisitor):

    def start_suite(self, suite):
        imports = suite.resource.imports
        # a failed step puts a picture of the screen in the report itself (one file to share)
        imports.library("Browser", args=["run_on_failure=Take Screenshot \\ EMBED"])
        imports.library("OdooKeywords")
        for path in filter(None, os.environ.get(KEYWORD_FILES, "").split(os.pathsep)):
            # a .py file is a library of keywords written in Python, a .resource file holds keywords made of keywords
            if path.endswith(".py"):
                imports.library(path)
            else:
                imports.resource(path)
