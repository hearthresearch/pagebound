import pytest

from pagebound.errors import NoTextLayerError
from pagebound.probe import PROBE_FLOOR_CHARS, PROBE_PAGES, text_layer


class FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_textpage(self):
        return self

    def get_text_range(self) -> str:
        return self._text


class FakeDocument:
    def __init__(self, texts: list[str]):
        self._pages = [FakePage(t) for t in texts]

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> FakePage:
        return self._pages[index]


def opener(texts: list[str]):
    return lambda _path: FakeDocument(texts)


def test_a_text_layer_is_detected():
    result = text_layer("x.pdf", open_document=opener(["word " * 200] * 5))
    assert result["has_text"] is True
    assert result["pages"] == 5


def test_only_the_first_pages_are_probed():
    texts = ["word " * 200] * 10
    result = text_layer("x.pdf", open_document=opener(texts))
    assert result["probe_pages"] == PROBE_PAGES
    assert result["probe_chars"] == len("word " * 200) * PROBE_PAGES


def test_a_scan_has_no_text_layer():
    result = text_layer("x.pdf", open_document=opener(["", "", ""]))
    assert result["has_text"] is False
    assert result["probe_chars"] == 0


def test_a_document_thinner_than_the_floor_has_no_text_layer():
    thin = "a" * (PROBE_FLOOR_CHARS // (PROBE_PAGES + 1))
    result = text_layer("x.pdf", open_document=opener([thin] * PROBE_PAGES))
    assert result["has_text"] is False


def test_a_document_with_fewer_pages_than_the_probe_window_still_works():
    result = text_layer("x.pdf", open_document=opener(["word " * 200]))
    assert result["pages"] == 1
    assert result["probe_pages"] == 1
    assert result["has_text"] is True


def test_an_unreadable_pdf_raises():
    def explode(_path):
        raise ValueError("not a pdf")

    with pytest.raises(NoTextLayerError):
        text_layer("x.pdf", open_document=explode)
