/*
 * The program inside "Photo Offloader.app".
 *
 * macOS names a process after the app bundle its program sits in. Run as
 * `venv/bin/python3 photo-offloader-menubar`, Homebrew's Python re-launches itself inside its
 * own Python.app, so the menu bar appeared as "Python" with Python's icon. This program loads
 * the Python library into its own process instead, so the process belongs to this bundle.
 *
 * Contents/Resources/launcher.conf, written by build_app.sh, has three lines:
 *   the Python library to load, the venv's python3, the script to run.
 */
#include <dlfcn.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FIX_HINT "Run install.sh again to rebuild the app."

static int read_line(FILE *f, char *buf, size_t size) {
    if (!fgets(buf, (int)size, f)) {
        return 0;
    }
    buf[strcspn(buf, "\n")] = '\0';
    return buf[0] != '\0';
}

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX], conf[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) {
        fprintf(stderr, "Photo Offloader: can't find where this program is. " FIX_HINT "\n");
        return 70;
    }
    snprintf(conf, sizeof conf, "%s/../Resources/launcher.conf", dirname(real));

    char lib[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    FILE *f = fopen(conf, "r");
    int complete = f && read_line(f, lib, sizeof lib) && read_line(f, python, sizeof python) &&
                   read_line(f, script, sizeof script);
    if (f) {
        fclose(f);
    }
    if (!complete) {
        fprintf(stderr, "Photo Offloader: can't read %s. " FIX_HINT "\n", conf);
        return 78;
    }

    void *libpython = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!libpython) {
        fprintf(stderr, "Photo Offloader: can't load Python from %s (%s). " FIX_HINT "\n", lib,
                dlerror());
        return 69;
    }
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(libpython, "Py_BytesMain");
    if (!py_main) {
        fprintf(stderr, "Photo Offloader: %s has no Py_BytesMain. " FIX_HINT "\n", lib);
        return 69;
    }

    /* argv[0] is the venv's python3: Python looks for pyvenv.cfg beside it, so it runs in the
       venv with the venv's packages. */
    char **args = calloc((size_t)argc + 2, sizeof *args);
    if (!args) {
        return 71;
    }
    args[0] = python;
    args[1] = script;
    for (int i = 1; i < argc; i++) {
        args[i + 1] = argv[i];
    }
    return py_main(argc + 1, args);
}
