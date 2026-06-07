"""Unit tests for the MediaConvert completion handler parsing helpers."""

from src.app.jobs.transcode_complete_job import _extract_mp4_path, _s3_uri_to_canonical


def test_s3_uri_to_canonical():
    out = _s3_uri_to_canonical(
        "s3://my-bucket/transcoded/e1/clip-web.mp4", "us-east-1", "my-bucket"
    )
    assert (
        out == "https://my-bucket.s3.us-east-1.amazonaws.com/transcoded/e1/clip-web.mp4"
    )


def test_s3_uri_to_canonical_rejects_non_s3():
    assert _s3_uri_to_canonical("https://x/y.mp4", "us-east-1", "b") is None
    assert _s3_uri_to_canonical("", "us-east-1", "b") is None


def test_extract_mp4_path_picks_mp4_output():
    detail = {
        "outputGroupDetails": [
            {
                "outputDetails": [
                    {"outputFilePaths": ["s3://b/transcoded/e1/clip-web.mp4"]}
                ]
            }
        ]
    }
    assert _extract_mp4_path(detail) == "s3://b/transcoded/e1/clip-web.mp4"


def test_extract_mp4_path_none_when_absent():
    assert _extract_mp4_path({}) is None
    assert _extract_mp4_path({"outputGroupDetails": []}) is None
