import logging

from trader.server import QuietPathsFilter


def _record(msg):
    return logging.LogRecord(
        "werkzeug", logging.INFO, "path", 1, msg, None, None
    )


def test_quiet_filter_drops_monitoring_lines():
    f = QuietPathsFilter()
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] '
        '"POST /api/reader_status HTTP/1.1" 200 -'
    ))
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "GET /api/summary HTTP/1.1" 200 -'
    ))
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "GET /api/settings HTTP/1.1" 200 -'
    ))
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "GET /health HTTP/1.1" 200 -'
    ))
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "GET / HTTP/1.1" 200 -'
    ))
    assert not f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] '
        '"GET /.well-known/appspecific/com.chrome.devtools.json HTTP/1.1" 404 -'
    ))


def test_quiet_filter_keeps_meaningful_lines():
    f = QuietPathsFilter()
    assert f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "POST /alert HTTP/1.1" 200 -'
    ))
    assert f.filter(_record(
        '127.0.0.1 - - [18/Sep/2026 07:15:10] "POST /api/settings HTTP/1.1" 200 -'
    ))
