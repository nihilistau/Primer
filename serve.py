"""Run the Illustrated Primer: python serve.py [--tier NAME] [--port 8810]."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from primer import children
from primer.config import TIERS_DIR, ConfigError, Tier, load_tier, read_settings
from primer.engines import make_engine
from primer.graph import Graph
from primer.hardware import Hardware, detect_hardware, suggest_tier
from primer.images.models import ModelStore
from primer.images.service import ImageService
from primer.pages import PageService
from primer.server import App, make_gate, make_server
from primer.servers import ServerManager, set_launch_check
from primer.setup.approvals import launch_check
from primer.setup.catalogues import Catalogues
from primer.setup.installer import Installer

ROOT = Path(__file__).resolve().parent


def _on_sigterm(signum, frame) -> None:
    raise KeyboardInterrupt


def choose_tier(cli_name: str | None, var_dir: Path, tiers_dir: Path, hardware) -> tuple[Tier, str]:
    """The tier to start with and a note for the banner. A bad --tier raises ConfigError; a bad
    or tampered saved setting falls back to the tier suggested for this hardware, with a warning."""
    if cli_name:
        return load_tier(cli_name, tiers_dir), ""
    try:
        saved = read_settings(var_dir).get("tier")
    except ConfigError as e:
        print(f"warning: {e}; ignoring saved settings", file=sys.stderr)
        saved = None
    if saved:
        try:
            return load_tier(saved, tiers_dir), ""
        except ConfigError as e:
            print(f"warning: saved setting: {e}; using the suggested tier", file=sys.stderr)
    return load_tier(suggest_tier(hardware), tiers_dir), " (suggested for this hardware)"


def build_app(tier: Tier, var_dir: Path, ui_dir: Path, tiers_dir: Path,
              hardware: Hardware | None = None) -> App:
    """Everything Primer serves, wired up but with no model server started yet. Model servers
    a crashed previous run left behind are stopped and image jobs it left unfinished are
    marked failed first."""
    for line in children.reap_leftovers(var_dir):
        print(line, file=sys.stderr, flush=True)
    children.use_ledger(var_dir)
    graph = Graph(var_dir / "primer.sqlite")
    graph.fail_unfinished_media("Primer restarted")
    graph.fail_unfinished_maps("Primer restarted")
    graph.fail_unfinished_timelines("Primer restarted")
    graph.fail_unfinished_stories("Primer restarted")
    graph.fail_unfinished_diagrams("Primer restarted")
    graph.fail_unfinished_books("Primer restarted")
    graph.clear_unfinished_drafts()
    store = ModelStore(var_dir)
    # Setup: a run that was going when Primer stopped comes back paused (Resume continues it)
    installer = Installer(var_dir, Catalogues(token_file=var_dir / "keys" / "huggingface.key"),
                          tiers_dir=tiers_dir, store=store)
    servers = ServerManager(tier)
    text_pause, gate = make_gate(tier, servers)
    fast = make_engine(tier.fast)
    pages = PageService(graph, make_engine(tier.text), fast, tier.source_mode, gate,
                        App.make_grounder(tier))
    images = ImageService(graph, store, tier.images, fast, gate, var_dir / "media")
    # App also makes the reader model (primer.reader.profile.Profile) over its signals and wires
    # it into the pages: its block goes into page prompts and page opens nudge its summary.
    # It also makes Test me and Review (primer.reader.quiz.Quiz): quiz results feed the profile
    # (quiz_facts), each answer nudges it, and the reader reset clears the quiz. And the Quest
    # (primer.reader.quest.Quests), whose doors the quiz writes and marks; the reset clears it too.
    # And the Tale (primer.tales.service.TaleService): chapters are stories of kind "chapter",
    # so the unfinished-stories sweep above covers a chapter cut off by a restart; a chapter
    # whose bible update was cut off shows that it failed (its retry is in the Tale window).
    # The reader reset keeps tales.
    app = App(tier=tier, servers=servers, pages=pages, graph=graph,
              hardware=hardware or detect_hardware(), var_dir=var_dir, ui_dir=ui_dir,
              tiers_dir=tiers_dir, store=store, gate=gate, images=images, text_pause=text_pause,
              maps=App.make_maps(graph, tier, fast), installer=installer)
    installer.approvals.on_approved = lambda: _retry_after_approval(app)
    return app


def _retry_after_approval(app: App) -> threading.Thread:
    """An approval in Setup starts the servers that were waiting for it. On its own thread
    (approve() may be called by a request already holding app._lock) and under app._lock, so a
    concurrent tier switch can't leave it starting a server of a discarded tier."""

    def run() -> None:
        with app._lock:
            app.servers.retry_unavailable()

    t = threading.Thread(target=run, name="primer-setup-retry", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The Illustrated Primer")
    ap.add_argument("--tier", help="tier name (default: saved setting, else suggested for this hardware)")
    ap.add_argument("--port", type=int, default=8810)
    ap.add_argument("--tiers-dir", type=Path, default=TIERS_DIR)
    args = ap.parse_args(argv)

    var_dir = ROOT / "var"
    hardware = detect_hardware()
    try:
        tier, note = choose_tier(args.tier, var_dir, args.tiers_dir, hardware)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    app = build_app(tier, var_dir, ROOT / "ui" / "dist", args.tiers_dir, hardware)
    # an executable Setup downloaded runs only once approved by its SHA-256 (any managed server)
    set_launch_check(launch_check(var_dir, app.installer.approvals))
    try:
        server = make_server(app, "127.0.0.1", args.port)
    except OSError as e:
        app.profile.shutdown(timeout=0)
        app.installer.shutdown()
        app.shutdown_images()
        app.shutdown_maps()
        app.shutdown_timelines()
        app.shutdown_stories()
        app.shutdown_diagrams()
        app.shutdown_books()
        app.shutdown_remote()
        app.pages.shutdown(drain=False)
        print(f"error: cannot listen on port {args.port}: {e}", file=sys.stderr)
        return 2
    app.servers.start_all(wait=False)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_sigterm)
    print(f"Illustrated Primer: http://127.0.0.1:{args.port}  tier: {tier.name}{note}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        # image jobs and sd-server, then the model servers, so a second Ctrl+C while prefetch
        # winds down can't skip them
        try:
            try:
                app.profile.shutdown(timeout=0)  # no new reader summaries; one in flight is a daemon
                app.installer.shutdown()  # keeps the current download's .part
                if app.setup_api is not None:
                    app.setup_api.shutdown()  # a model move in progress stops; its .part is removed
                app.shutdown_images()
            finally:
                try:
                    app.shutdown_maps()
                finally:
                    try:
                        app.shutdown_timelines()
                    finally:
                        try:
                            app.shutdown_stories()
                        finally:
                            try:
                                app.shutdown_diagrams()
                            finally:
                                try:
                                    app.shutdown_books()
                                finally:
                                    try:
                                        app.shutdown_remote()  # Kairos's tool streams end
                                    finally:
                                        app.servers.stop_all()
        finally:
            server.server_close()
            app.pages.shutdown(drain=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
