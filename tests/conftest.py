from pathlib import Path
import contextlib
import os
import io
import pytest  # pylint: disable=unused-import

from loki import SourceFile, fgen, OFP, compile_and_load, FindNodes, CallStatement
from loki.tools import gettempdir, filehash


__all__ = ['generate_identity', 'jit_compile', 'clean_test', 'stdchannel_redirected',
           'stdchannel_is_captured']


def generate_identity(refpath, routinename, modulename=None, frontend=OFP):
    """
    Generate the "identity" of a single subroutine with a frontend-specific suffix.
    """
    testname = refpath.parent/('%s_%s_%s.f90' % (refpath.stem, routinename, frontend))
    source = SourceFile.from_file(refpath, frontend=frontend)

    if modulename:
        module = [m for m in source.modules if m.name == modulename][0]
        module.name += '_%s_%s' % (routinename, frontend)
        for routine in source.all_subroutines:
            routine.name += '_%s' % frontend
            for call in FindNodes(CallStatement).visit(routine.body):
                call.name += '_%s' % frontend
        source.write(source=fgen(module), filename=testname)
    else:
        routine = [r for r in source.subroutines if r.name == routinename][0]
        routine.name += '_%s' % frontend
        source.write(source=fgen(routine), filename=testname)

    pymod = compile_and_load(testname, cwd=str(refpath.parent), use_f90wrap=True)

    if modulename:
        # modname = '_'.join(s.capitalize() for s in refpath.stem.split('_'))
        return getattr(pymod, testname.stem)
    return pymod


def jit_compile(source, filepath=None, objname=None):
    """
    Generate, Just-in-Time compile and load a given item (`Module` or
    `Subroutine`) for interactive execution.
    """
    if isinstance(source, SourceFile):
        filepath = source.filepath if filepath is None else Path(filepath)
        source.write(filename=filepath)
    else:
        source = fgen(source)
        if filepath is None:
            filepath = gettempdir()/filehash(source, prefix='', suffix='.f90')
        else:
            filepath = Path(filepath)
        SourceFile(filepath).write(source)

    pymod = compile_and_load(filepath, cwd=str(filepath.parent), use_f90wrap=True)

    if objname:
        return getattr(pymod, objname)
    return pymod


def clean_test(filepath):
    """
    Clean test directory based on JIT'ed source file.
    """
    filepath.with_suffix('.f90').unlink()
    filepath.with_suffix('.o').unlink()
    filepath.with_suffix('.py').unlink()
    f90wrap_toplevel = filepath.parent/'f90wrap_toplevel.f90'
    if f90wrap_toplevel.exists():
        f90wrap_toplevel.unlink()
    for sofile in filepath.parent.glob('_%s.*.so' % filepath.stem):
        sofile.unlink()


@contextlib.contextmanager
def stdchannel_redirected(stdchannel, dest_filename):
    """
    A context manager to temporarily redirect stdout or stderr

    e.g.:

    ```
    with stdchannel_redirected(sys.stderr, os.devnull):
        if compiler.has_function('clock_gettime', libraries=['rt']):
            libraries.append('rt')
    ```

    Source: https://stackoverflow.com/a/17753573

    Note, that this only works when pytest is invoked with '--show-capture' (or '-s').
    This can be checked using `stdchannel_is_captured(capsys)`.
    Additionally, capturing of sys.stdout/sys.stderr needs to be disabled explicitly,
    i.e., use the fixture `capsys` and wrap the above:

    ```
    with capsys.disabled():
        with stdchannel_redirected(sys.stdout, 'stdout.log'):
            function()
    ```
    """

    def try_dup(fd):
        try:
            oldfd = os.dup(fd.fileno())
        except io.UnsupportedOperation:
            oldfd = None
        return oldfd

    def try_dup2(fd, fd2, fd_fileno=True):
        try:
            if fd_fileno:
                os.dup2(fd.fileno(), fd2.fileno())
            else:
                os.dup2(fd, fd2.fileno())
        except io.UnsupportedOperation:
            pass

    oldstdchannel, dest_file = None, None
    try:
        oldstdchannel = try_dup(stdchannel)
        dest_file = open(dest_filename, 'w')
        try_dup2(dest_file, stdchannel)

        yield
    finally:
        if oldstdchannel is not None:
            try_dup2(oldstdchannel, stdchannel, fd_fileno=False)
        if dest_file is not None:
            dest_file.close()


def stdchannel_is_captured(capsys):
    """
    Utility function to verify if pytest captures stdout/stderr.

    This hinders redirecting stdout/stderr for f2py/f90wrap functions.

    :param capsys: the capsys fixture of the test.
    :returns: True if pytest captures output, otherwise False.
    """

    capturemanager = capsys.request.config.pluginmanager.getplugin("capturemanager")
    return capturemanager._global_capturing.out is not None
