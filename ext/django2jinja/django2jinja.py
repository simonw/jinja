# -*- coding: utf-8 -*-
"""
    Django to Jinja
    ~~~~~~~~~~~~~~~

    Helper module that can convert django templates into Jinja2 templates.

    This file is not intended to be used as stand alone application but to
    be used as library.  To convert templates you basically create your own
    writer, add extra conversion logic for your custom template tags,
    configure your django environment and run the `convert_templates`
    function.

    Here a simple example::

        # configure django (or use settings.configure)
        import os
        os.environ['DJANGO_SETTINGS_MODULE'] = 'yourapplication.settings'
        from yourapplication.foo.templatetags.bar import MyNode

        from django2jinja import Writer, convert_templates

        def write_my_node(writer, node):
            writer.start_variable()
            writer.write('myfunc(')
            for idx, arg in enumerate(node.args):
                if idx:
                    writer.write(', ')
                writer.node(arg)
            writer.write(')')
            writer.end_variable()

        writer = Writer()
        writer.node_handlers[MyNode] = write_my_node
        convert_templates('/path/to/output/folder', writer=writer)

    For details about the writing process have a look at the module code.

    :copyright: Copyright 2008 by Armin Ronacher.
    :license: BSD.
"""
import re
import os
import sys
from jinja2.defaults import *
from django.conf import settings
from django.template import defaulttags as core_tags, loader, TextNode, \
     FilterExpression, libraries, Variable, loader_tags
from django.template.debug import DebugVariableNode as VariableNode
from StringIO import StringIO


_node_handlers = {}
_resolved_filters = {}
_newline_re = re.compile(r'(?:\r\n|\r|\n)')


# don't ask....
_old_cycle_init = core_tags.CycleNode.__init__
def _fixed_cycle_init(self, cyclevars, variable_name=None):
    self.raw_cycle_vars = map(Variable, cyclevars)
    _old_cycle_init(self, cyclevars, variable_name)
core_tags.CycleNode.__init__ = _fixed_cycle_init


def node(cls):
    def proxy(f):
        _node_handlers[cls] = f
        return f
    return proxy


def convert_templates(output_dir, extensions=('.html', '.txt'), writer=None,
                      callback=None):
    """Iterates over all templates in the template dirs configured and
    translates them and writes the new templates into the output directory.
    """
    if writer is None:
        writer = Writer()

    def filter_templates(files):
        for filename in files:
            ifilename = filename.lower()
            for extension in extensions:
                if ifilename.endswith(extension):
                    yield filename

    def translate(f, loadname):
        template = loader.get_template(loadname)
        original = writer.stream
        writer.stream = f
        writer.body(template.nodelist)
        writer.stream = original

    if callback is None:
        def callback(template):
            print template

    for directory in settings.TEMPLATE_DIRS:
        for dirname, _, files in os.walk(directory):
            dirname = dirname[len(directory) + 1:]
            for filename in filter_templates(files):
                source = os.path.normpath(os.path.join(dirname, filename))
                target = os.path.join(output_dir, dirname, filename)
                basetarget = os.path.dirname(target)
                if not os.path.exists(basetarget):
                    os.makedirs(basetarget)
                callback(source)
                f = file(target, 'w')
                try:
                    translate(f, source)
                finally:
                    f.close()


class Writer(object):
    """The core writer class."""

    def __init__(self, stream=None, error_stream=None,
                 block_start_string=BLOCK_START_STRING,
                 block_end_string=BLOCK_END_STRING,
                 variable_start_string=VARIABLE_START_STRING,
                 variable_end_string=VARIABLE_END_STRING,
                 comment_start_string=COMMENT_START_STRING,
                 comment_end_string=COMMENT_END_STRING,
                 initial_autoescape=True,
                 use_jinja_autoescape=False,
                 custom_node_handlers=None):
        if stream is None:
            stream = sys.stdout
        if error_stream is None:
            error_stream = sys.stderr
        self.stream = stream
        self.error_stream = error_stream
        self.block_start_string = block_start_string
        self.block_end_string = block_end_string
        self.variable_start_string = variable_start_string
        self.variable_end_string = variable_end_string
        self.comment_start_string = comment_start_string
        self.comment_end_string = comment_end_string
        self.autoescape = initial_autoescape
        self.spaceless = False
        self.use_jinja_autoescape = use_jinja_autoescape
        self.node_handlers = dict(_node_handlers,
                                  **(custom_node_handlers or {}))

        self._loop_depth = 0
        self._filters_warned = set()

    def enter_loop(self):
        """Increments the loop depth so that write functions know if they
        are in a loop.
        """
        self._loop_depth += 1

    def leave_loop(self):
        """Reverse of enter_loop."""
        self._loop_depth -= 1

    @property
    def in_loop(self):
        """True if we are in a loop."""
        return self._loop_depth > 0

    def write(self, s):
        """Writes stuff to the stream."""
        self.stream.write(s.encode(settings.FILE_CHARSET))

    def print_expr(self, expr):
        """Open a variable tag, write to the string to the stream and close."""
        self.start_variable()
        self.write(expr)
        self.end_variable()

    def _post_open(self):
        if self.spaceless:
            self.write('- ')
        else:
            self.write(' ')

    def _pre_close(self):
        if self.spaceless:
            self.write(' -')
        else:
            self.write(' ')

    def start_variable(self):
        """Start a variable."""
        self.write(self.variable_start_string)
        self._post_open()

    def end_variable(self, always_safe=False):
        """End a variable."""
        if not always_safe and self.autoescape and \
           not self.use_jinja_autoescape:
            self.write('|e')
        self._pre_close()
        self.write(self.variable_end_string)

    def start_block(self):
        """Starts a block."""
        self.write(self.block_start_string)
        self._post_open()

    def end_block(self):
        """Ends a block."""
        self._pre_close()
        self.write(self.block_end_string)

    def tag(self, name):
        """Like `print_expr` just for blocks."""
        self.start_block()
        self.write(name)
        self.end_block()

    def variable(self, name):
        """Prints a variable.  This performs variable name transformation."""
        self.write(self.translate_variable_name(name))

    def literal(self, value):
        """Writes a value as literal."""
        value = repr(value)
        if value[:2] in ('u"', "u'"):
            value = value[1:]
        self.write(value)

    def filters(self, filters, is_block=False):
        """Dumps a list of filters."""
        want_pipe = not is_block
        for filter, args in filters:
            name = self.get_filter_name(filter)
            if name is None:
                self.warn('Could not find filter %s' % name)
                continue
            if name not in DEFAULT_FILTERS and \
               name not in self._filters_warned:
                self._filters_warned.add(name)
                self.warn('Filter %s probably doesn\'t exist in Jinja' %
                            name)
            if not want_pipe:
                want_pipe = True
            else:
                self.write('|')
            self.write(name)
            if args:
                self.write('(')
                for idx, (is_var, value) in enumerate(args):
                    if idx:
                        self.write(', ')
                    if is_var:
                        self.node(value)
                    else:
                        self.literal(value)
                self.write(')')

    def get_location(self, origin, position):
        """Returns the location for an origin and position tuple as name
        and lineno.
        """
        if hasattr(origin, 'source'):
            source = origin.source
            name = '<unknown source>'
        else:
            source = origin.loader(origin.loadname, origin.dirs)[0]
            name = origin.loadname
        lineno = len(_newline_re.findall(source[:position[0]])) + 1
        return name, lineno

    def warn(self, message, node=None):
        """Prints a warning to the error stream."""
        if node is not None and hasattr(node, 'source'):
            filename, lineno = self.get_location(*node.source)
            message = '[%s:%d] %s' % (filename, lineno, message)
        print >> self.error_stream, message

    def translate_variable_name(self, var):
        """Performs variable name translation."""
        if self.in_loop and var == 'forloop' or var.startswith('forloop.'):
            return var[3:]
        return var

    def get_filter_name(self, filter):
        """Returns the filter name for a filter function or `None` if there
        is no such filter.
        """
        if filter not in _resolved_filters:
            for library in libraries.values():
                for key, value in library.filters.iteritems():
                    _resolved_filters[value] = key
        return _resolved_filters.get(filter, None)

    def node(self, node):
        """Invokes the node handler for a node."""
        for cls, handler in self.node_handlers.iteritems():
            if type(node) is cls:
                handler(self, node)
                break
        else:
            self.warn('Untranslatable node %s.%s found' % (
                node.__module__,
                node.__class__.__name__
            ), node)

    def body(self, nodes):
        """Calls node() for every node in the iterable passed."""
        for node in nodes:
            self.node(node)


@node(TextNode)
def text_node(writer, node):
    writer.write(node.s)


@node(Variable)
def variable(writer, node):
    if node.literal is not None:
        writer.literal(node.literal)
    else:
        writer.variable(node.var)


@node(VariableNode)
def variable_node(writer, node):
    writer.start_variable()
    if node.filter_expression.var.var == 'block.super' \
       and not node.filter_expression.filters:
        writer.write('super()')
    else:
        writer.node(node.filter_expression)
    writer.end_variable()


@node(FilterExpression)
def filter_expression(writer, node):
    writer.node(node.var)
    writer.filters(node.filters)


@node(core_tags.CommentNode)
def comment_tag(writer, node):
    pass


@node(core_tags.DebugNode)
def comment_tag(writer, node):
    writer.warn('Debug tag detected.  Make sure to add a global function '
                'called debug to the namespace.', node=node)
    writer.print_expr('debug()')


@node(core_tags.ForNode)
def for_loop(writer, node):
    writer.start_block()
    writer.write('for ')
    for idx, var in enumerate(node.loopvars):
        if idx:
            writer.write(', ')
        writer.variable(var)
    writer.write(' in ')
    if node.is_reversed:
        writer.write('(')
    writer.node(node.sequence)
    if node.is_reversed:
        writer.write(')|reverse')
    writer.end_block()
    writer.enter_loop()
    writer.body(node.nodelist_loop)
    writer.leave_loop()
    writer.tag('endfor')


@node(core_tags.IfNode)
def if_condition(writer, node):
    writer.start_block()
    writer.write('if ')
    join_with = core_tags.IfNode.LinkTypes.or_ and 'or' or 'and'
    for idx, (ifnot, expr) in enumerate(node.bool_exprs):
        if idx:
            writer.write(' %s ' % join_with)
        if ifnot:
            writer.write('not ')
        writer.node(expr)
    writer.end_block()
    writer.body(node.nodelist_true)
    if node.nodelist_false:
        writer.tag('else')
        writer.body(node.nodelist_false)
    writer.tag('endif')


@node(core_tags.IfEqualNode)
def if_equal(writer, node):
    writer.start_block()
    writer.write('if ')
    writer.node(node.var1)
    writer.write(' == ')
    writer.node(node.var2)
    writer.end_block()
    writer.body(node.nodelist_true)
    if node.nodelist_false:
        writer.tag('else')
        writer.body(node.nodelist_false)
    writer.tag('endif')


@node(loader_tags.BlockNode)
def block(writer, node):
    writer.tag('block ' + node.name.replace('-', '_'))
    node = node
    while node.parent is not None:
        node = node.parent
    writer.body(node.nodelist)
    writer.tag('endblock')


@node(loader_tags.ExtendsNode)
def extends(writer, node):
    writer.start_block()
    writer.write('extends ')
    if node.parent_name_expr:
        writer.node(node.parent_name_expr)
    else:
        writer.literal(node.parent_name)
    writer.end_block()
    writer.body(node.nodelist)


@node(loader_tags.ConstantIncludeNode)
@node(loader_tags.IncludeNode)
def include(writer, node):
    writer.start_block()
    writer.write('include ')
    if hasattr(node, 'template'):
        writer.literal(node.template.name)
    else:
        writer.node(node.template_name)
    writer.end_block()


@node(core_tags.CycleNode)
def cycle(writer, node):
    if not writer.in_loop:
        writer.warn('Untranslatable free cycle (cycle outside loop)', node=node)
        return
    if node.variable_name is not None:
        writer.start_block()
        writer.write('set %s = ' % node.variable_name)
    else:
        writer.start_variable()
    writer.write('loop.cycle(')
    for idx, var in enumerate(node.raw_cycle_vars):
        if idx:
            writer.write(', ')
        writer.node(var)
    writer.write(')')
    if node.variable_name is not None:
        writer.end_block()
    else:
        writer.end_variable()


@node(core_tags.FilterNode)
def filter(writer, node):
    writer.start_block()
    writer.write('filter ')
    writer.filters(node.filter_expr.filters, True)
    writer.end_block()
    writer.body(node.nodelist)
    writer.tag('endfilter')


@node(core_tags.AutoEscapeControlNode)
def autoescape_control(writer, node):
    original = writer.autoescape
    writer.autoescape = node.setting
    writer.body(node.nodelist)
    writer.autoescape = original


@node(core_tags.SpacelessNode)
def spaceless(writer, node):
    original = writer.spaceless
    writer.spaceless = True
    writer.warn('entering spaceless mode with different semantics', node)
    # do the initial stripping
    nodelist = list(node.nodelist)
    if nodelist:
        if isinstance(nodelist[0], TextNode):
            nodelist[0] = TextNode(nodelist[0].s.lstrip())
        if isinstance(nodelist[-1], TextNode):
            nodelist[-1] = TextNode(nodelist[-1].s.rstrip())
    writer.body(nodelist)
    writer.spaceless = original


@node(core_tags.TemplateTagNode)
def template_tag(writer, node):
    tag = {
        'openblock':            writer.block_start_string,
        'closeblock':           writer.block_end_string,
        'openvariable':         writer.variable_start_string,
        'closevariable':        writer.variable_end_string,
        'opencomment':          writer.comment_start_string,
        'closecomment':         writer.comment_end_string,
        'openbrace':            '{',
        'closebrace':           '}'
    }.get(node.tagtype)
    if tag:
        writer.start_variable()
        writer.literal(tag)
        writer.end_variable()


@node(core_tags.URLNode)
def url_tag(writer, node):
    writer.warn('url node used.  make sure to provide a proper url() '
                'function', node)
    if node.asvar:
        writer.start_block()
        writer.write('set %s = ' % node.asvar)
    else:
        writer.start_variable()
    autoescape = writer.autoescape
    writer.write('url(')
    writer.literal(node.view_name)
    for arg in node.args:
        writer.write(', ')
        writer.node(arg)
    for key, arg in node.kwargs.items():
        writer.write(', %s=' % key)
        writer.node(arg)
    writer.write(')')
    if node.asvar:
        writer.end_block()
    else:
        writer.end_variable()


@node(core_tags.WidthRatioNode)
def width_ratio(writer, node):
    writer.warn('widthratio expanded into formula.  You may want to provide '
                'a helper function for this calculation', node)
    writer.start_variable()
    writer.write('(')
    writer.node(node.val_expr)
    writer.write(' / ')
    writer.node(node.max_expr)
    writer.write(' * ')
    writer.write(str(int(node.max_width)))
    writer.write(')|round|int')
    writer.end_variable(always_safe=True)


@node(core_tags.WithNode)
def with_block(writer, node):
    writer.warn('with block expanded into set statement.  This could cause '
                'variables following that block to be overriden.', node)
    writer.start_block()
    writer.write('set %s = ' % node.name)
    writer.node(node.var)
    writer.end_block()
    writer.body(node.nodelist)


@node(core_tags.RegroupNode)
def regroup(writer, node):
    if node.expression.var.literal:
        writer.warn('literal in groupby filter used.   Behavior in that '
                    'situation is undefined and translation is skipped.', node)
        return
    elif node.expression.filters:
        writer.warn('filters in groupby filter used.   Behavior in that '
                    'situation is undefined which is most likely a bug '
                    'in your code.  Filters were ignored.', node)
    writer.start_block()
    writer.write('set %s = ' % node.var_name)
    writer.node(node.target)
    writer.write('|groupby(')
    writer.literal(node.expression.var.var)
    writer.write(')')
    writer.end_block()


@node(core_tags.LoadNode)
def warn_load(writer, node):
    writer.warn('load statement used which was ignored on conversion', node)


# get rid of node now, it shouldn't be used normally
del node