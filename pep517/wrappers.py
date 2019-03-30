from contextlib import contextmanager
import os
from os.path import dirname, abspath, join as pjoin
import shutil
from subprocess import check_call
import sys
from tempfile import mkdtemp

from . import compat

_in_proc_script = pjoin(dirname(abspath(__file__)), '_in_process.py')


@contextmanager
def tempdir():
    td = mkdtemp()
    try:
        yield td
    finally:
        shutil.rmtree(td)


class BackendUnavailable(Exception):
    """Will be raised if the backend cannot be imported in the hook process."""
    def __init__(self, traceback):
        self.traceback = traceback


class BackendInvalid(Exception):
    """Will be raised if the backend is invalid."""
    def __init__(self, backend_name, backend_path, message):
        self.backend_name = backend_name
        self.backend_path = backend_path
        self.message = message


class UnsupportedOperation(Exception):
    """May be raised by build_sdist if the backend indicates that it can't."""
    def __init__(self, traceback):
        self.traceback = traceback


def default_subprocess_runner(cmd, cwd=None, extra_environ=None):
    """The default method of calling the wrapper subprocess."""
    env = os.environ.copy()
    if extra_environ:
        env.update(extra_environ)

    check_call(cmd, cwd=cwd, env=env)


def norm_and_check(source_tree, requested):
    """Normalise and check a backend path.

    Ensure that the requested backend path is specified as a relative path,
    and resolves to a location under the given source tree.

    Return an absolute version of the requested path.
    """
    if os.path.isabs(requested):
        raise ValueError("paths must be relative")

    abs_source = os.path.abspath(source_tree)
    abs_requested = os.path.normpath(os.path.join(abs_source, requested))
    # We have to use commonprefix for Python 2.7 compatibility. So we
    # normalise case to avoid problems because commonprefix is a character
    # based comparison :-(
    norm_source = os.path.normcase(abs_source)
    norm_requested = os.path.normcase(abs_requested)
    if os.path.commonprefix([norm_source, norm_requested]) != norm_source:
        raise ValueError("paths must be inside source tree")

    return abs_requested


class Pep517HookCaller(object):
    """A wrapper around a source directory to be built with a PEP 517 backend.

    source_dir : The path to the source directory, containing pyproject.toml.
    backend : The build backend spec, as per PEP 517, from pyproject.toml.
    backend_path : The backend path, as per PEP 517, from pyproject.toml.
    """
    def __init__(self, source_dir, build_backend, backend_path=None):
        self.source_dir = abspath(source_dir)
        self.build_backend = build_backend
        if backend_path:
            backend_path = [
                norm_and_check(self.source_dir, p) for p in backend_path
            ]
        self.backend_path = backend_path
        self._subprocess_runner = default_subprocess_runner

    # TODO: Is this over-engineered? Maybe frontends only need to
    #       set this when creating the wrapper, not on every call.
    @contextmanager
    def subprocess_runner(self, runner):
        prev = self._subprocess_runner
        self._subprocess_runner = runner
        yield
        self._subprocess_runner = prev

    def get_requires_for_build_wheel(self, config_settings=None):
        """Identify packages required for building a wheel

        Returns a list of dependency specifications, e.g.:
            ["wheel >= 0.25", "setuptools"]

        This does not include requirements specified in pyproject.toml.
        It returns the result of calling the equivalently named hook in a
        subprocess.
        """
        return self._call_hook('get_requires_for_build_wheel', {
            'config_settings': config_settings
        })

    def prepare_metadata_for_build_wheel(
            self, metadata_directory, config_settings=None):
        """Prepare a *.dist-info folder with metadata for this project.

        Returns the name of the newly created folder.

        If the build backend defines a hook with this name, it will be called
        in a subprocess. If not, the backend will be asked to build a wheel,
        and the dist-info extracted from that.
        """
        return self._call_hook('prepare_metadata_for_build_wheel', {
            'metadata_directory': abspath(metadata_directory),
            'config_settings': config_settings,
        })

    def build_wheel(
            self, wheel_directory, config_settings=None,
            metadata_directory=None):
        """Build a wheel from this project.

        Returns the name of the newly created file.

        In general, this will call the 'build_wheel' hook in the backend.
        However, if that was previously called by
        'prepare_metadata_for_build_wheel', and the same metadata_directory is
        used, the previously built wheel will be copied to wheel_directory.
        """
        if metadata_directory is not None:
            metadata_directory = abspath(metadata_directory)
        return self._call_hook('build_wheel', {
            'wheel_directory': abspath(wheel_directory),
            'config_settings': config_settings,
            'metadata_directory': metadata_directory,
        })

    def get_requires_for_build_sdist(self, config_settings=None):
        """Identify packages required for building a wheel

        Returns a list of dependency specifications, e.g.:
            ["setuptools >= 26"]

        This does not include requirements specified in pyproject.toml.
        It returns the result of calling the equivalently named hook in a
        subprocess.
        """
        return self._call_hook('get_requires_for_build_sdist', {
            'config_settings': config_settings
        })

    def build_sdist(self, sdist_directory, config_settings=None):
        """Build an sdist from this project.

        Returns the name of the newly created file.

        This calls the 'build_sdist' backend hook in a subprocess.
        """
        return self._call_hook('build_sdist', {
            'sdist_directory': abspath(sdist_directory),
            'config_settings': config_settings,
        })

    def _call_hook(self, hook_name, kwargs):
        # On Python 2, pytoml returns Unicode values (which is correct) but the
        # environment passed to check_call needs to contain string values. We
        # convert here by encoding using ASCII (the backend can only contain
        # letters, digits and _, . and : characters, and will be used as a
        # Python identifier, so non-ASCII content is wrong on Python 2 in
        # any case).
        # For backend_path, we use sys.getfilesystemencoding.
        if sys.version_info[0] == 2:
            build_backend = self.build_backend.encode('ASCII')
        else:
            build_backend = self.build_backend
        extra_environ = {'PEP517_BUILD_BACKEND': build_backend}

        if self.backend_path:
            backend_path = os.pathsep.join(self.backend_path)
            if sys.version_info[0] == 2:
                backend_path = backend_path.encode(sys.getfilesystemencoding())
            extra_environ['PEP517_BACKEND_PATH'] = backend_path

        with tempdir() as td:
            hook_input = {'kwargs': kwargs}
            compat.write_json(hook_input, pjoin(td, 'input.json'),
                              indent=2)

            # Run the hook in a subprocess
            self._subprocess_runner(
                [sys.executable, _in_proc_script, hook_name, td],
                cwd=self.source_dir,
                extra_environ=extra_environ
            )

            data = compat.read_json(pjoin(td, 'output.json'))
            if data.get('unsupported'):
                raise UnsupportedOperation(data.get('traceback', ''))
            if data.get('no_backend'):
                raise BackendUnavailable(data.get('traceback', ''))
            if data.get('backend_invalid'):
                raise BackendInvalid(
                    backend_name=self.build_backend,
                    backend_path=self.backend_path,
                    message=data.get('backend_error', '')
                )
            return data['return_val']
