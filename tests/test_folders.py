from __future__ import annotations

from dataclasses import dataclass

from app.folders import extract_filter_title, filter_peer_candidates


@dataclass
class FakeTitle:
    text: str


@dataclass
class FakePeer:
    id: int


class FakeFilter:
    def __init__(self):
        self.include_peers = [FakePeer(1), FakePeer(2)]
        self.pinned_peers = [FakePeer(2), FakePeer(3)]
        self.exclude_peers = [FakePeer(2)]


def test_extract_filter_title_supports_strings_and_text_objects():
    assert extract_filter_title(type("Filter", (), {"title": "MAX"})()) == "MAX"
    assert extract_filter_title(type("Filter", (), {"title": FakeTitle("MAX")})()) == "MAX"


def test_peer_filtering_deduplicates_pinned_and_excludes():
    result = filter_peer_candidates(FakeFilter())

    assert [peer.id for peer in result] == [1, 3]
