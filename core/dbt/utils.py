import collections
import concurrent.futures
import copy
import datetime
import decimal
import functools
import hashlib
import itertools
import json
import os
from enum import Enum
from typing_extensions import Protocol
from typing import (
    Tuple, Type, Any, Optional, TypeVar, Dict, Union, Callable, List, Iterator,
    Mapping, Iterable, AbstractSet, Set
)

import dbt.exceptions

from dbt.logger import GLOBAL_LOGGER as logger
from dbt.node_types import NodeType
from dbt.clients import yaml_helper

DECIMALS: Tuple[Type[Any], ...]
try:
    import cdecimal  # typing: ignore
except ImportError:
    DECIMALS = (decimal.Decimal,)
else:
    DECIMALS = (decimal.Decimal, cdecimal.Decimal)


class ExitCodes(int, Enum):
    Success = 0
    ModelError = 1
    UnhandledError = 2


def to_bytes(s):
    return s.encode('latin-1')


def coalesce(*args):
    for arg in args:
        if arg is not None:
            return arg
    return None


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def get_profile_from_project(project):
    target_name = project.get('target', {})
    profile = project.get('outputs', {}).get(target_name, {})
    return profile


def get_model_name_or_none(model):
    if model is None:
        name = '<None>'

    elif isinstance(model, str):
        name = model
    elif isinstance(model, dict):
        name = model.get('alias', model.get('name'))
    elif hasattr(model, 'alias'):
        name = model.alias
    elif hasattr(model, 'name'):
        name = model.name
    else:
        name = str(model)
    return name


def compiler_warning(model, msg, resource_type='model'):
    name = get_model_name_or_none(model)
    logger.info(
        "* Compilation warning while compiling {} {}:\n* {}\n"
        .format(resource_type, name, msg)
    )


MACRO_PREFIX = 'dbt_macro__'
DOCS_PREFIX = 'dbt_docs__'


def get_dbt_macro_name(name):
    if name is None:
        raise dbt.exceptions.InternalException('Got None for a macro name!')
    return '{}{}'.format(MACRO_PREFIX, name)


def get_dbt_docs_name(name):
    if name is None:
        raise dbt.exceptions.InternalException('Got None for a doc name!')
    return '{}{}'.format(DOCS_PREFIX, name)


def get_materialization_macro_name(materialization_name, adapter_type=None,
                                   with_prefix=True):
    if adapter_type is None:
        adapter_type = 'default'

    name = 'materialization_{}_{}'.format(materialization_name, adapter_type)

    if with_prefix:
        return get_dbt_macro_name(name)
    else:
        return name


def get_docs_macro_name(docs_name, with_prefix=True):
    if with_prefix:
        return get_dbt_docs_name(docs_name)
    else:
        return docs_name


def split_path(path):
    return path.split(os.sep)


def merge(*args):
    if len(args) == 0:
        return None

    if len(args) == 1:
        return args[0]

    lst = list(args)
    last = lst.pop(len(lst) - 1)

    return _merge(merge(*lst), last)


def _merge(a, b):
    to_return = a.copy()
    to_return.update(b)
    return to_return


# http://stackoverflow.com/questions/20656135/python-deep-merge-dictionary-data
def deep_merge(*args):
    """
    >>> dbt.utils.deep_merge({'a': 1, 'b': 2, 'c': 3}, {'a': 2}, {'a': 3, 'b': 1})  # noqa
    {'a': 3, 'b': 1, 'c': 3}
    """
    if len(args) == 0:
        return None

    if len(args) == 1:
        return copy.deepcopy(args[0])

    lst = list(args)
    last = copy.deepcopy(lst.pop(len(lst) - 1))

    return _deep_merge(deep_merge(*lst), last)


def _deep_merge(destination, source):
    if isinstance(source, dict):
        for key, value in source.items():
            deep_merge_item(destination, key, value)
        return destination


def deep_merge_item(destination, key, value):
    if isinstance(value, dict):
        node = destination.setdefault(key, {})
        destination[key] = deep_merge(node, value)
    elif isinstance(value, tuple) or isinstance(value, list):
        if key in destination:
            destination[key] = list(value) + list(destination[key])
        else:
            destination[key] = value
    else:
        destination[key] = value


def _deep_map(
    func: Callable[[Any, Tuple[Union[str, int], ...]], Any],
    value: Any,
    keypath: Tuple[Union[str, int], ...],
) -> Any:
    atomic_types: Tuple[Type[Any], ...] = (int, float, str, type(None), bool)

    ret: Any

    if isinstance(value, list):
        ret = [
            _deep_map(func, v, (keypath + (idx,)))
            for idx, v in enumerate(value)
        ]
    elif isinstance(value, dict):
        ret = {
            k: _deep_map(func, v, (keypath + (str(k),)))
            for k, v in value.items()
        }
    elif isinstance(value, atomic_types):
        ret = func(value, keypath)
    else:
        container_types: Tuple[Type[Any], ...] = (list, dict)
        ok_types = container_types + atomic_types
        raise dbt.exceptions.DbtConfigError(
            'in _deep_map, expected one of {!r}, got {!r}'
            .format(ok_types, type(value))
        )

    return ret


def deep_map(
    func: Callable[[Any, Tuple[Union[str, int], ...]], Any],
    value: Any
) -> Any:
    """map the function func() onto each non-container value in 'value'
    recursively, returning a new value. As long as func does not manipulate
    value, then deep_map will also not manipulate it.

    value should be a value returned by `yaml.safe_load` or `json.load` - the
    only expected types are list, dict, native python number, str, NoneType,
    and bool.

    func() will be called on numbers, strings, Nones, and booleans. Its first
    parameter will be the value, and the second will be its keypath, an
    iterable over the __getitem__ keys needed to get to it.

    :raises: If there are cycles in the value, raises a
        dbt.exceptions.RecursionException
    """
    try:
        return _deep_map(func, value, ())
    except RuntimeError as exc:
        if 'maximum recursion depth exceeded' in str(exc):
            raise dbt.exceptions.RecursionException(
                'Cycle detected in deep_map'
            )
        raise


class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self


def get_pseudo_test_path(node_name, source_path, test_type):
    "schema tests all come from schema.yml files. fake a source sql file"
    source_path_parts = split_path(source_path)
    source_path_parts.pop()  # ignore filename
    suffix = [test_type, "{}.sql".format(node_name)]
    pseudo_path_parts = source_path_parts + suffix
    return os.path.join(*pseudo_path_parts)


def get_pseudo_hook_path(hook_name):
    path_parts = ['hooks', "{}.sql".format(hook_name)]
    return os.path.join(*path_parts)


def md5(string):
    return hashlib.md5(string.encode('utf-8')).hexdigest()


def get_hash(model):
    return hashlib.md5(model.unique_id.encode('utf-8')).hexdigest()


def get_hashed_contents(model):
    return hashlib.md5(model.raw_sql.encode('utf-8')).hexdigest()


def flatten_nodes(dep_list):
    return list(itertools.chain.from_iterable(dep_list))


class memoized:
    '''Decorator. Caches a function's return value each time it is called. If
    called later with the same arguments, the cached value is returned (not
    reevaluated).

    Taken from https://wiki.python.org/moin/PythonDecoratorLibrary#Memoize'''
    def __init__(self, func):
        self.func = func
        self.cache = {}

    def __call__(self, *args):
        if not isinstance(args, collections.Hashable):
            # uncacheable. a list, for instance.
            # better to not cache than blow up.
            return self.func(*args)
        if args in self.cache:
            return self.cache[args]
        value = self.func(*args)
        self.cache[args] = value
        return value

    def __repr__(self):
        '''Return the function's docstring.'''
        return self.func.__doc__

    def __get__(self, obj, objtype):
        '''Support instance methods.'''
        return functools.partial(self.__call__, obj)


def invalid_ref_test_message(node, target_model_name, target_model_package,
                             disabled):
    if disabled:
        msg = dbt.exceptions.get_target_disabled_msg(
            node, target_model_name, target_model_package
        )
    else:
        msg = dbt.exceptions.get_target_not_found_msg(
            node, target_model_name, target_model_package
        )
    return 'WARNING: {}'.format(msg)


def invalid_ref_fail_unless_test(node, target_model_name,
                                 target_model_package, disabled):
    if node.resource_type == NodeType.Test:
        msg = invalid_ref_test_message(node, target_model_name,
                                       target_model_package, disabled)
        if disabled:
            logger.debug(msg)
        else:
            dbt.exceptions.warn_or_error(msg)

    else:
        dbt.exceptions.ref_target_not_found(
            node,
            target_model_name,
            target_model_package)


def invalid_source_fail_unless_test(node, target_name, target_table_name):
    if node.resource_type == NodeType.Test:
        msg = dbt.exceptions.source_disabled_message(node, target_name,
                                                     target_table_name)
        dbt.exceptions.warn_or_error(msg, log_fmt='WARNING: {}')
    else:
        dbt.exceptions.source_target_not_found(node, target_name,
                                               target_table_name)


def parse_cli_vars(var_string: str) -> Dict[str, Any]:
    try:
        cli_vars = yaml_helper.load_yaml_text(var_string)
        var_type = type(cli_vars)
        if var_type is dict:
            return cli_vars
        else:
            type_name = var_type.__name__
            dbt.exceptions.raise_compiler_error(
                "The --vars argument must be a YAML dictionary, but was "
                "of type '{}'".format(type_name))
    except dbt.exceptions.ValidationException:
        logger.error(
            "The YAML provided in the --vars argument is not valid.\n"
        )
        raise


K_T = TypeVar('K_T')
V_T = TypeVar('V_T')


def filter_null_values(input: Dict[K_T, Optional[V_T]]) -> Dict[K_T, V_T]:
    return {k: v for k, v in input.items() if v is not None}


def add_ephemeral_model_prefix(s: str) -> str:
    return '__dbt__CTE__{}'.format(s)


def timestring() -> str:
    """Get the current datetime as an RFC 3339-compliant string"""
    # isoformat doesn't include the mandatory trailing 'Z' for UTC.
    return datetime.datetime.utcnow().isoformat() + 'Z'


class JSONEncoder(json.JSONEncoder):
    """A 'custom' json encoder that does normal json encoder things, but also
    handles `Decimal`s. Naturally, this can lose precision because they get
    converted to floats.
    """
    def default(self, obj):
        if isinstance(obj, DECIMALS):
            return float(obj)
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        if hasattr(obj, 'to_dict'):
            # if we have a to_dict we should try to serialize the result of
            # that!
            obj = obj.to_dict()
        return super().default(obj)


class ForgivingJSONEncoder(JSONEncoder):
    def default(self, obj):
        # let dbt's default JSON encoder handle it if possible, fallback to
        # str()
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def translate_aliases(
    kwargs: Dict[str, Any], aliases: Dict[str, str], recurse: bool = False,
) -> Dict[str, Any]:
    """Given a dict of keyword arguments and a dict mapping aliases to their
    canonical values, canonicalize the keys in the kwargs dict.

    If recurse is True, perform this operation recursively.

    :return: A dict continaing all the values in kwargs referenced by their
        canonical key.
    :raises: `AliasException`, if a canonical key is defined more than once.
    """
    result: Dict[str, Any] = {}

    for given_key, value in kwargs.items():
        canonical_key = aliases.get(given_key, given_key)
        if canonical_key in result:
            # dupe found: go through the dict so we can have a nice-ish error
            key_names = ', '.join("{}".format(k) for k in kwargs if
                                  aliases.get(k) == canonical_key)

            raise dbt.exceptions.AliasException(
                'Got duplicate keys: ({}) all map to "{}"'
                .format(key_names, canonical_key)
            )
        if recurse:
            if isinstance(value, dict):
                value = translate_aliases(value, aliases, recurse)
            elif isinstance(value, list):
                value = [translate_aliases(v, aliases, recurse) for v in value]
        result[canonical_key] = value
    return result


def _pluralize(string: Union[str, NodeType]) -> str:
    try:
        convert = NodeType(string)
    except ValueError:
        return f'{string}s'
    else:
        return convert.pluralize()


def pluralize(count, string: Union[str, NodeType]):
    pluralized: str = str(string)
    if count != 1:
        pluralized = _pluralize(string)
    return f'{count} {pluralized}'


def restrict_to(*restrictions):
    """Create the metadata for a restricted dataclass field"""
    return {'restrict': list(restrictions)}


def coerce_dict_str(value: Any) -> Optional[Dict[str, Any]]:
    """For annoying mypy reasons, this helper makes dealing with nested dicts
    easier. You get either `None` if it's not a Dict[str, Any], or the
    Dict[str, Any] you expected (to pass it to JsonSchemaMixin.from_dict(...)).
    """
    if (isinstance(value, dict) and all(isinstance(k, str) for k in value)):
        return value
    else:
        return None


# some types need to make constants available to the jinja context as
# attributes, and regular properties only work with objects. maybe this should
# be handled by the RelationProxy?

class classproperty(object):
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, objtype):
        return self.func(objtype)


def format_bytes(num_bytes):
    for unit in ['Bytes', 'KB', 'MB', 'GB', 'TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0

    return "> 1024 TB"


# a little concurrent.futures.Executor for single-threaded mode
class SingleThreadedExecutor(concurrent.futures.Executor):
    def submit(*args, **kwargs):
        # this basic pattern comes from concurrent.futures.Executor itself,
        # but without handling the `fn=` form.
        if len(args) >= 2:
            self, fn, *args = args
        elif not args:
            raise TypeError(
                "descriptor 'submit' of 'SingleThreadedExecutor' object needs "
                "an argument"
            )
        else:
            raise TypeError(
                'submit expected at least 1 positional argument, '
                'got %d' % (len(args) - 1)
            )
        fut = concurrent.futures.Future()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(result)
        return fut


class ThreadedArgs(Protocol):
    single_threaded: bool


class HasThreadingConfig(Protocol):
    args: ThreadedArgs
    threads: Optional[int]


def executor(config: HasThreadingConfig) -> concurrent.futures.Executor:
    if config.args.single_threaded:
        return SingleThreadedExecutor()
    else:
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=config.threads
        )


def fqn_search(
    root: Dict[str, Any], fqn: List[str]
) -> Iterator[Any]:
    """Iterate into a nested dictionary, looking for keys in the fqn as levels.
    Yield level name, level config pairs.
    """
    yield root

    for level in fqn:
        level_config = root.get(level, None)
        if level_config is None:
            break
        yield copy.deepcopy(level_config)
        root = level_config


StringMap = Mapping[str, Any]
StringMapList = List[StringMap]
StringMapIter = Iterable[StringMap]


class MultiDict(Mapping[str, Any]):
    """Implement the mapping protocol using a list of mappings. The most
    recently added mapping "wins".
    """
    def __init__(self, sources: Optional[StringMapList] = None) -> None:
        super().__init__()
        self.sources: StringMapList

        if sources is None:
            self.sources = []
        else:
            self.sources = sources

    def add_from(self, sources: Iterable[Mapping[str, Any]]):
        self.sources.extend(sources)

    def add(self, source: Mapping[str, Any]):
        self.sources.append(source)

    def _keyset(self) -> AbstractSet[str]:
        # return the set of keys
        keys: Set[str] = set()
        for entry in self._itersource():
            keys.update(entry)
        return keys

    def _itersource(self) -> Iterable[Mapping[str, Any]]:
        return reversed(self.sources)

    def __iter__(self) -> Iterator[str]:
        # we need to avoid duplicate keys
        return iter(self._keyset())

    def __len__(self):
        return len(self._keyset())

    def __getitem__(self, name: str) -> Any:
        for entry in self._itersource():
            if name in entry:
                return entry[name]
        raise KeyError(name)

    def __contains__(self, name) -> bool:
        return any((name in entry for entry in self._itersource()))
