"""Pinned Hermes CLI adapter: snapshot module names before plugin eviction."""

import sys


def purge_modules(module_name, modules):
    prefix = module_name + "."
    # dict.copy runs under the GIL. Imports on other threads may mutate the
    # live registry during the following loop, so never iterate that registry.
    for name in modules.copy():
        if name == module_name or name.startswith(prefix):
            modules.pop(name, None)


def install_loader_fix():
    from hermes_cli import plugins, plugins_loader

    def evict(module_name):
        purge_modules(module_name, sys.modules)

    # plugins also imports this function by value for profile reload cleanup.
    plugins_loader._evict_modules = evict
    plugins._evict_modules = evict


def main():
    repository = sys.argv.pop(1)
    sys.path.insert(0, repository)
    install_loader_fix()
    from hermes_cli.main import main as native_main

    return native_main()


if __name__ == "__main__":
    raise SystemExit(main())
