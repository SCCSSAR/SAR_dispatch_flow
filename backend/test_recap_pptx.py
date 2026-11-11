"""Tests for backend/recap_pptx.py — writes the month's .pptx.

python-pptx lives only in the recap venv (research/monthly-recap/.venv), so this file skips cleanly
under the build's system python. PUBLIC repo: every value here is invented.
"""

import io

import pytest

pptx = pytest.importorskip("pptx")
from PIL import Image  # noqa: E402

import monthly_recap as mr  # noqa: E402
import recap_pptx as rp  # noqa: E402

FACTS = ("date: {date}\nevent: {event}\nattendees: {att}\nhours: {hours}\nvolunteer_hours: {vh}\n"
         "cancelled: {canc}\nmutual_aid: {ma}\nenvironment: Urban\nfound: {found}\ndistance: 1.2mi NE\n"
         "specialty: K9, UAS\nmap: {map}\npls: 37.30000, -121.90000\n")


def _section(number, date, event, att, hours, vh, canc="no", ma="no", found="Found", map_id="ZZ12AB3",
             lessons=("Ask for transit records early.",), disruption=()):
    out = [f"## {number} · {date} · {event}", "```facts",
           FACTS.format(date=date, event=event, att=att, hours=hours, vh=vh, canc=canc, ma=ma, found=found,
                        map=map_id).rstrip("\n"), "```",
           "### Summary", "A man left on foot and was found well.", "### Highlight", "K9 found the trail.",
           "### Lessons learned"] + [f"- {l}" for l in lessons]
    out += ["### The disruption"] + [f"- {l}" for l in disruption] + ["### Sources and conflicts", "x", ""]
    return "\n".join(out)


def _draft():
    text = "\n".join([
        "# 2026-10 incident recap — drafts", "",
        _section("98701", "10-02", "EXPD Elmwood", 10, "6.2", "10 × 6.2 h = 62"),
        _section("98702", "10-04", "Farshire M/A Type 1", 5, "14.5", "5 × 14.5 h = 72", ma="yes", map_id=""),
        _section("98703", "10-28", "EXPD Birch CANCELLED", 14, "[fill in]", "14 × not closed out", canc="yes",
                 found="[fill in]", lessons=("[draft]",), disruption=("14 had answered YES.",)),
        "## Excluded", ""])
    return mr.parse_drafts(text)


def _png(path):
    Image.new("RGB", (40, 30), (200, 230, 200)).save(path)
    return path


def _texts(slide):
    out = []
    for sh in slide.shapes:
        if sh.has_text_frame:
            out.append(sh.text_frame.text)
        if getattr(sh, "has_table", False) and sh.has_table:
            out += [c.text for r in sh.table.rows for c in r.cells]
    return out


@pytest.fixture
def deck(tmp_path):
    maps = {"98701": _png(tmp_path / "a.png"), "98702": None, "98703": _png(tmp_path / "c.png")}
    links = {"98701": "https://caltopo.com/m/ZZ12AB3", "98703": "https://caltopo.com/m/ZZ12AB4"}
    out = rp.build_deck(_draft(), _png(tmp_path / "o.png"), maps, links, tmp_path / "2026-10 Incident Recap.pptx")
    return pptx.Presentation(str(out))


class TestBuildDeckRefusesOverwrite:
    def test_existing_file_untouched(self, tmp_path):
        out = tmp_path / "2026-10 Incident Recap.pptx"
        out.write_bytes(b"the presenter's edited deck")
        with pytest.raises(FileExistsError):
            rp.build_deck(_draft(), None, {}, {}, out)
        assert out.read_bytes() == b"the presenter's edited deck"


class TestSummarySlide:
    def test_title_and_table(self, deck):
        s = deck.slides[0]
        texts = _texts(s)
        assert "OCTOBER 2026 · INCIDENTS" in texts
        table = next(sh.table for sh in s.shapes if getattr(sh, "has_table", False) and sh.has_table)
        rows = [[c.text for c in r.cells] for r in table.rows]
        assert rows[0] == ["#", "Date", "Event", "Volunteer hours", "Att."]
        assert len(rows) == 1 + 3 + 1
        assert rows[1][0] == "1" and rows[2][0] == "M/A" and rows[3][0] == "2"
        assert rows[3][2] == "EXPD Birch — cancelled"
        assert rows[4][2] == "3 incidents (1 cancelled, 1 M/A)"
        assert rows[4][3] == "134+" and rows[4][4] == "29"

    def test_slide_count(self, deck):
        assert len(deck.slides) == 4

    def test_widescreen(self, deck):
        assert (deck.slide_width, deck.slide_height) == (pptx.util.Inches(13.333), pptx.util.Inches(7.5))


class TestIncidentSlide:
    def test_title_and_sections(self, deck):
        texts = "\n".join(_texts(deck.slides[1]))
        assert "10-02 · EXPD ELMWOOD" in texts
        assert "LESSONS LEARNED" in texts and "TEAM TAKEAWAYS" in texts
        assert "Highlight: K9 found the trail." in texts
        assert "Found  ·  Urban  ·  1.2mi NE  ·  10 × 6.2 h = 62  ·  K9, UAS  ·  M/A: no" in texts
        assert "[draft]" not in texts

    def test_map_links_to_caltopo(self, deck):
        pics = [sh for sh in deck.slides[1].shapes if sh.shape_type == pptx.enum.shapes.MSO_SHAPE_TYPE.PICTURE]
        assert len(pics) == 1
        assert pics[0].click_action.hyperlink.address == "https://caltopo.com/m/ZZ12AB3"
        caption = next(sh for sh in deck.slides[1].shapes if sh.has_text_frame and sh.text_frame.text == "Open the live map")
        assert caption.text_frame.paragraphs[0].runs[0].hyperlink.address == "https://caltopo.com/m/ZZ12AB3"

    def test_mutual_aid_title(self, deck):
        assert any("· M/A" in t for t in _texts(deck.slides[2]))


class TestCancelledSlide:
    def test_disruption_first_no_draft_lessons(self, deck):
        texts = "\n".join(_texts(deck.slides[3]))
        assert "10-28 · EXPD BIRCH — CANCELLED" in texts
        assert "THE DISRUPTION" in texts and "14 had answered YES." in texts
        assert "LESSONS LEARNED" not in texts  # its only lesson was [draft]
        assert texts.index("THE DISRUPTION") < texts.index("TEAM TAKEAWAYS")


class TestNoMapPlaceholder:
    def test_placeholder_box(self, deck):
        s = deck.slides[2]
        assert "Space for a photo" in _texts(s)
        assert not [sh for sh in s.shapes if sh.shape_type == pptx.enum.shapes.MSO_SHAPE_TYPE.PICTURE]


class TestPicturesInsideSlide:
    def test_every_picture_fits(self, deck):
        for slide in deck.slides:
            for sh in slide.shapes:
                assert sh.left + sh.width <= deck.slide_width and sh.top + sh.height <= deck.slide_height, sh.name


class TestFonts:
    def test_only_house_fonts(self, deck):
        names = set()
        for slide in deck.slides:
            for sh in slide.shapes:
                frames = [sh.text_frame] if sh.has_text_frame else []
                if getattr(sh, "has_table", False) and sh.has_table:
                    frames += [c.text_frame for r in sh.table.rows for c in r.cells]
                for tf in frames:
                    names |= {r.font.name for p in tf.paragraphs for r in p.runs}
        assert names <= {"Oswald", "Public Sans"} and names


class TestLongTextFits:
    """Text never shrinks below 75% (the presenter, 2026-11-11: 10.5 pt bullets are the floor);
    anything that still does not fit continues on a '(cont.)' slide."""

    def test_short_text_keeps_full_size(self):
        assert rp.text_scale([("A short summary.", 15, 0)], 453.6, 432) == 1.0

    def test_moderate_text_shrinks_but_not_below_floor(self):
        paras = [("x " * 150, 14, 4)] * 6
        scale = rp.text_scale(paras, 453.6, 432)
        assert rp.MIN_SCALE <= scale < 1.0
        assert rp.estimated_height(paras, 453.6, scale) <= 432

    def test_too_much_text_has_no_scale(self):
        assert rp.text_scale([("x " * 300, 14, 4)] * 6, 453.6, 432) is None


def _specs(n_lessons, words=60):
    inc = mr.IncidentDraft(number="98701", title="EXPD Elmwood", facts={"found": "Found"},
                           summary="A short summary.", highlight="A highlight.",
                           lessons=[f"Lesson {i}: " + "word " * words for i in range(n_lessons)])
    return rp._left_column(inc, False, False)


class TestPaginate:
    def test_fits_on_one_page(self):
        assert len(rp.paginate(_specs(3, 10), 453.6, 432)) == 1

    def test_overflow_continues_with_a_cont_heading(self):
        pages = rp.paginate(_specs(9), 453.6, 432)
        assert len(pages) >= 2
        assert pages[1][0]["text"] == "LESSONS LEARNED (cont.)"
        assert pages[-1][-1]["text"] == "TEAM TAKEAWAYS"
        texts = [p["text"] for page in pages for p in page if p["text"].startswith("•")]
        assert texts == [p["text"] for p in _specs(9) if p["text"].startswith("•")]  # each once, in order
        for page in pages:
            paras = [(p["text"], p["size"], p.get("space", 0)) for p in page]
            assert rp.text_scale(paras, 453.6, 432) is not None

    def test_no_heading_left_alone_at_a_page_bottom(self):
        # Sweep the summary length so that, somewhere, the heading fits and its first lesson does not.
        lesson = "A long lesson " + "about what happened " * 30
        for words in range(20, 900, 10):
            inc = mr.IncidentDraft(number="98701", title="T", facts={}, summary="word " * words,
                                   highlight="", lessons=[lesson])
            for page in rp.paginate(rp._left_column(inc, False, False), 453.6, 432):
                assert not page[-1].get("heading") or page[-1]["text"] == "TEAM TAKEAWAYS"


class TestContinuationSlide:
    def test_long_lessons_continue_and_never_go_below_floor(self, tmp_path):
        long = tuple(f"Lesson {i}: " + "a fairly long sentence about what happened on scene " * 4 for i in range(9))
        text = "\n".join(["# 2026-11 incident recap — drafts", "",
                          _section("98701", "11-07", "EXPD Elmwood", 13, "2.5", "13 × 2.5 h = 32", lessons=long), ""])
        notes = []
        out = rp.build_deck(mr.parse_drafts(text), None, {}, {}, tmp_path / "d.pptx", notes=notes)
        slides = pptx.Presentation(str(out)).slides
        assert len(slides) >= 3
        titles = ["\n".join(_texts(s)) for s in slides]
        assert "11-07 · EXPD ELMWOOD (cont.)" in titles[2]
        assert "TEAM TAKEAWAYS" in titles[-1] and "TEAM TAKEAWAYS" not in titles[1]
        sizes = [r.font.size.pt for s in list(slides)[1:] for sh in s.shapes if sh.has_text_frame
                 for p in sh.text_frame.paragraphs for r in p.runs if p.text.startswith("•")]
        assert min(sizes) >= 14 * rp.MIN_SCALE - 0.01
        assert len(notes) == 1 and notes[0].startswith("98701 continued onto")
