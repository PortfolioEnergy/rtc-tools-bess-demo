# Configuration file for the Sphinx documentation builder.

import os
import sys

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document) are in another directory, add these
# directories to sys.path here.
sys.path.insert(0, os.path.abspath('../src'))

# -- Project information -----------------------------------------------------

project = 'Linear RTC-Tools BESS Demo'
copyright = '2025 PortfolioEnergy'
author = 'PortfolioEnergy'
release = '0.1.0'

# -- General configuration ---------------------------------------------------

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon',
    'sphinx.ext.mathjax',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# -- Options for HTML output ------------------------------------------------

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

# Custom CSS for color scheme
html_css_files = [
]

# -- Theme options -----------------------------------------------------------

html_theme_options = {
    'navigation_depth': 4,
    'collapse_navigation': False,
    'sticky_navigation': True,
    'includehidden': True,
    'titles_only': False
}
