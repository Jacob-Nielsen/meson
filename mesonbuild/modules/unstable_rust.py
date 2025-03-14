# Copyright © 2020 Intel Corporation

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import typing as T

from . import ExtensionModule, ModuleReturnValue
from .. import mlog
from ..build import BuildTarget, CustomTargetIndex, Executable, GeneratedList, InvalidArguments, IncludeDirs, CustomTarget
from ..dependencies import Dependency, ExternalLibrary
from ..interpreter import ExecutableHolder, BuildTargetHolder, CustomTargetHolder, permitted_test_kwargs
from ..interpreterbase import InterpreterException, permittedKwargs, FeatureNew, typed_pos_args, noPosargs
from ..mesonlib import stringlistify, unholder, listify, typeslistify, File

if T.TYPE_CHECKING:
    from . import ModuleState
    from ..interpreter import Interpreter
    from ..programs import ExternalProgram
    from ..interpreter.interpreter import SourceOutputs


class RustModule(ExtensionModule):

    """A module that holds helper functions for rust."""

    @FeatureNew('rust module', '0.57.0')
    def __init__(self, interpreter: 'Interpreter') -> None:
        super().__init__(interpreter)
        self._bindgen_bin: T.Optional['ExternalProgram'] = None

    @permittedKwargs(permitted_test_kwargs | {'dependencies'} ^ {'protocol'})
    @typed_pos_args('rust.test', str, BuildTargetHolder)
    def test(self, state: 'ModuleState', args: T.Tuple[str, BuildTargetHolder], kwargs: T.Dict[str, T.Any]) -> ModuleReturnValue:
        """Generate a rust test target from a given rust target.

        Rust puts it's unitests inside it's main source files, unlike most
        languages that put them in external files. This means that normally
        you have to define two separate targets with basically the same
        arguments to get tests:

        ```meson
        rust_lib_sources = [...]
        rust_lib = static_library(
            'rust_lib',
            rust_lib_sources,
        )

        rust_lib_test = executable(
            'rust_lib_test',
            rust_lib_sources,
            rust_args : ['--test'],
        )

        test(
            'rust_lib_test',
            rust_lib_test,
            protocol : 'rust',
        )
        ```

        This is all fine, but not very DRY. This method makes it much easier
        to define rust tests:

        ```meson
        rust = import('unstable-rust')

        rust_lib = static_library(
            'rust_lib',
            [sources],
        )

        rust.test('rust_lib_test', rust_lib)
        ```
        """
        name = args[0]
        base_target: BuildTarget = unholder(args[1])
        if not base_target.uses_rust():
            raise InterpreterException('Second positional argument to rustmod.test() must be a rust based target')
        extra_args = stringlistify(kwargs.get('args', []))

        # Delete any arguments we don't want passed
        if '--test' in extra_args:
            mlog.warning('Do not add --test to rustmod.test arguments')
            extra_args.remove('--test')
        if '--format' in extra_args:
            mlog.warning('Do not add --format to rustmod.test arguments')
            i = extra_args.index('--format')
            # Also delete the argument to --format
            del extra_args[i + 1]
            del extra_args[i]
        for i, a in enumerate(extra_args):
            if a.startswith('--format='):
                del extra_args[i]
                break

        dependencies = unholder(listify(kwargs.get('dependencies', [])))
        for d in dependencies:
            if not isinstance(d, (Dependency, ExternalLibrary)):
                raise InvalidArguments('dependencies must be a dependency or external library')

        kwargs['args'] = extra_args + ['--test', '--format', 'pretty']
        kwargs['protocol'] = 'rust'

        new_target_kwargs = base_target.kwargs.copy()
        # Don't mutate the shallow copied list, instead replace it with a new
        # one
        new_target_kwargs['rust_args'] = new_target_kwargs.get('rust_args', []) + ['--test']
        new_target_kwargs['install'] = False
        new_target_kwargs['dependencies'] = new_target_kwargs.get('dependencies', []) + dependencies

        new_target = Executable(
            name, base_target.subdir, state.subproject,
            base_target.for_machine, base_target.sources,
            base_target.objects, base_target.environment,
            new_target_kwargs
        )

        e = ExecutableHolder(new_target, self.interpreter)
        test = self.interpreter.make_test(
            self.interpreter.current_node, [name, e], kwargs)

        return ModuleReturnValue([], [e, test])

    @noPosargs
    @permittedKwargs({'input', 'output', 'include_directories', 'c_args', 'args'})
    def bindgen(self, state: 'ModuleState', args: T.List, kwargs: T.Dict[str, T.Any]) -> ModuleReturnValue:
        """Wrapper around bindgen to simplify it's use.

        The main thing this simplifies is the use of `include_directory`
        objects, instead of having to pass a plethora of `-I` arguments.
        """
        header: 'SourceOutputs'
        _deps: T.Sequence['SourceOutputs']
        try:
            header, *_deps = unholder(self.interpreter.source_strings_to_files(listify(kwargs['input'])))
        except KeyError:
            raise InvalidArguments('rustmod.bindgen() `input` argument must have at least one element.')

        try:
            output: str = kwargs['output']
        except KeyError:
            raise InvalidArguments('rustmod.bindgen() `output` must be provided')
        if not isinstance(output, str):
            raise InvalidArguments('rustmod.bindgen() `output` argument must be a string.')

        include_dirs: T.List[IncludeDirs] = typeslistify(unholder(listify(kwargs.get('include_directories', []))), IncludeDirs)
        c_args: T.List[str] = stringlistify(listify(kwargs.get('c_args', [])))
        bind_args: T.List[str] = stringlistify(listify(kwargs.get('args', [])))

        # Split File and Target dependencies to add pass to CustomTarget
        depends: T.List[T.Union[GeneratedList, BuildTarget, CustomTargetIndex]] = []
        depend_files: T.List[File] = []
        for d in _deps:
            if isinstance(d, File):
                depend_files.append(d)
            else:
                depends.append(d)

        inc_strs: T.List[str] = []
        for i in include_dirs:
            # bindgen always uses clang, so it's safe to hardcode -I here
            inc_strs.extend([f'-I{x}' for x in i.to_string_list(state.environment.get_source_dir())])

        if self._bindgen_bin is None:
            # there's some bugs in the interpreter typeing.
            self._bindgen_bin = T.cast('ExternalProgram', self.interpreter.find_program_impl(['bindgen']).held_object)

        name: str
        if isinstance(header, File):
            name = header.fname
        else:
            name = header.get_outputs()[0]

        target = CustomTarget(
            f'rustmod-bindgen-{name}'.replace('/', '_'),
            state.subdir,
            state.subproject,
            {
                'input': header,
                'output': output,
                'command': self._bindgen_bin.get_command() + [
                    '@INPUT@', '--output',
                    os.path.join(state.environment.build_dir, '@OUTPUT@')] +
                    bind_args + ['--'] + c_args + inc_strs +
                    ['-MD', '-MQ', '@INPUT@', '-MF', '@DEPFILE@'],
                'depfile': '@PLAINNAME@.d',
                'depends': depends,
                'depend_files': depend_files,
            },
            backend=state.backend,
        )

        return ModuleReturnValue([target], [CustomTargetHolder(target, self.interpreter)])


def initialize(*args: T.List, **kwargs: T.Dict) -> RustModule:
    return RustModule(*args, **kwargs)  # type: ignore
