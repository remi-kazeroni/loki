from pathlib import Path
import pytest

from conftest import jit_compile, clean_test
from loki import (
    OFP, OMNI, FP, Sourcefile, Subroutine, CallStatement, Import,
    FindNodes, FindInlineCalls, fgen,
    Assignment, IntLiteral, Module
)
from loki.transform import Transformation, DependencyTransformation, replace_selected_kind


@pytest.fixture(scope='module', name='here')
def fixture_here():
    return Path(__file__).parent


@pytest.fixture(scope='module', name='rename_transform')
def fixture_rename_transform():

    class RenameTransform(Transformation):
        """
        Simple `Transformation` object that renames subroutine and modules.
        """

        def transform_subroutine(self, routine, **kwargs):
            routine.name += '_test'

        def transform_module(self, module, **kwargs):
            module.name += '_test'

    return RenameTransform()


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transformation_apply(rename_transform, frontend):
    """
    Apply a simple transformation that renames routines and modules
    """
    fcode = """
module mymodule
  real(kind=4) :: myvar
end module mymodule

subroutine myroutine(a, b)
  real(kind=4), intent(inout) :: a, b

  a = a + b
end subroutine myroutine
"""
    # Let source apply transformation to all items and verify
    source = Sourcefile.from_source(fcode, frontend=frontend)
    source.apply(rename_transform)
    assert source.modules[0].name == 'mymodule_test'
    assert source['mymodule_test'] == source.modules[0]
    assert source.subroutines[0].name == 'myroutine_test'
    assert source['myroutine_test'] == source.subroutines[0]

    # Apply transformation explicitly to whole source and verify
    source = Sourcefile.from_source(fcode, frontend=frontend)
    rename_transform.apply(source)
    assert source.modules[0].name == 'mymodule_test'
    assert source['mymodule_test'] == source.modules[0]
    assert source.subroutines[0].name == 'myroutine_test'
    assert source['myroutine_test'] == source.subroutines[0]


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transformation_apply_subroutine(rename_transform, frontend):
    """
    Apply a simple transformation that renames routines and modules
    """
    fcode = """
module mymodule
  real(kind=4) :: myvar

contains

  subroutine module_routine(argument)
    real(kind=4), intent(inout) :: argument

    argument = member_func()

  contains
    function member_func() result(res)
      real(kind=4) :: res

      res = 4.
    end function member_func
  end subroutine module_routine
end module mymodule

subroutine myroutine(a, b)
  real(kind=4), intent(inout) :: a, b

  a = a + b
end subroutine myroutine
"""
    # Let only the inner module routine apply the transformation
    source = Sourcefile.from_source(fcode, frontend=frontend)
    source['module_routine'].apply(rename_transform)
    assert source.modules[0].name == 'mymodule'
    assert source['mymodule'] == source.modules[0]
    assert source.subroutines[0].name == 'myroutine'
    assert source['myroutine'] == source.subroutines[0]
    assert len(source.all_subroutines) == 2  # Ignore member func
    assert source.all_subroutines[1].name == 'module_routine_test'
    assert source['module_routine_test'] == source.all_subroutines[1]
    assert len(source['module_routine_test'].members) == 1
    assert source['module_routine_test'].members[0].name == 'member_func_test'

    # Apply transformation explicitly to the outer routine
    source = Sourcefile.from_source(fcode, frontend=frontend)
    rename_transform.apply_subroutine(source['myroutine'])
    assert source.modules[0].name == 'mymodule'
    assert source['mymodule'] == source.modules[0]
    assert source.subroutines[0].name == 'myroutine_test'
    assert source['myroutine_test'] == source.subroutines[0]
    assert len(source.all_subroutines) == 2
    assert source.all_subroutines[1].name == 'module_routine'
    assert source['module_routine'] == source.all_subroutines[1]


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transformation_apply_module(rename_transform, frontend):
    """
    Apply a simple transformation that renames routines and modules
    """
    fcode = """
module mymodule
  real(kind=4) :: myvar

contains

  subroutine module_routine(argument)
    real(kind=4), intent(inout) :: argument

    argument = argument  + 1.
  end subroutine module_routine
end module mymodule

subroutine myroutine(a, b)
  real(kind=4), intent(inout) :: a, b

  a = a + b
end subroutine myroutine
"""
    # Let the module and apply the transformation to everything it contains
    source = Sourcefile.from_source(fcode, frontend=frontend)
    source['mymodule'].apply(rename_transform)
    assert source.modules[0].name == 'mymodule_test'
    assert source['mymodule_test'] == source.modules[0]
    assert len(source.all_subroutines) == 2
    # Outer subroutine is untouched, since we apply all
    # transformations to anything in the module.
    assert source.subroutines[0].name == 'myroutine'
    assert source['myroutine'] == source.subroutines[0]
    assert source.all_subroutines[1].name == 'module_routine_test'
    assert source['module_routine_test'] == source.all_subroutines[1]

    # Apply transformation only to modules, not subroutines, in the source
    source = Sourcefile.from_source(fcode, frontend=frontend)
    rename_transform.apply_module(source['mymodule'])
    assert source.modules[0].name == 'mymodule_test'
    assert source['mymodule_test'] == source.modules[0]
    assert len(source.all_subroutines) == 2
    assert source.subroutines[0].name == 'myroutine'
    assert source['myroutine'] == source.subroutines[0]
    assert source.all_subroutines[1].name == 'module_routine_test'
    assert source['module_routine_test'] == source.all_subroutines[1]


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_dependency_transformation_module_imports(frontend):
    """
    Test injection of suffixed kernels into unchanged driver
    routines via module imports.
    """

    kernel = Sourcefile.from_source(source="""
MODULE kernel_mod
CONTAINS
    SUBROUTINE kernel(a, b, c)
    INTEGER, INTENT(INOUT) :: a, b, c

    a = 1
    b = 2
    c = 3
  END SUBROUTINE kernel
END MODULE kernel_mod
""", frontend=frontend)

    driver = Sourcefile.from_source(source="""
MODULE driver_mod
  USE kernel_mod, only: kernel
CONTAINS
  SUBROUTINE driver(a, b, c)
    INTEGER, INTENT(INOUT) :: a, b, c

    CALL kernel(a, b ,c)
  END SUBROUTINE driver
END MODULE driver_mod
""", frontend=frontend)

    transformation = DependencyTransformation(suffix='_test', module_suffix='_mod')
    kernel.apply(transformation, role='kernel')
    driver.apply(transformation, role='driver', targets='kernel')

    # Check that the basic entity names in the kernel source have changed
    assert kernel.all_subroutines[0].name == 'kernel_test'
    assert kernel['kernel_test'] == kernel.all_subroutines[0]
    assert kernel.modules[0].name == 'kernel_test_mod'
    assert kernel['kernel_test_mod'] == kernel.modules[0]

    # Check that the entity names in the driver have not changed
    assert driver.all_subroutines[0].name == 'driver'
    assert driver['driver'] == driver.all_subroutines[0]
    assert driver.modules[0].name == 'driver_mod'
    assert driver['driver_mod'] == driver.modules[0]

    # Check that calls and imports have been diverted to the re-generated routine
    calls = FindNodes(CallStatement).visit(driver['driver'].body)
    assert len(calls) == 1
    assert calls[0].name == 'kernel_test'
    imports = FindNodes(Import).visit(driver['driver_mod'].spec)
    assert len(imports) == 1
    assert isinstance(imports[0], Import)
    assert driver['driver_mod'].spec.body[0].module == 'kernel_test_mod'
    assert 'kernel_test' in [str(s) for s in driver['driver_mod'].spec.body[0].symbols]


@pytest.mark.parametrize('frontend', [
    OFP,
    FP,
    pytest.param(OMNI, marks=pytest.mark.xfail(reason='C-imports need pre-processing for OMNI')),
])
def test_dependency_transformation_header_includes(here, frontend):
    """
    Test injection of suffixed kernels into unchanged driver
    routines via c-header includes.
    """

    driver = Sourcefile.from_source(source="""
SUBROUTINE driver(a, b, c)
  INTEGER, INTENT(INOUT) :: a, b, c

#include "kernel.intfb.h"

  CALL kernel(a, b ,c)
END SUBROUTINE driver
""", frontend=frontend)

    kernel = Sourcefile.from_source(source="""
SUBROUTINE kernel(a, b, c)
  INTEGER, INTENT(INOUT) :: a, b, c

  a = 1
  b = 2
  c = 3
END SUBROUTINE kernel
""", frontend=frontend)

    # Ensure header file does not exist a-priori
    header_file = here/'kernel_test.intfb.h'
    if header_file.exists():
        header_file.unlink()

    # Apply injection transformation via C-style includes by giving `include_path`
    transformation = DependencyTransformation(suffix='_test', mode='strict', include_path=here)
    kernel.apply(transformation, role='kernel')
    driver.apply(transformation, role='driver', targets='kernel')

    # Check that the subroutine name in the kernel source has changed
    assert len(kernel.modules) == 0
    assert len(kernel.subroutines) == 1
    assert kernel.subroutines[0].name == 'kernel_test'
    assert kernel['kernel_test'] == kernel.all_subroutines[0]

    # Check that the driver name has not changed
    assert len(kernel.modules) == 0
    assert len(kernel.subroutines) == 1
    assert driver.subroutines[0].name == 'driver'

    # Check that the import has been updated
    assert '#include "kernel.intfb.h"' not in driver.to_fortran()
    assert '#include "kernel_test.intfb.h"' in driver.to_fortran()

    # Check that header file was generated and clean up
    assert header_file.exists()
    header_file.unlink()


@pytest.mark.parametrize('frontend', [
    OFP,
    FP,
    pytest.param(OMNI, marks=pytest.mark.xfail(reason='C-imports need pre-processing for OMNI')),
])
def test_dependency_transformation_module_wrap(frontend):
    """
    Test injection of suffixed kernels into unchanged driver
    routines automatic module wrapping of the kernel.
    """

    driver = Sourcefile.from_source(source="""
SUBROUTINE driver(a, b, c)
  INTEGER, INTENT(INOUT) :: a, b, c

#include "kernel.intfb.h"

  CALL kernel(a, b ,c)
END SUBROUTINE driver
""", frontend=frontend)

    kernel = Sourcefile.from_source(source="""
SUBROUTINE kernel(a, b, c)
  INTEGER, INTENT(INOUT) :: a, b, c

  a = 1
  b = 2
  c = 3
END SUBROUTINE kernel
""", frontend=frontend)

    # Apply injection transformation via C-style includes by giving `include_path`
    transformation = DependencyTransformation(suffix='_test', mode='module', module_suffix='_mod')
    kernel.apply(transformation, role='kernel')
    driver.apply(transformation, role='driver', targets='kernel')

    # Check that the kernel has been wrapped
    assert len(kernel.subroutines) == 0
    assert len(kernel.all_subroutines) == 1
    assert kernel.all_subroutines[0].name == 'kernel_test'
    assert kernel['kernel_test'] == kernel.all_subroutines[0]
    assert len(kernel.modules) == 1
    assert kernel.modules[0].name == 'kernel_test_mod'
    assert kernel['kernel_test_mod'] == kernel.modules[0]

    # Check that the driver name has not changed
    assert len(driver.modules) == 0
    assert len(driver.subroutines) == 1
    assert driver.subroutines[0].name == 'driver'

    # Check that calls and imports have been diverted to the re-generated routine
    calls = FindNodes(CallStatement).visit(driver['driver'].body)
    assert len(calls) == 1
    assert calls[0].name == 'kernel_test'
    imports = FindNodes(Import).visit(driver['driver'].spec)
    assert len(imports) == 1
    assert imports[0].module == 'kernel_test_mod'
    assert 'kernel_test' in [str(s) for s in imports[0].symbols]


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_replace_selected_kind(here, frontend):
    """
    Test correct replacement of all `selected_x_kind` calls by
    iso_fortran_env constant.
    """
    fcode = """
subroutine transform_replace_selected_kind(i, a)
  use iso_fortran_env, only: int8
  implicit none
  integer, parameter :: jprm = selected_real_kind(6,37)
  integer(kind=selected_int_kind(9)), intent(out) :: i
  real(kind=selected_real_kind(13,300)), intent(out) :: a
  integer(kind=int8) :: j = 1
  integer(kind=selected_int_kind(1)) :: k = 9
  real(kind=selected_real_kind(7)) :: b = 5._jprm
  real(kind=selected_real_kind(r=2, p=4)) :: c = 1.

  i = j + k
  a = b + c + real(4, kind=selected_real_kind(6, r=37))
end subroutine transform_replace_selected_kind
    """.strip()

    routine = Subroutine.from_source(fcode, frontend=frontend)
    imports = FindNodes(Import).visit(routine.spec)
    assert len(imports) == 1 and imports[0].module.lower() == 'iso_fortran_env'
    assert len(imports[0].symbols) == 1 and imports[0].symbols[0].name.lower() == 'int8'

    # Test the original implementation
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    i, a = function()
    assert i == 10
    assert a == 10.

    # Apply transformation and check imports
    replace_selected_kind(routine)
    assert not [call for call in FindInlineCalls().visit(routine.ir)
                if call.name.lower().startswith('selected')]

    imports = FindNodes(Import).visit(routine.spec)
    assert len(imports) == 1 and imports[0].module.lower() == 'iso_fortran_env'

    source = fgen(routine).lower()
    assert not 'selected_real_kind' in source
    assert not 'selected_int_kind' in source

    if frontend == OMNI:
        # F£$%^% OMNI replaces randomly SOME selected_real_kind calls by
        # (wrong!) integer kinds
        symbols = {'int8', 'real32', 'real64'}
    else:
        symbols = {'int8', 'int32', 'real32', 'real64'}

    assert len(imports[0].symbols) == len(symbols)
    assert {s.name.lower() for s in imports[0].symbols} == symbols

    # Test the transformed implementation
    iso_filepath = here/('%s_replaced_%s.f90' % (routine.name, frontend))
    iso_function = jit_compile(routine, filepath=iso_filepath, objname=routine.name)

    i, a = iso_function()
    assert i == 10
    assert a == 10.

    clean_test(filepath)
    clean_test(iso_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transformation_post_apply_subroutine(here, frontend):
    """Verify that post_apply is called for subroutines."""

    #### Test that rescoping is applied and effective ####

    tmp_routine = Subroutine('some_routine')
    class ScopingErrorTransformation(Transformation):
        """Intentionally idiotic transformation that introduces a scoping error."""

        def transform_subroutine(self, routine, **kwargs):
            i = routine.variable_map['i']
            j = i.clone(name='j', scope=tmp_routine, type=i.type.clone(intent=None))
            routine.variables += (j,)
            routine.body.append(Assignment(lhs=j, rhs=IntLiteral(2)))
            routine.body.append(Assignment(lhs=i, rhs=j))
            routine.name += '_transformed'
            assert routine.variable_map['j'].scope is tmp_routine

    fcode = """
subroutine transformation_post_apply(i)
  integer, intent(out) :: i
  i = 1
end subroutine transformation_post_apply
    """.strip()

    routine = Subroutine.from_source(fcode, frontend=frontend)

    # Test the original implementation
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    i = function()
    assert i == 1

    # Apply transformation and make sure variable scope is correct
    routine.apply(ScopingErrorTransformation())
    assert routine.variable_map['j'].scope is routine

    new_filepath = here/('%s_%s.f90' % (routine.name, frontend))
    new_function = jit_compile(routine, filepath=new_filepath, objname=routine.name)

    i = new_function()
    assert i == 2

    clean_test(filepath)
    clean_test(new_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transformation_post_apply_module(here, frontend):
    """Verify that post_apply is called for modules."""

    #### Test that rescoping is applied and effective ####

    tmp_scope = Module('some_module')
    class ScopingErrorTransformation(Transformation):
        """Intentionally idiotic transformation that introduces a scoping error."""

        def transform_module(self, module, **kwargs):
            i = module.variable_map['i']
            j = i.clone(name='j', scope=tmp_scope, type=i.type.clone(intent=None))
            module.variables += (j,)
            routine = module.subroutines[0]
            routine.body.prepend(Assignment(lhs=i, rhs=j))
            routine.body.prepend(Assignment(lhs=j, rhs=IntLiteral(2)))
            module.name += '_transformed'
            assert module.variable_map['j'].scope is tmp_scope

    fcode = """
module transformation_module_post_apply
  integer :: i = 0
contains
  subroutine test_post_apply(ret)
    integer, intent(out) :: ret
    i = i + 1
    ret = i
  end subroutine test_post_apply
end module transformation_module_post_apply
    """.strip()

    module = Module.from_source(fcode, frontend=frontend)

    # Test the original implementation
    filepath = here/('%s_%s.f90' % (module.name, frontend))
    mod = jit_compile(module, filepath=filepath, objname=module.name)

    i = mod.test_post_apply()
    assert i == 1

    # Apply transformation
    module.apply(ScopingErrorTransformation())
    assert module.variable_map['j'].scope is module

    new_filepath = here/('%s_%s.f90' % (module.name, frontend))
    new_mod = jit_compile(module, filepath=new_filepath, objname=module.name)

    i = new_mod.test_post_apply()
    assert i == 3

    clean_test(filepath)
    clean_test(new_filepath)
