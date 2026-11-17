#!/usr/bin/env python3
"""Start the AI dashboard:  python3 run.py [--data-dir DIR] [--host 0.0.0.0] [--port 8787]"""
import argparse
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from aidash import net  # noqa: E402
from aidash.config import Store  # noqa: E402
from aidash.db import History  # noqa: E402
from aidash.fx import FX  # noqa: E402
from aidash.news import News  # noqa: E402
from aidash.oauth import OAuthManager  # noqa: E402
from aidash.poller import Poller  # noqa: E402
from aidash.server import App, serve  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI usage & cost dashboard")
    ap.add_argument("--data-dir", default=os.environ.get("AIDASH_DATA", os.path.join(HERE, "data")))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=int(os.environ["AIDASH_PORT"]) if os.environ.get("AIDASH_PORT") else None)
    args = ap.parse_args(argv)

    store = Store(args.data_dir)
    net.set_overrides(store.get("upstream_overrides") or {})
    net.set_default_user_agent((store.get("user_agents") or {}).get("default"))
    history = History(os.path.join(args.data_dir, "history.sqlite"))
    fx = FX(history)
    oauth_mgr = OAuthManager(store)
    news = News(store, history)
    poller = Poller(store, oauth_mgr, history, fx, news)
    app = App(store, history, fx, oauth_mgr, poller, news)

    host = args.host or store.get("listen_host") or "0.0.0.0"
    port = args.port or int(store.get("listen_port") or 8787)
    httpd = serve(app, host, port)

    def _sigterm(*_):
        raise KeyboardInterrupt()  # systemd stop -> clean shutdown below

    signal.signal(signal.SIGTERM, _sigterm)
    poller.start()
    print("AI dashboard on http://%s:%d  (data: %s)" % (host, port, args.data_dir), flush=True)
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
