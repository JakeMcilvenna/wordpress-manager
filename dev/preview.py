"""Development-only Flask runner; never use for the managed Hetzner deployment."""

from wordpress_manager import create_app


def main():
    # Auto-reload templates as well as Python code, without enabling a debugger.
    app = create_app({"TEMPLATES_AUTO_RELOAD": True})
    app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=True)


if __name__ == "__main__":
    main()
