from . import create_app


def main():
    # Intentionally no configurable public bind and no debugger/reloader.
    create_app().run(host="127.0.0.1", port=8080, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
