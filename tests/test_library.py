"""The Library window's model: sorting, filtering and incremental reloads, on a real (offscreen) Qt."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from songsnag import config, paths  # noqa: E402
from songsnag.catalog import DONE, PRIORITY_LIVE, QUEUED, Catalog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    try:
        return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    except Exception as e:  # no usable Qt platform on this machine
        pytest.skip(f"Qt unavailable: {e}")


@pytest.fixture
def lib(app):
    from songsnag.ui.library import LibraryWindow

    cat = Catalog(paths.data_dir() / "catalog.db")
    for i in range(30):
        vid = f"song{i:07d}"
        cat.add_sighting(vid, source="brave", played_at=1000 + i)
        cat.next_job()
        cat.finish_done(vid, {"artist": f"Artist {i % 3}", "title": f"Title {i:02d}", "album": "LP", "track": i})
    cat.add_sighting("queued00001", source="album", played=False)
    cat.add_sighting("queued00002", source="brave", priority=PRIORITY_LIVE)
    w = LibraryWindow(cat, config.load(), lambda: None, lambda f: None, lambda i: None)
    w.show()
    yield w, cat
    w.hide()
    cat.close()


def col(w, name):
    from songsnag.ui.library import COLUMNS

    return [w.model.data(w.model.index(r, COLUMNS.index(name))) for r in range(w.model.rowCount())]


def test_queue_first_in_download_order_then_newest(lib):
    w, _ = lib
    status = col(w, "Status")
    assert status[:2] == ["Waiting #1", "Waiting #2"]  # the live play first
    assert col(w, "Title")[2] == "Title 29"


def test_every_column_sorts_both_ways_and_keeps_the_selection(lib):
    from songsnag.ui.library import COLUMNS

    w, _ = lib
    w.table.selectRow(5)
    picked = w._selected()
    for c in range(len(COLUMNS)):
        for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
            w.table.horizontalHeader().setSortIndicator(c, order)
            assert w._selected() == picked
    w.table.horizontalHeader().setSortIndicator(COLUMNS.index("#"), Qt.SortOrder.AscendingOrder)
    assert [t for t in col(w, "#") if t][:3] == ["1", "2", "3"]  # the album in track order (no # first)


def test_incremental_reload_sees_changes_and_filters(lib):
    w, cat = lib
    time.sleep(0.01)
    cat.next_job()  # queued00002 starts downloading
    w.refresh(full=False)
    assert col(w, "Status")[0] == "Downloading…" and col(w, "Status")[1] == "Waiting #1"
    cat.finish_done("queued00002", {"artist": "New", "title": "Fresh"})
    cat.add_sighting("brandnew001", source="brave")
    w.refresh(full=False)
    assert "Fresh" in col(w, "Title") and "brandnew001" in col(w, "Title")
    assert w.model.rowCount() == 33

    w.status.setCurrentIndex(w.status.findData(QUEUED))  # "Waiting"
    assert sorted(col(w, "Title")) == ["brandnew001", "queued00001"]
    cat.finish_done("brandnew001", {"artist": "X", "title": "Done now"})
    w.refresh(full=False)  # no longer waiting: drops out of this view
    assert col(w, "Title") == ["queued00001"]
    w.status.setCurrentIndex(0)  # back to Everything
    assert w.model.rowCount() == 33 and cat.counts()[DONE] == 32


def test_closing_the_window_frees_the_rows(lib):
    w, _ = lib
    w.hide()
    assert w.model.rowCount() == 0
    w.show()
    assert w.model.rowCount() == 32
