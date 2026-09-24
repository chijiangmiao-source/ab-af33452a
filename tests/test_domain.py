"""Unit tests for the pure split/merge transformation (no database needed)."""

import pytest

from app.domain import Segment, apply_publish, merge_adjacent


def test_publish_into_empty_map():
    assert apply_publish([], 10, 20, {"a": 1}) == [Segment(10, 20, {"a": 1})]


def test_invalid_interval_rejected():
    with pytest.raises(ValueError):
        apply_publish([], 20, 20, {})
    with pytest.raises(ValueError):
        apply_publish([], 30, 20, {})


def test_middle_split_keeps_both_residuals():
    current = [Segment(0, 100, "A")]
    result = apply_publish(current, 30, 50, "B")
    assert result == [
        Segment(0, 30, "A"),
        Segment(30, 50, "B"),
        Segment(50, 100, "A"),
    ]


def test_left_aligned_overlap_keeps_right_residual():
    current = [Segment(0, 100, "A")]
    assert apply_publish(current, 0, 40, "B") == [
        Segment(0, 40, "B"),
        Segment(40, 100, "A"),
    ]


def test_right_aligned_overlap_keeps_left_residual():
    current = [Segment(0, 100, "A")]
    assert apply_publish(current, 60, 100, "B") == [
        Segment(0, 60, "A"),
        Segment(60, 100, "B"),
    ]


def test_full_cover_drops_segment():
    current = [Segment(0, 100, "A")]
    assert apply_publish(current, 0, 100, "B") == [Segment(0, 100, "B")]


def test_spanning_publish_covers_multiple_segments():
    current = [Segment(0, 10, "A"), Segment(10, 20, "B"), Segment(20, 30, "C")]
    result = apply_publish(current, 5, 25, "X")
    assert result == [
        Segment(0, 5, "A"),
        Segment(5, 25, "X"),
        Segment(25, 30, "C"),
    ]


def test_disjoint_segments_untouched():
    current = [Segment(0, 10, "A"), Segment(20, 30, "B")]
    result = apply_publish(current, 10, 20, "X")
    assert result == [
        Segment(0, 10, "A"),
        Segment(10, 20, "X"),
        Segment(20, 30, "B"),
    ]


def test_merge_with_left_and_right_neighbours():
    current = [Segment(0, 10, "A"), Segment(20, 30, "A")]
    result = apply_publish(current, 10, 20, "A")
    assert result == [Segment(0, 30, "A")]


def test_no_merge_when_content_differs():
    current = [Segment(0, 10, "A"), Segment(20, 30, "B")]
    result = apply_publish(current, 10, 20, "C")
    assert result == [
        Segment(0, 10, "A"),
        Segment(10, 20, "C"),
        Segment(20, 30, "B"),
    ]


def test_republish_same_content_merges_back_to_single_segment():
    current = [Segment(0, 40, "A"), Segment(40, 60, "B"), Segment(60, 100, "A")]
    result = apply_publish(current, 40, 60, "A")
    assert result == [Segment(0, 100, "A")]


def test_merge_adjacent_only_merges_touching_equal_segments():
    segs = [Segment(0, 10, "A"), Segment(10, 20, "A"), Segment(30, 40, "A")]
    assert merge_adjacent(segs) == [Segment(0, 20, "A"), Segment(30, 40, "A")]


def test_content_equality_is_structural_not_key_order():
    current = [Segment(0, 10, {"x": 1, "y": [1, 2]})]
    result = apply_publish(current, 10, 20, {"y": [1, 2], "x": 1})
    assert result == [Segment(0, 20, {"x": 1, "y": [1, 2]})]
