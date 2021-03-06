import json
import os
import pytz
import voluptuous

from dbt.adapters.factory import get_adapter
from dbt.compat import basestring, to_string

import dbt.clients.jinja
import dbt.flags
import dbt.schema
import dbt.tracking
import dbt.utils

import dbt.hooks

from dbt.logger import GLOBAL_LOGGER as logger  # noqa


class DatabaseWrapper(object):
    """
    Wrapper for runtime database interaction. Should only call adapter
    functions.
    """

    def __init__(self, model, adapter, profile):
        self.model = model
        self.adapter = adapter
        self.profile = profile

        # Fun with metaprogramming
        # Most adapter functions take `profile` as the first argument, and
        # `model_name` as the last. This automatically injects those arguments.
        # In model code, these functions can be called without those two args.
        for context_function in self.adapter.context_functions:
            setattr(self,
                    context_function,
                    self.wrap_with_profile_and_model_name(context_function))

        for raw_function in self.adapter.raw_functions:
            setattr(self,
                    raw_function,
                    getattr(self.adapter, raw_function))

    def wrap_with_profile_and_model_name(self, fn):
        def wrapped(*args, **kwargs):
            args = (self.profile,) + args
            kwargs['model_name'] = self.model.get('name')
            return getattr(self.adapter, fn)(*args, **kwargs)

        return wrapped

    def type(self):
        return self.adapter.type()

    def commit(self):
        return self.adapter.commit_if_has_connection(
            self.profile, self.model.get('name'))


def _add_macros(context, model, flat_graph):
    macros_to_add = {'global': [], 'local': []}

    for unique_id, macro in flat_graph.get('macros', {}).items():
        package_name = macro.get('package_name')

        macro_map = {
            macro.get('name'): macro.get('generator')(context)
        }

        if context.get(package_name) is None:
            context[package_name] = {}

        context.get(package_name, {}) \
               .update(macro_map)

        if package_name == model.get('package_name'):
            macros_to_add['local'].append(macro_map)
        elif package_name == dbt.include.GLOBAL_PROJECT_NAME:
            macros_to_add['global'].append(macro_map)

    # Load global macros before local macros -- local takes precedence
    unprefixed_macros = macros_to_add['global'] + macros_to_add['local']
    for macro_map in unprefixed_macros:
        context.update(macro_map)

    return context


def _add_tracking(context):
    if dbt.tracking.active_user is not None:
        context = dbt.utils.merge(context, {
            "run_started_at": dbt.tracking.active_user.run_started_at,
            "invocation_id": dbt.tracking.active_user.invocation_id,
        })
    else:
        context = dbt.utils.merge(context, {
            "run_started_at": None,
            "invocation_id": None
        })

    return context


def _add_validation(context):
    validation_utils = dbt.utils.AttrDict({
        'any': voluptuous.Any,
        'all': voluptuous.All,
    })

    return dbt.utils.merge(
        context,
        {'validation': validation_utils})


def _env_var(var, default=None):
    if var in os.environ:
        return os.environ[var]
    elif default is not None:
        return default
    else:
        msg = "Env var required but not provided: '{}'".format(var)
        dbt.clients.jinja.undefined_error(msg)


def _store_result(sql_results):
    def call(name, status, data=[]):
        sql_results[name] = dbt.utils.AttrDict({
            'status': status,
            'data': data
        })
        return ''

    return call


def _load_result(sql_results):
    def call(name):
        return sql_results.get(name)

    return call


def _add_sql_handlers(context):
    sql_results = {}
    return dbt.utils.merge(context, {
        '_sql_results': sql_results,
        'store_result': _store_result(sql_results),
        'load_result': _load_result(sql_results),
    })


def log(msg, info=False):
    if info:
        logger.info(msg)
    else:
        logger.debug(msg)
    return ''


class Var(object):
    UndefinedVarError = "Required var '{}' not found in config:\nVars "\
                        "supplied to {} = {}"
    NoneVarError = "Supplied var '{}' is undefined in config:\nVars supplied "\
                   "to {} = {}"

    def __init__(self, model, context):
        self.model = model
        self.context = context

        if isinstance(model, dict) and model.get('unique_id'):
            self.local_vars = model.get('config', {}).get('vars')
            self.model_name = model.get('name')
        else:
            # still used for wrapping
            self.model_name = model.nice_name
            self.local_vars = model.config.get('vars', {})

    def pretty_dict(self, data):
        return json.dumps(data, sort_keys=True, indent=4)

    def assert_var_defined(self, var_name, default):
        if var_name not in self.local_vars and default is None:
            pretty_vars = self.pretty_dict(self.local_vars)
            dbt.exceptions.raise_compiler_error(
                self.UndefinedVarError.format(
                    var_name, self.model_name, pretty_vars
                ),
                self.model
            )

    def assert_var_not_none(self, var_name):
        raw = self.local_vars[var_name]
        if raw is None:
            pretty_vars = self.pretty_dict(self.local_vars)
            model_name = dbt.utils.get_model_name_or_none(self.model)
            dbt.exceptions.raise_compiler_error(
                self.NoneVarError.format(
                    var_name, model_name, pretty_vars
                ),
                self.model
            )

    def __call__(self, var_name, default=None):
        self.assert_var_defined(var_name, default)

        if var_name not in self.local_vars:
            return default

        self.assert_var_not_none(var_name)

        raw = self.local_vars[var_name]

        # if bool/int/float/etc are passed in, don't compile anything
        if not isinstance(raw, basestring):
            return raw

        return dbt.clients.jinja.get_rendered(raw, self.context)


def write(node, target_path, subdirectory):
    def fn(payload):
        node['build_path'] = dbt.writer.write_node(
            node, target_path, subdirectory, payload)
        return ''

    return fn


def render(context, node):
    def fn(string):
        return dbt.clients.jinja.get_rendered(string, context, node)

    return fn


def fromjson(string, default=None):
    try:
        return json.loads(string)
    except ValueError as e:
        return default


def tojson(value, default=None):
    try:
        return json.dumps(value)
    except ValueError as e:
        return default


def _return(value):
    raise dbt.exceptions.MacroReturn(value)


def generate(model, project, flat_graph, provider=None):
    """
    Not meant to be called directly. Call with either:
        dbt.context.parser.generate
    or
        dbt.context.runtime.generate
    """
    if provider is None:
        raise dbt.exceptions.InternalException(
            "Invalid provider given to context: {}".format(provider))

    target_name = project.get('target')
    profile = project.get('outputs').get(target_name)
    target = profile.copy()
    target.pop('pass', None)
    target['name'] = target_name
    adapter = get_adapter(profile)

    context = {'env': target}
    schema = profile.get('schema', 'public')

    pre_hooks = model.get('config', {}).get('pre-hook')
    post_hooks = model.get('config', {}).get('post-hook')

    db_wrapper = DatabaseWrapper(model, adapter, profile)

    context = dbt.utils.merge(context, {
        "adapter": db_wrapper,
        "column": dbt.schema.Column,
        "config": provider.Config(model),
        "env_var": _env_var,
        "exceptions": dbt.exceptions,
        "execute": provider.execute,
        "flags": dbt.flags,
        "graph": flat_graph,
        "log": log,
        "model": model,
        "modules": {
            "pytz": pytz,
        },
        "post_hooks": post_hooks,
        "pre_hooks": pre_hooks,
        "ref": provider.ref(model, project, profile, flat_graph),
        "return": _return,
        "schema": model.get('schema', schema),
        "sql": model.get('injected_sql'),
        "sql_now": adapter.date_function(),
        "fromjson": fromjson,
        "tojson": tojson,
        "target": target,
        "this": dbt.utils.Relation(profile, adapter, model, use_temp=True)
    })

    context = _add_tracking(context)
    context = _add_validation(context)
    context = _add_sql_handlers(context)

    # we make a copy of the context for each of these ^^

    context = _add_macros(context, model, flat_graph)

    context["write"] = write(model, project.get('target-path'), 'run')
    context["render"] = render(context, model)
    context["var"] = Var(model, context=context)
    context['context'] = context

    return context
