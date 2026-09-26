import io

from trader.ops.loghook import TeeStream, WebhookBatcher


def test_batcher_posts_batches(monkeypatch):
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append((url, json))

    monkeypatch.setattr(
        "trader.ops.loghook.requests.post", fake_post, raising=True
    )
    import trader.ops.loghook as lh
    monkeypatch.setattr(lh.requests, "post", fake_post)

    b = WebhookBatcher("http://hook")
    b.add("line one")
    b.add("line two")
    b.flush_now()
    assert len(posts) == 1
    assert posts[0][0] == "http://hook"
    assert posts[0][1]["content"] == "line one\nline two\n"

    posts.clear()
    for _ in range(8):
        b.add("y" * 300)
    b.flush_now()
    assert all(len(p[1]["content"]) <= 1900 for p in posts)
    assert len(posts) >= 2


def test_batcher_no_url_is_noop():
    b = WebhookBatcher("")
    b.add("line")
    b.flush_now()


def test_tee_stream_forwards_and_batches():
    import re as _re

    out = io.StringIO()
    batcher = WebhookBatcher("")
    tee = TeeStream(out, batcher)
    tee.write("hello\n")
    tee.write("")
    tee.write("\n")
    # every console line carries the log timestamp
    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert _re.match(r"\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2} ", line)
    assert lines[0].endswith("hello")
    tee.flush()
