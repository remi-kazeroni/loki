#!/usr/bin/env python

"""
Loki head script for source-to-source transformations concerning ECMWF
physics, including "Single Column" (SCA) and CLAW transformations.
"""

import sys
from pathlib import Path
import click

from loki import (
    SourceFile, Module, Transformation, Transformer, FindNodes, Loop,
    Pragma, Frontend, flatten
)

# Get generalized transformations provided by Loki
from loki.transform import DependencyTransformation, FortranCTransformation

# Bootstrap the local transformations directory for custom transformations
sys.path.insert(0, str(Path(__file__).parent))
# pylint: disable=wrong-import-position,wrong-import-order
from transformations import DerivedTypeArgumentsTransformation
from transformations import Dimension, ExtractSCATransformation, CLAWTransformation


def remove_omp_do(routine):
    """
    Utility routine that strips existing !$opm do pragmas from driver code.
    """
    mapper = {}
    for p in FindNodes(Pragma).visit(routine.body):
        if p.keyword.lower() == 'omp':
            if p.content.startswith('do') or p.content.startswith('end do'):
                mapper[p] = None
    routine.body = Transformer(mapper).visit(routine.body)


@click.group()
def cli():
    pass


@cli.command('idem')
@click.option('--out-path', '-out', type=click.Path(),
              help='Path for generated source files.')
@click.option('--source', '-s', type=click.Path(), multiple=True,
              help='Source file to convert.')
@click.option('--driver', '-d', type=click.Path(),
              help='Driver file to convert.')
@click.option('--header', '-h', type=click.Path(), multiple=True,
              help='Path for additional header file(s).')
@click.option('--xmod', '-M', type=click.Path(), multiple=True,
              help='Path for additional module file(s)')
@click.option('--include', '-I', type=click.Path(), multiple=True,
              help='Path for additional header file(s)')
@click.option('--flatten-args/--no-flatten-args', default=True,
              help='Flag to trigger derived-type argument unrolling')
@click.option('--openmp/--no-openmp', default=False,
              help='Flag to force OpenMP pragmas onto existing horizontal loops')
@click.option('--frontend', default='fp', type=click.Choice(['fp', 'ofp', 'omni']),
              help='Frontend parser to use (default FP)')
def idempotence(out_path, source, driver, header, xmod, include, flatten_args, openmp, frontend):
    """
    Idempotence: A "do-nothing" debug mode that performs a parse-and-unparse cycle.
    """
    driver_name = 'CLOUDSC_DRIVER'
    kernel_name = 'CLOUDSC'

    frontend = Frontend[frontend.upper()]
    frontend_type = Frontend.OFP if frontend == Frontend.OMNI else frontend
    definitions = flatten(SourceFile.from_file(h, xmods=xmod,
                                               frontend=frontend_type).modules for h in header)

    driver = SourceFile.from_file(driver, xmods=xmod, includes=include,
                                  frontend=frontend, builddir=out_path)
    kernels = [SourceFile.from_file(src, definitions=definitions, frontend=frontend,
                                    xmods=xmod, includes=include, builddir=out_path)
               for src in source]

    # Get a separate list of routine objects ad names for transformations
    kernel_routines = flatten(kernel.all_subroutines for kernel in kernels)
    kernel_targets = [routine.name.upper() for routine in kernel_routines]

    # Ensure that the kernel calls have all meta-information
    driver[driver_name].enrich_calls(routines=kernel_routines)
    for kernel in kernels:
        for routine in kernel.all_subroutines:
            routine.enrich_calls(routines=kernel_routines)

    class IdemTransformation(Transformation):
        """
        Define a custom transformation pipeline that optionally inserts
        experimental OpenMP pragmas for horizontal loops.
        """

        def transform_subroutine(self, routine, **kwargs):
            # Define the horizontal dimension
            horizontal = Dimension(name='KLON', aliases=['NPROMA', 'KDIM%KLON'],
                                   variable='JL', iteration=('KIDIA', 'KFDIA'))

            if openmp:
                # Experimental OpenMP loop pragma insertion
                for loop in FindNodes(Loop).visit(routine.body):
                    if loop.variable == horizontal.variable:
                        # Update the loop in-place with new OpenMP pragmas
                        pragma = Pragma(keyword='omp', content='do simd')
                        pragma_nowait = Pragma(keyword='omp',
                                               content='end do simd nowait')
                        loop._update(pragma=pragma, pragma_post=pragma_nowait)

    if flatten_args:
        # Unroll derived-type arguments into multiple arguments
        # Caller must go first, as it needs info from routine
        driver.apply(DerivedTypeArgumentsTransformation(), role='driver')
        for kernel in kernels:
            kernel.apply(DerivedTypeArgumentsTransformation(), role='kernel')

    # Now we instantiate our pipeline and apply the "idempotence" changes
    driver.apply(IdemTransformation())
    for kernel in kernels:
        kernel.apply(IdemTransformation())

    # Housekeeping: Inject our re-named kernel and auto-wrapped it in a module
    dependency = DependencyTransformation(suffix='_IDEM', mode='module', module_suffix='_MOD')
    for kernel in kernels:
        kernel.apply(dependency, role='kernel', targets=kernel_targets)
        kernel.write(path=Path(out_path)/kernel.path.with_suffix('.idem.F90').name)

    # Re-generate the driver that mimicks the original source file,
    # but imports and calls our re-generated kernel.
    driver.apply(dependency, role='driver', targets=kernel_name)
    driver.write(path=Path(out_path)/driver.path.with_suffix('.idem.F90').name)


@cli.command()
@click.option('--out-path', '-out', type=click.Path(),
              help='Path for generated souce files.')
@click.option('--source', '-s', type=click.Path(), multiple=True,
              help='Source file to convert.')
@click.option('--driver', '-d', type=click.Path(),
              help='Driver file to convert.')
@click.option('--header', '-h', type=click.Path(), multiple=True,
              help='Path for additional header file(s).')
@click.option('--xmod', '-M', type=click.Path(), multiple=True,
              help='Path for additional module file(s)')
@click.option('--include', '-I', type=click.Path(), multiple=True,
              help='Path for additional header file(s)')
@click.option('--strip-omp-do', is_flag=True, default=False,
              help='Removes existing !$omp do loop pragmas')
@click.option('--mode', '-m', default='sca',
              type=click.Choice(['sca', 'claw']))
@click.option('--frontend', default='fp', type=click.Choice(['fp', 'ofp', 'omni']),
              help='Frontend parser to use (default FP)')
def convert(out_path, source, driver, header, xmod, include, strip_omp_do, mode, frontend):
    """
    Single Column Abstraction (SCA): Convert kernel into single-column
    format and adjust driver to apply it over in a horizontal loop.

    Optionally, this can also insert CLAW directives that may be use
    for further downstream transformations.
    """
    driver_name = 'CLOUDSC_DRIVER'
    kernel_name = 'CLOUDSC'

    frontend = Frontend[frontend.upper()]
    frontend_type = Frontend.OFP if frontend == Frontend.OMNI else frontend
    definitions = flatten(SourceFile.from_file(h, xmods=xmod,
                                               frontend=frontend_type).modules for h in header)

    driver = SourceFile.from_file(driver, xmods=xmod, includes=include,
                                  frontend=frontend, builddir=out_path)
    kernels = [SourceFile.from_file(src, definitions=definitions, frontend=frontend,
                                    xmods=xmod, includes=include, builddir=out_path)
               for src in source]

    # Get a separate list of routine objects and names for transformations
    kernel_routines = flatten(kernel.all_subroutines for kernel in kernels)
    kernel_targets = [routine.name.upper() for routine in kernel_routines]

    # Ensure that the kernel calls have all IPA meta-information
    driver[driver_name].enrich_calls(routines=kernel_routines)
    for kernel in kernels:
        for routine in kernel.all_subroutines:
            routine.enrich_calls(routines=kernel_routines)

    # First, remove all derived-type arguments; caller first!
    driver.apply(DerivedTypeArgumentsTransformation(), role='driver')
    for kernel in kernels:
        kernel.apply(DerivedTypeArgumentsTransformation(), role='kernel')

    # Define the target dimension to strip from kernel and caller
    horizontal = Dimension(name='KLON', aliases=['NPROMA', 'KDIM%KLON'],
                           variable='JL', iteration=('KIDIA', 'KFDIA'))

    # Now we instantiate our SCA pipeline and apply the changes
    if mode == 'sca':
        sca_transform = ExtractSCATransformation(dimension=horizontal)
    elif mode == 'claw':
        sca_transform = CLAWTransformation(dimension=horizontal)
    driver.apply(sca_transform, role='driver', targets=['CLOUDSC'])
    for kernel in kernels:
        kernel.apply(sca_transform, role='kernel', targets=kernel_targets)

    if strip_omp_do:
        remove_omp_do(driver[driver_name])

    # Housekeeping: Inject our re-named kernel and auto-wrapped it in a module
    dependency = DependencyTransformation(suffix='_{}'.format(mode.upper()),
                                          mode='module', module_suffix='_MOD')
    for kernel in kernels:
        kernel.apply(dependency, role='kernel', targets=kernel_targets)
        kernel.write(path=Path(out_path)/kernel.path.with_suffix('.%s.F90' % mode).name)

    # Re-generate the driver that mimicks the original source file,
    # but imports and calls our re-generated kernel.
    driver.apply(dependency, role='driver', targets=kernel_name)
    driver.write(path=Path(out_path)/driver.path.with_suffix('.%s.F90' % mode).name)


@cli.command()
@click.option('--out-path', '-out', type=click.Path(),
              help='Path for generated souce files.')
@click.option('--header', '-I', type=click.Path(), multiple=True,
              help='Path for additional header file(s).')
@click.option('--source', '-s', type=click.Path(),
              help='Source file to convert.')
@click.option('--driver', '-d', type=click.Path(),
              help='Driver file to convert.')
@click.option('--xmod', '-M', type=click.Path(), multiple=True,
              help='Path for additional module file(s)')
@click.option('--include', '-I', type=click.Path(), multiple=True,
              help='Path for additional header file(s)')
@click.option('--frontend', default='omni', type=click.Choice(['fp', 'ofp', 'omni']),
              help='Frontend parser to use (default FP)')
def transpile(out_path, header, source, driver, xmod, include, frontend):
    """
    Convert kernels to C and generate ISO-C bindings and interfaces.
    """
    driver_name = 'CLOUDSC_DRIVER'
    kernel_name = 'CLOUDSC'

    frontend = Frontend[frontend.upper()]
    frontend_type = Frontend.OFP if frontend == Frontend.OMNI else frontend

    # Parse original driver and kernel routine, and enrich the driver
    definitions = flatten(SourceFile.from_file(h, xmods=xmod,
                                               frontend=frontend_type).modules for h in header)
    kernel = SourceFile.from_file(source, xmods=xmod, includes=include,
                                  frontend=frontend, definitions=definitions,
                                  builddir=out_path)
    driver = SourceFile.from_file(driver, xmods=xmod, includes=include,
                                  frontend=frontend, builddir=out_path)
    # Ensure that the kernel calls have all meta-information
    driver[driver_name].enrich_calls(routines=kernel[kernel_name])

    # First, remove all derived-type arguments; caller first!
    driver.apply(DerivedTypeArgumentsTransformation(), role='driver')
    kernel.apply(DerivedTypeArgumentsTransformation(), role='kernel')

    # Now we instantiate our pipeline and apply the changes
    transformation = FortranCTransformation()
    transformation.apply(kernel, role='kernel', path=out_path)

    # Traverse header modules to create getter functions for module variables
    for header in definitions:
        transformation.apply(header, role='header', path=out_path)

    # Housekeeping: Inject our re-named kernel and auto-wrapped it in a module
    dependency = DependencyTransformation(suffix='_FC', mode='module', module_suffix='_MOD')
    kernel.apply(dependency, role='kernel')
    kernel.write(path=Path(out_path)/kernel.path.with_suffix('.c.F90').name)

    # Re-generate the driver that mimicks the original source file,
    # but imports and calls our re-generated kernel.
    driver.apply(dependency, role='driver', targets=kernel_name)
    driver.write(path=Path(out_path)/driver.path.with_suffix('.c.F90').name)


if __name__ == "__main__":
    cli()
