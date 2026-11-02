"""The phone layout (static/style.css, below 720px wide): a bottom tab bar whose More button opens the other pages."""
import unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]


class MobileLayoutTests(unittest.TestCase):
    def test_the_tab_bar(self):
        page,app,css=((ROOT/'static'/name).read_text() for name in ('index.html','app.js','style.css'))
        self.assertIn('viewport-fit=cover',page)
        for tab in ('team','devices','server','settings'):self.assertIn(f'<button data-tab="{tab}" class="nav-extra">',page)
        for tab in ('overview','queue','slicer','activity'):self.assertNotIn(f'data-tab="{tab}" class="nav-extra"',page)
        self.assertIn('id="navMore"',page)
        self.assertIn("$('navMore').onclick=",app);self.assertIn('document.body.dataset.tab=name',app)
        mobile=css[css.index('/* ---- Phones:'):]
        self.assertIn('nav{position:fixed;left:0;right:0;bottom:0',mobile)
        self.assertIn('nav.open .nav-extra{display:flex}',mobile)
        self.assertIn('input,select,textarea{font-size:16px}',mobile)
        # A backdrop-filter on the top bar would make it the fixed tab bar's container (pinning the bar to the top).
        aside=mobile[mobile.index(' aside{'):mobile.index('}',mobile.index(' aside{'))]
        self.assertNotIn('backdrop-filter',aside)


if __name__=='__main__':
    unittest.main()
