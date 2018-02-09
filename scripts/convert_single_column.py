import click as cli
import re
from collections import OrderedDict

from ecir import FortranSourceFile

@cli.command()
@cli.option('--file', '-f', help='Source file to convert.')
@cli.option('--output', '-o', help='Source file to convert.')
def convert(file, output):

    print('Processing %s ...' % file)
    source = FortranSourceFile(file)

    tdim = 'KLON'  # Name of the target dimension
    tvar = 'JL'  # Name of the target iteration variable

    # Get list of all declarations involving the target dimension
    decls = [l for l in source.declarations.longlines if tdim in l]
    decls = [l for l in decls if not l.startswith('!')]  # Strip comments

    # Get list of all allocations involving the target dimension
    re_allocs = re.compile('ALLOCATE\(.*?\)\)')
    allocs = [l for l in re_allocs.findall(source.body._source) if tdim in l]

    # Extract all variables that use the target dimensions from decls and allocs
    re_vnames = re.compile('::\W*(?P<name>[a-zA-Z0-9\_]*)')
    vnames = [re_vnames.search(l).groupdict()['name'] for l in decls]
    re_valloc = re.compile('ALLOCATE\((?P<var>[a-zA-Z0-9\_]*)\(')
    vnames += [re_valloc.search(l).groupdict()['var'] for l in allocs]

    # Note: We assume that KLON is always the leading dimension(!)
    # Strip target dimension from declarations and body (for ALLOCATEs)
    source.declarations.replace({'(%s,' % tdim: '(', '(%s)' % tdim: ''})
    source.body.replace({'(%s,' % tdim: '(', '(%s)' % tdim: ''})

    # Strip all target iteration indices
    source.body.replace({'(%s,' % tvar: '(', '(%s)' % tvar: ''})

    for var in vnames:
        # Strip all colon indices for leading dimensions
        source.body.replace({'%s(:,' % var: '%s(' % var,
                             '%s(:)' % var: '%s' % var})

    # Super-hacky regex replacement for the target loops,
    # assuming that we only ever replace the inner (fast) loop!
    re_target_loop = re.compile('[^\n]*DO %s.*?ENDDO' % tvar, re.DOTALL)
    re_loop_body = re.compile('DO %s.*?\n(.*?)\n\W*ENDDO' % tvar, re.DOTALL)
    for loop in re_target_loop.findall(source.body._source):
        # Get loop body and drop two leading chars for unindentation
        body = re_loop_body.findall(loop)[0]
        body = '\n'.join([line.replace('  ', '', 1) for line in body.split('\n')])
        # Manually perform the replacement, as we're going accross lines
        source.body._source = source.body._source.replace(loop, body)

    # Strip a dimension from all affected ALLOCATABLEs
    allocatables = [ll for ll in source.declarations.longlines
                    if 'ALLOCATABLE' in ll]
    for allocatable in allocatables:
        for vname in vnames:
            if vname in allocatable:
                source.declarations.replace({'%s(:,' % vname: '%s(' % vname})

    print("Writing to %s" % output)
    source.write(output)


if __name__ == "__main__":
    convert()
