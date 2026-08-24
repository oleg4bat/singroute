"""Application entry point."""

from singbox_outbound_updater.gui.app import run_gui


def main() -> None:
    raise SystemExit(run_gui())


if __name__ == "__main__":
    main()
