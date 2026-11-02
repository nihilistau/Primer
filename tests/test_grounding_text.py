from primer.grounding.text import clip, html_to_text

HTML = """<html><body><nav>Menu</nav><article>
<table class="infobox"><tr><td>Born 1900</td></tr></table>
<h2>Whales</h2>
<p>Whales are mammals.<sup class="reference">[1]</sup></p>
<div class="navbox"><p>Navbox junk</p></div>
<script>var x = 1;</script>
<p>They live in the sea.</p>
<div class="reflist"><ol><li>Ref one</li></ol></div>
</article><footer>Foot</footer></body></html>"""


def test_html_to_text_drops_infobox_and_refs():
    out = html_to_text(HTML)
    assert out == "Whales\n\nWhales are mammals.\n\nThey live in the sea."


def test_html_to_text_without_main_only_keeps_outside():
    assert "Menu" not in html_to_text(HTML, main_only=False)
    assert html_to_text("<body><p>a</p><p>b</p></body>") == "a\n\nb"


def test_clip_cuts_at_sentence_end():
    text = "First sentence here. Second one is a bit longer. Third goes on and on without end"
    assert clip(text, 55) == "First sentence here. Second one is a bit longer."
    assert clip("short", 100) == "short"
    # no sentence end in the last 30%: hard cut
    assert clip("A" * 20 + ". " + "b" * 100, 60) == "A" * 20 + ". " + "b" * 38
