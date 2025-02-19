import functools
import inspect
import operator
import os
import posixpath
from urllib.parse import quote, urljoin, urlparse, urlunparse
import warnings
from webob.acceptparse import Accept
from zope.interface import Interface, implementedBy, implementer
from zope.interface.interfaces import IInterface

from pyramid import renderers
from pyramid.asset import resolve_asset_spec
from pyramid.config.actions import action_method
from pyramid.config.predicates import (
    DEFAULT_PHASH,
    MAX_ORDER,
    normalize_accept_offer,
    predvalseq,
    sort_accept_offers,
)
from pyramid.decorator import reify
from pyramid.exceptions import ConfigurationError, PredicateMismatch
from pyramid.httpexceptions import (
    HTTPForbidden,
    HTTPNotFound,
    default_exceptionresponse_view,
)
from pyramid.interfaces import (
    PHASE1_CONFIG,
    IAcceptOrder,
    IException,
    IExceptionViewClassifier,
    IMultiView,
    IPackageOverrides,
    IRendererFactory,
    IRequest,
    IResponse,
    IRouteRequest,
    ISecuredView,
    IStaticURLInfo,
    IView,
    IViewClassifier,
    IViewDeriverInfo,
    IViewDerivers,
    IViewMapperFactory,
)
import pyramid.predicates
from pyramid.registry import Deferred
from pyramid.security import NO_PERMISSION_REQUIRED
from pyramid.static import static_view
from pyramid.url import parse_url_overrides
from pyramid.util import (
    WIN,
    TopologicalSorter,
    as_sorted_tuple,
    is_nonstr_iter,
)
from pyramid.view import AppendSlashNotFoundViewFactory
import pyramid.viewderivers
from pyramid.viewderivers import (
    INGRESS,
    VIEW,
    DefaultViewMapper,
    preserve_view_attrs,
    requestonly,
    view_description,
    wraps_view,
)

DefaultViewMapper = DefaultViewMapper  # bw-compat
preserve_view_attrs = preserve_view_attrs  # bw-compat
requestonly = requestonly  # bw-compat
view_description = view_description  # bw-compat


@implementer(IMultiView)
class MultiView:
    def __init__(self, name):
        self.name = name
        self.media_views = {}
        self.views = []
        self.accepts = []

    def __discriminator__(self, context, request):
        # used by introspection systems like so:
        # view = adapters.lookup(....)
        # view.__discriminator__(context, request) -> view's discriminator
        # so that superdynamic systems can feed the discriminator to
        # the introspection system to get info about it
        view = self.match(context, request)
        return view.__discriminator__(context, request)

    def add(self, view, order, phash=None, accept=None, accept_order=None):
        if phash is not None:
            for i, (s, v, h) in enumerate(list(self.views)):
                if phash == h:
                    self.views[i] = (order, view, phash)
                    return

        if accept is None:
            self.views.append((order, view, phash))
            self.views.sort(key=operator.itemgetter(0))
        else:
            subset = self.media_views.setdefault(accept, [])
            for i, (s, v, h) in enumerate(list(subset)):
                if phash == h:
                    subset[i] = (order, view, phash)
                    return
            else:
                subset.append((order, view, phash))
                subset.sort(key=operator.itemgetter(0))
            # dedupe accepts and sort appropriately
            accepts = set(self.accepts)
            accepts.add(accept)
            if accept_order:
                accept_order = [v for _, v in accept_order.sorted()]
            self.accepts = sort_accept_offers(accepts, accept_order)

    def get_views(self, request):
        if self.accepts and hasattr(request, 'accept'):
            views = []
            for offer, _ in request.accept.acceptable_offers(self.accepts):
                views.extend(self.media_views[offer])
            views.extend(self.views)
            return views
        return self.views

    def match(self, context, request):
        for order, view, phash in self.get_views(request):
            if not hasattr(view, '__predicated__'):
                return view
            if view.__predicated__(context, request):
                return view
        raise PredicateMismatch(self.name)

    def __permitted__(self, context, request):
        view = self.match(context, request)
        if hasattr(view, '__permitted__'):
            return view.__permitted__(context, request)
        return True

    def __call_permissive__(self, context, request):
        view = self.match(context, request)
        view = getattr(view, '__call_permissive__', view)
        return view(context, request)

    def __call__(self, context, request):
        for order, view, phash in self.get_views(request):
            try:
                return view(context, request)
            except PredicateMismatch:
                continue
        raise PredicateMismatch(self.name)


def attr_wrapped_view(view, info):
    accept, order, phash = (
        info.options.get('accept', None),
        getattr(info, 'order', MAX_ORDER),
        getattr(info, 'phash', DEFAULT_PHASH),
    )
    # this is a little silly but we don't want to decorate the original
    # function with attributes that indicate accept, order, and phash,
    # so we use a wrapper
    if (accept is None) and (order == MAX_ORDER) and (phash == DEFAULT_PHASH):
        return view  # defaults

    def attr_view(context, request):
        return view(context, request)

    attr_view.__accept__ = accept
    attr_view.__order__ = order
    attr_view.__phash__ = phash
    attr_view.__view_attr__ = info.options.get('attr')
    attr_view.__permission__ = info.options.get('permission')
    return attr_view


attr_wrapped_view.options = ('accept', 'attr', 'permission')


def predicated_view(view, info):
    preds = info.predicates
    if not preds:
        return view

    def predicate_wrapper(context, request):
        for predicate in preds:
            if not predicate(context, request):
                view_name = getattr(view, '__name__', view)
                raise PredicateMismatch(
                    'predicate mismatch for view %s (%s)'
                    % (view_name, predicate.text())
                )
        return view(context, request)

    def checker(context, request):
        return all(predicate(context, request) for predicate in preds)

    predicate_wrapper.__predicated__ = checker
    predicate_wrapper.__predicates__ = preds
    return predicate_wrapper


def viewdefaults(wrapped):
    """Decorator for add_view-like methods which takes into account
    __view_defaults__ attached to view it is passed.  Not a documented API but
    used by some external systems."""

    def wrapper(self, *arg, **kw):
        defaults = {}
        if arg:
            view = arg[0]
        else:
            view = kw.get('view')
        view = self.maybe_dotted(view)
        if inspect.isclass(view):
            defaults = getattr(view, '__view_defaults__', {}).copy()
        if '_backframes' not in kw:
            kw['_backframes'] = 1  # for action_method
        defaults.update(kw)
        return wrapped(self, *arg, **defaults)

    return functools.wraps(wrapped)(wrapper)


def combine_decorators(*decorators):
    def decorated(view_callable):
        # reversed() allows a more natural ordering in the api
        for decorator in reversed(decorators):
            view_callable = decorator(view_callable)
        return view_callable

    return decorated


class ViewsConfiguratorMixin:
    @viewdefaults
    @action_method
    def add_view(
        self,
        view=None,
        name="",
        for_=None,
        permission=None,
        request_type=None,
        route_name=None,
        request_method=None,
        request_param=None,
        containment=None,
        attr=None,
        renderer=None,
        wrapper=None,
        xhr=None,
        accept=None,
        header=None,
        path_info=None,
        custom_predicates=(),
        context=None,
        decorator=None,
        mapper=None,
        http_cache=None,
        match_param=None,
        require_csrf=None,
        exception_only=False,
        **view_options,
    ):
        """Add a :term:`view configuration` to the current
        configuration state.  Arguments to ``add_view`` are broken
        down below into *predicate* arguments and *non-predicate*
        arguments.  Predicate arguments narrow the circumstances in
        which the view callable will be invoked when a request is
        presented to :app:`Pyramid`; non-predicate arguments are
        informational.

        Non-Predicate Arguments

        view

          A :term:`view callable` or a :term:`dotted Python name`
          which refers to a view callable.  This argument is required
          unless a ``renderer`` argument also exists.  If a
          ``renderer`` argument is passed, and a ``view`` argument is
          not provided, the view callable defaults to a callable that
          returns an empty dictionary (see
          :ref:`views_which_use_a_renderer`).

        permission

          A :term:`permission` that the user must possess in order to invoke
          the :term:`view callable`.  See :ref:`view_security_section` for
          more information about view security and permissions.  This is
          often a string like ``view`` or ``edit``.

          If ``permission`` is omitted, a *default* permission may be used
          for this view registration if one was named as the
          :class:`pyramid.config.Configurator` constructor's
          ``default_permission`` argument, or if
          :meth:`pyramid.config.Configurator.set_default_permission` was used
          prior to this view registration.  Pass the value
          :data:`pyramid.security.NO_PERMISSION_REQUIRED` as the permission
          argument to explicitly indicate that the view should always be
          executable by entirely anonymous users, regardless of the default
          permission, bypassing any :term:`authorization policy` that may be
          in effect.

        attr

          This knob is most useful when the view definition is a class.

          The view machinery defaults to using the ``__call__`` method
          of the :term:`view callable` (or the function itself, if the
          view callable is a function) to obtain a response.  The
          ``attr`` value allows you to vary the method attribute used
          to obtain the response.  For example, if your view was a
          class, and the class has a method named ``index`` and you
          wanted to use this method instead of the class' ``__call__``
          method to return the response, you'd say ``attr="index"`` in the
          view configuration for the view.

        renderer

          This is either a single string term (e.g. ``json``) or a
          string implying a path or :term:`asset specification`
          (e.g. ``templates/views.pt``) naming a :term:`renderer`
          implementation.  If the ``renderer`` value does not contain
          a dot ``.``, the specified string will be used to look up a
          renderer implementation, and that renderer implementation
          will be used to construct a response from the view return
          value.  If the ``renderer`` value contains a dot (``.``),
          the specified term will be treated as a path, and the
          filename extension of the last element in the path will be
          used to look up the renderer implementation, which will be
          passed the full path.  The renderer implementation will be
          used to construct a :term:`response` from the view return
          value.

          Note that if the view itself returns a :term:`response` (see
          :ref:`the_response`), the specified renderer implementation
          is never called.

          When the renderer is a path, although a path is usually just
          a simple relative pathname (e.g. ``templates/foo.pt``,
          implying that a template named "foo.pt" is in the
          "templates" directory relative to the directory of the
          current :term:`package` of the Configurator), a path can be
          absolute, starting with a slash on UNIX or a drive letter
          prefix on Windows.  The path can alternately be a
          :term:`asset specification` in the form
          ``some.dotted.package_name:relative/path``, making it
          possible to address template assets which live in a
          separate package.

          The ``renderer`` attribute is optional.  If it is not
          defined, the "null" renderer is assumed (no rendering is
          performed and the value is passed back to the upstream
          :app:`Pyramid` machinery unmodified).

        http_cache

          .. versionadded:: 1.1

          When you supply an ``http_cache`` value to a view configuration,
          the ``Expires`` and ``Cache-Control`` headers of a response
          generated by the associated view callable are modified.  The value
          for ``http_cache`` may be one of the following:

          - A nonzero integer.  If it's a nonzero integer, it's treated as a
            number of seconds.  This number of seconds will be used to
            compute the ``Expires`` header and the ``Cache-Control:
            max-age`` parameter of responses to requests which call this view.
            For example: ``http_cache=3600`` instructs the requesting browser
            to 'cache this response for an hour, please'.

          - A ``datetime.timedelta`` instance.  If it's a
            ``datetime.timedelta`` instance, it will be converted into a
            number of seconds, and that number of seconds will be used to
            compute the ``Expires`` header and the ``Cache-Control:
            max-age`` parameter of responses to requests which call this view.
            For example: ``http_cache=datetime.timedelta(days=1)`` instructs
            the requesting browser to 'cache this response for a day, please'.

          - Zero (``0``).  If the value is zero, the ``Cache-Control`` and
            ``Expires`` headers present in all responses from this view will
            be composed such that client browser cache (and any intermediate
            caches) are instructed to never cache the response.

          - A two-tuple.  If it's a two tuple (e.g. ``http_cache=(1,
            {'public':True})``), the first value in the tuple may be a
            nonzero integer or a ``datetime.timedelta`` instance; in either
            case this value will be used as the number of seconds to cache
            the response.  The second value in the tuple must be a
            dictionary.  The values present in the dictionary will be used as
            input to the ``Cache-Control`` response header.  For example:
            ``http_cache=(3600, {'public':True})`` means 'cache for an hour,
            and add ``public`` to the Cache-Control header of the response'.
            All keys and values supported by the
            ``webob.cachecontrol.CacheControl`` interface may be added to the
            dictionary.  Supplying ``{'public':True}`` is equivalent to
            calling ``response.cache_control.public = True``.

          Providing a non-tuple value as ``http_cache`` is equivalent to
          calling ``response.cache_expires(value)`` within your view's body.

          Providing a two-tuple value as ``http_cache`` is equivalent to
          calling ``response.cache_expires(value[0], **value[1])`` within your
          view's body.

          If you wish to avoid influencing, the ``Expires`` header, and
          instead wish to only influence ``Cache-Control`` headers, pass a
          tuple as ``http_cache`` with the first element of ``None``, e.g.:
          ``(None, {'public':True})``.

          If you wish to prevent a view that uses ``http_cache`` in its
          configuration from having its caching response headers changed by
          this machinery, set ``response.cache_control.prevent_auto = True``
          before returning the response from the view.  This effectively
          disables any HTTP caching done by ``http_cache`` for that response.

        require_csrf

          .. versionadded:: 1.7

          A boolean option or ``None``. Default: ``None``.

          If this option is set to ``True`` then CSRF checks will be enabled
          for requests to this view. The required token or header default to
          ``csrf_token`` and ``X-CSRF-Token``, respectively.

          CSRF checks only affect "unsafe" methods as defined by RFC2616. By
          default, these methods are anything except
          ``GET``, ``HEAD``, ``OPTIONS``, and ``TRACE``.

          The defaults here may be overridden by
          :meth:`pyramid.config.Configurator.set_default_csrf_options`.

          This feature requires a configured :term:`session factory`.

          If this option is set to ``False`` then CSRF checks will be disabled
          regardless of the default ``require_csrf`` setting passed
          to ``set_default_csrf_options``.

          See :ref:`auto_csrf_checking` for more information.

        wrapper

          The :term:`view name` of a different :term:`view
          configuration` which will receive the response body of this
          view as the ``request.wrapped_body`` attribute of its own
          :term:`request`, and the :term:`response` returned by this
          view as the ``request.wrapped_response`` attribute of its
          own request.  Using a wrapper makes it possible to "chain"
          views together to form a composite response.  The response
          of the outermost wrapper view will be returned to the user.
          The wrapper view will be found as any view is found: see
          :ref:`view_lookup`.  The "best" wrapper view will be found
          based on the lookup ordering: "under the hood" this wrapper
          view is looked up via
          ``pyramid.view.render_view_to_response(context, request,
          'wrapper_viewname')``. The context and request of a wrapper
          view is the same context and request of the inner view.  If
          this attribute is unspecified, no view wrapping is done.

        decorator

          A :term:`dotted Python name` to function (or the function itself,
          or an iterable of the aforementioned) which will be used to
          decorate the registered :term:`view callable`.  The decorator
          function(s) will be called with the view callable as a single
          argument.  The view callable it is passed will accept
          ``(context, request)``.  The decorator(s) must return a
          replacement view callable which also accepts ``(context,
          request)``.

          If decorator is an iterable, the callables will be combined and
          used in the order provided as a decorator.
          For example::

            @view_config(...,
                decorator=(decorator2,
                           decorator1))
            def myview(request):
                ....

          Is similar to doing::

            @view_config(...)
            @decorator2
            @decorator1
            def myview(request):
                ...

          Except with the existing benefits of ``decorator=`` (having a common
          decorator syntax for all view calling conventions and not having to
          think about preserving function attributes such as ``__name__`` and
          ``__module__`` within decorator logic).

          An important distinction is that each decorator will receive a
          response object implementing :class:`pyramid.interfaces.IResponse`
          instead of the raw value returned from the view callable. All
          decorators in the chain must return a response object or raise an
          exception:

          .. code-block:: python

             def log_timer(wrapped):
                 def wrapper(context, request):
                     start = time.time()
                     response = wrapped(context, request)
                     duration = time.time() - start
                     response.headers['X-View-Time'] = '%.3f' % (duration,)
                     log.info('view took %.3f seconds', duration)
                     return response
                 return wrapper

          .. versionchanged:: 1.4a4
             Passing an iterable.

        mapper

          A Python object or :term:`dotted Python name` which refers to a
          :term:`view mapper`, or ``None``.  By default it is ``None``, which
          indicates that the view should use the default view mapper.  This
          plug-point is useful for Pyramid extension developers, but it's not
          very useful for 'civilians' who are just developing stock Pyramid
          applications. Pay no attention to the man behind the curtain.

        accept

          A :term:`media type` that will be matched against the ``Accept``
          HTTP request header.  If this value is specified, it must be a
          specific media type such as ``text/html`` or ``text/html;level=1``.
          If the media type is acceptable by the ``Accept`` header of the
          request, or if the ``Accept`` header isn't set at all in the request,
          this predicate will match. If this does not match the ``Accept``
          header of the request, view matching continues.

          If ``accept`` is not specified, the ``HTTP_ACCEPT`` HTTP header is
          not taken into consideration when deciding whether or not to invoke
          the associated view callable.

          The ``accept`` argument is technically not a predicate and does
          not support wrapping with :func:`pyramid.config.not_`.

          See :ref:`accept_content_negotiation` for more information.

          .. versionchanged:: 1.10

              Specifying a media range is deprecated and will be removed in
              :app:`Pyramid` 2.0. Use explicit media types to avoid any
              ambiguities in content negotiation.

          .. versionchanged:: 2.0

              Removed support for media ranges.

        exception_only

          .. versionadded:: 1.8

          When this value is ``True``, the ``context`` argument must be
          a subclass of ``Exception``. This flag indicates that only an
          :term:`exception view` should be created, and that this view should
          not match if the traversal :term:`context` matches the ``context``
          argument. If the ``context`` is a subclass of ``Exception`` and
          this value is ``False`` (the default), then a view will be
          registered to match the traversal :term:`context` as well.

        Predicate Arguments

        name

          The :term:`view name`.  Read :ref:`traversal_chapter` to
          understand the concept of a view name.

        context

          An object or a :term:`dotted Python name` referring to an
          interface or class object that the :term:`context` must be
          an instance of, *or* the :term:`interface` that the
          :term:`context` must provide in order for this view to be
          found and called.  This predicate is true when the
          :term:`context` is an instance of the represented class or
          if the :term:`context` provides the represented interface;
          it is otherwise false.  This argument may also be provided
          to ``add_view`` as ``for_`` (an older, still-supported
          spelling). If the view should *only* match when handling
          exceptions, then set the ``exception_only`` to ``True``.

        route_name

          This value must match the ``name`` of a :term:`route
          configuration` declaration (see :ref:`urldispatch_chapter`)
          that must match before this view will be called.

        request_type

          This value should be an :term:`interface` that the
          :term:`request` must provide in order for this view to be
          found and called.  This value exists only for backwards
          compatibility purposes.

        request_method

          This value can be either a string (such as ``"GET"``, ``"POST"``,
          ``"PUT"``, ``"DELETE"``, ``"HEAD"`` or ``"OPTIONS"``) representing
          an HTTP ``REQUEST_METHOD``, or a tuple containing one or more of
          these strings.  A view declaration with this argument ensures that
          the view will only be called when the ``method`` attribute of the
          request (aka the ``REQUEST_METHOD`` of the WSGI environment) matches
          a supplied value.  Note that use of ``GET`` also implies that the
          view will respond to ``HEAD`` as of Pyramid 1.4.

          .. versionchanged:: 1.2
             The ability to pass a tuple of items as ``request_method``.
             Previous versions allowed only a string.

        request_param

          This value can be any string or any sequence of strings.  A view
          declaration with this argument ensures that the view will only be
          called when the :term:`request` has a key in the ``request.params``
          dictionary (an HTTP ``GET`` or ``POST`` variable) that has a
          name which matches the supplied value (if the value is a string)
          or values (if the value is a tuple).  If any value
          supplied has a ``=`` sign in it,
          e.g. ``request_param="foo=123"``, then the key (``foo``)
          must both exist in the ``request.params`` dictionary, *and*
          the value must match the right hand side of the expression
          (``123``) for the view to "match" the current request.

        match_param

          .. versionadded:: 1.2

          This value can be a string of the format "key=value" or a tuple
          containing one or more of these strings.

          A view declaration with this argument ensures that the view will
          only be called when the :term:`request` has key/value pairs in its
          :term:`matchdict` that equal those supplied in the predicate.
          e.g. ``match_param="action=edit"`` would require the ``action``
          parameter in the :term:`matchdict` match the right hand side of
          the expression (``edit``) for the view to "match" the current
          request.

          If the ``match_param`` is a tuple, every key/value pair must match
          for the predicate to pass.

        containment

          This value should be a Python class or :term:`interface` (or a
          :term:`dotted Python name`) that an object in the
          :term:`lineage` of the context must provide in order for this view
          to be found and called.  The nodes in your object graph must be
          "location-aware" to use this feature.  See
          :ref:`location_aware` for more information about
          location-awareness.

        xhr

          This value should be either ``True`` or ``False``.  If this
          value is specified and is ``True``, the :term:`request`
          must possess an ``HTTP_X_REQUESTED_WITH`` (aka
          ``X-Requested-With``) header that has the value
          ``XMLHttpRequest`` for this view to be found and called.
          This is useful for detecting AJAX requests issued from
          jQuery, Prototype and other Javascript libraries.

        header

          This argument can be a string or an iterable of strings for HTTP
          headers.  The matching is determined as follow:

          - If a string does not contain a ``:`` (colon), it will be
            considered to be a header name (example ``If-Modified-Since``).
            In this case, the header specified by the name must be present
            in the request for this string to match.  Case is not significant.

          - If a string contains a colon, it will be considered a
            name/value pair (for example ``User-Agent:Mozilla/.*`` or
            ``Host:localhost``), where the value part is a regular
            expression.  The header specified by the name must be present
            in the request *and* the regular expression specified as the
            value part must match the value of the request header.  Case is
            not significant for the header name, but it is for the value.

          All strings must be matched for this predicate to return ``True``.
          If this predicate returns ``False``, view matching continues.

        path_info

          This value represents a regular expression pattern that will
          be tested against the ``PATH_INFO`` WSGI environment
          variable.  If the regex matches, this predicate will be
          ``True``.

        physical_path

          If specified, this value should be a string or a tuple representing
          the :term:`physical path` of the context found via traversal for this
          predicate to match as true.  For example: ``physical_path='/'`` or
          ``physical_path='/a/b/c'`` or ``physical_path=('', 'a', 'b', 'c')``.
          This is not a path prefix match or a regex, it's a whole-path match.
          It's useful when you want to always potentially show a view when some
          object is traversed to, but you can't be sure about what kind of
          object it will be, so you can't use the ``context`` predicate.  The
          individual path elements in between slash characters or in tuple
          elements should be the Unicode representation of the name of the
          resource and should not be encoded in any way.

          .. versionadded:: 1.4a3

        is_authenticated

          This value, if specified, must be either ``True`` or ``False``.
          If it is specified and ``True``, only a request from an authenticated
          user, as determined by the :term:`security policy` in use, will
          satisfy the predicate.
          If it is specified and ``False``, only a request from a user who is
          not authenticated will satisfy the predicate.

          .. versionadded:: 2.0

        effective_principals

          If specified, this value should be a :term:`principal` identifier or
          a sequence of principal identifiers.  If the
          :attr:`pyramid.request.Request.effective_principals` property
          indicates that every principal named in the argument list is present
          in the current request, this predicate will return True; otherwise it
          will return False.  For example:
          ``effective_principals=pyramid.authorization.Authenticated`` or
          ``effective_principals=('fred', 'group:admins')``.

          .. versionadded:: 1.4a4

          .. deprecated:: 2.0
              Use ``is_authenticated`` or a custom predicate.

        custom_predicates

            .. deprecated:: 1.5
                This value should be a sequence of references to custom
                predicate callables.  Each custom predicate callable
                should accept two arguments:
                ``context`` and ``request`` and should return either
                ``True`` or ``False`` after doing arbitrary evaluation of
                the context and/or the request.  The ability to register
                custom view predicates via
                :meth:`pyramid.config.Configurator.add_view_predicate`
                obsoletes this argument, but it is kept around for backwards
                compatibility.

        \\*\\*view_options

          Pass extra keyword parameters to use custom predicates
          or set a value for a view deriver. See
          :meth:`pyramid.config.Configurator.add_view_predicate` and
          :meth:`pyramid.config.Configurator.add_view_deriver`. See
          :ref:`view_and_route_predicates` for more information about
          custom predicates and :ref:`view_derivers` for information
          about view derivers.

          .. versionadded: 1.4a1

          .. versionchanged: 1.7

             Support setting view deriver options. Previously, only custom
             view predicate values could be supplied.

          .. versionchanged:: 2.0

             Removed support for the ``check_csrf`` predicate.

        """
        if custom_predicates:
            warnings.warn(
                (
                    'The "custom_predicates" argument to '
                    'Configurator.add_view is deprecated as of Pyramid 1.5. '
                    'Use "config.add_view_predicate" and use the registered '
                    'view predicate as a predicate argument to add_view '
                    'instead. See "Adding A Custom View, Route, or '
                    'Subscriber Predicate" in the "Hooks" chapter of the '
                    'documentation for more information.'
                ),
                DeprecationWarning,
                stacklevel=4,
            )

        if 'effective_principals' in view_options:
            warnings.warn(
                (
                    'The new security policy has deprecated '
                    'effective_principals. See "Upgrading '
                    'Authentication/Authorization" in "What\'s New in '
                    'Pyramid 2.0" of the documentation for more information.'
                ),
                DeprecationWarning,
                stacklevel=4,
            )

        if accept is not None:
            if is_nonstr_iter(accept):
                raise ConfigurationError(
                    'A list is not supported in the "accept" view predicate.'
                )
            accept = normalize_accept_offer(accept)

        view = self.maybe_dotted(view)
        context = self.maybe_dotted(context)
        for_ = self.maybe_dotted(for_)
        containment = self.maybe_dotted(containment)
        mapper = self.maybe_dotted(mapper)

        if is_nonstr_iter(decorator):
            decorator = combine_decorators(*map(self.maybe_dotted, decorator))
        else:
            decorator = self.maybe_dotted(decorator)

        if not view:
            if renderer:

                def view(context, request):
                    return {}

            else:
                raise ConfigurationError(
                    '"view" was not specified and no "renderer" specified'
                )

        if request_type is not None:
            request_type = self.maybe_dotted(request_type)
            if not IInterface.providedBy(request_type):
                raise ConfigurationError(
                    'request_type must be an interface, not %s' % request_type
                )

        if context is None:
            context = for_

        isexc = isexception(context)
        if exception_only and not isexc:
            raise ConfigurationError(
                'view "context" must be an exception type when '
                '"exception_only" is True'
            )

        r_context = context
        if r_context is None:
            r_context = Interface
        if not IInterface.providedBy(r_context):
            r_context = implementedBy(r_context)

        if isinstance(renderer, str):
            renderer = renderers.RendererHelper(
                name=renderer, package=self.package, registry=self.registry
            )

        introspectables = []
        ovals = view_options.copy()
        ovals.update(
            dict(
                xhr=xhr,
                request_method=request_method,
                path_info=path_info,
                request_param=request_param,
                header=header,
                accept=accept,
                containment=containment,
                request_type=request_type,
                match_param=match_param,
                custom=predvalseq(custom_predicates),
            )
        )

        def discrim_func():
            # We need to defer the discriminator until we know what the phash
            # is.  It can't be computed any sooner because thirdparty
            # predicates/view derivers may not yet exist when add_view is
            # called.
            predlist = self.get_predlist('view')
            valid_predicates = predlist.names()
            pvals = {}
            dvals = {}

            for (k, v) in ovals.items():
                if k in valid_predicates:
                    pvals[k] = v
                else:
                    dvals[k] = v

            self._check_view_options(**dvals)

            order, preds, phash = predlist.make(self, **pvals)

            view_intr.update(
                {'phash': phash, 'order': order, 'predicates': preds}
            )
            return ('view', context, name, route_name, phash)

        discriminator = Deferred(discrim_func)

        if inspect.isclass(view) and attr:
            view_desc = 'method {!r} of {}'.format(
                attr,
                self.object_description(view),
            )
        else:
            view_desc = self.object_description(view)

        tmpl_intr = None

        view_intr = self.introspectable(
            'views', discriminator, view_desc, 'view'
        )
        view_intr.update(
            dict(
                name=name,
                context=context,
                exception_only=exception_only,
                containment=containment,
                request_param=request_param,
                request_methods=request_method,
                route_name=route_name,
                attr=attr,
                xhr=xhr,
                accept=accept,
                header=header,
                path_info=path_info,
                match_param=match_param,
                http_cache=http_cache,
                require_csrf=require_csrf,
                callable=view,
                mapper=mapper,
                decorator=decorator,
            )
        )
        view_intr.update(view_options)
        introspectables.append(view_intr)

        def register(permission=permission, renderer=renderer):
            request_iface = IRequest
            if route_name is not None:
                request_iface = self.registry.queryUtility(
                    IRouteRequest, name=route_name
                )
                if request_iface is None:
                    # route configuration should have already happened in
                    # phase 2
                    raise ConfigurationError(
                        'No route named %s found for view registration'
                        % route_name
                    )

            if renderer is None:
                # use default renderer if one exists (reg'd in phase 1)
                if self.registry.queryUtility(IRendererFactory) is not None:
                    renderer = renderers.RendererHelper(
                        name=None, package=self.package, registry=self.registry
                    )

            renderer_type = getattr(renderer, 'type', None)
            intrspc = self.introspector
            if (
                renderer_type is not None
                and tmpl_intr is not None
                and intrspc is not None
                and intrspc.get('renderer factories', renderer_type)
                is not None
            ):
                # allow failure of registered template factories to be deferred
                # until view execution, like other bad renderer factories; if
                # we tried to relate this to an existing renderer factory
                # without checking if the factory actually existed, we'd end
                # up with a KeyError at startup time, which is inconsistent
                # with how other bad renderer registrations behave (they throw
                # a ValueError at view execution time)
                tmpl_intr.relate('renderer factories', renderer.type)

            # make a new view separately for normal and exception paths
            if not exception_only:
                derived_view = derive_view(False, renderer)
                register_view(IViewClassifier, request_iface, derived_view)
            if isexc:
                derived_exc_view = derive_view(True, renderer)
                register_view(
                    IExceptionViewClassifier, request_iface, derived_exc_view
                )

                if exception_only:
                    derived_view = derived_exc_view

            # if there are two derived views, combine them into one for
            # introspection purposes
            if not exception_only and isexc:
                derived_view = runtime_exc_view(derived_view, derived_exc_view)

            derived_view.__discriminator__ = lambda *arg: discriminator
            # __discriminator__ is used by superdynamic systems
            # that require it for introspection after manual view lookup;
            # see also MultiView.__discriminator__
            view_intr['derived_callable'] = derived_view

            self.registry._clear_view_lookup_cache()

        def derive_view(isexc_only, renderer):
            # added by discrim_func above during conflict resolving
            preds = view_intr['predicates']
            order = view_intr['order']
            phash = view_intr['phash']

            derived_view = self._derive_view(
                view,
                route_name=route_name,
                permission=permission,
                predicates=preds,
                attr=attr,
                context=context,
                exception_only=isexc_only,
                renderer=renderer,
                wrapper_viewname=wrapper,
                viewname=name,
                accept=accept,
                order=order,
                phash=phash,
                decorator=decorator,
                mapper=mapper,
                http_cache=http_cache,
                require_csrf=require_csrf,
                extra_options=ovals,
            )
            return derived_view

        def register_view(classifier, request_iface, derived_view):
            # A multiviews is a set of views which are registered for
            # exactly the same context type/request type/name triad.  Each
            # constituent view in a multiview differs only by the
            # predicates which it possesses.

            # To find a previously registered view for a context
            # type/request type/name triad, we need to use the
            # ``registered`` method of the adapter registry rather than
            # ``lookup``.  ``registered`` ignores interface inheritance
            # for the required and provided arguments, returning only a
            # view registered previously with the *exact* triad we pass
            # in.

            # We need to do this three times, because we use three
            # different interfaces as the ``provided`` interface while
            # doing registrations, and ``registered`` performs exact
            # matches on all the arguments it receives.

            old_view = None
            order, phash = view_intr['order'], view_intr['phash']
            registered = self.registry.adapters.registered

            for view_type in (IView, ISecuredView, IMultiView):
                old_view = registered(
                    (classifier, request_iface, r_context), view_type, name
                )
                if old_view is not None:
                    break

            old_phash = getattr(old_view, '__phash__', DEFAULT_PHASH)
            is_multiview = IMultiView.providedBy(old_view)
            want_multiview = (
                is_multiview
                # no component was yet registered for exactly this triad
                # or only one was registered but with the same phash, meaning
                # that this view is an override
                or (old_view is not None and old_phash != phash)
            )

            if not want_multiview:
                if hasattr(derived_view, '__call_permissive__'):
                    view_iface = ISecuredView
                else:
                    view_iface = IView
                self.registry.registerAdapter(
                    derived_view,
                    (classifier, request_iface, context),
                    view_iface,
                    name,
                )

            else:
                # - A view or multiview was already registered for this
                #   triad, and the new view is not an override.

                # XXX we could try to be more efficient here and register
                # a non-secured view for a multiview if none of the
                # multiview's constituent views have a permission
                # associated with them, but this code is getting pretty
                # rough already
                if is_multiview:
                    multiview = old_view
                else:
                    multiview = MultiView(name)
                    old_accept = getattr(old_view, '__accept__', None)
                    old_order = getattr(old_view, '__order__', MAX_ORDER)
                    # don't bother passing accept_order here as we know we're
                    # adding another one right after which will re-sort
                    multiview.add(old_view, old_order, old_phash, old_accept)
                accept_order = self.registry.queryUtility(IAcceptOrder)
                multiview.add(derived_view, order, phash, accept, accept_order)
                for view_type in (IView, ISecuredView):
                    # unregister any existing views
                    self.registry.adapters.unregister(
                        (classifier, request_iface, r_context),
                        view_type,
                        name=name,
                    )
                self.registry.registerAdapter(
                    multiview,
                    (classifier, request_iface, context),
                    IMultiView,
                    name=name,
                )

        if mapper:
            mapper_intr = self.introspectable(
                'view mappers',
                discriminator,
                'view mapper for %s' % view_desc,
                'view mapper',
            )
            mapper_intr['mapper'] = mapper
            mapper_intr.relate('views', discriminator)
            introspectables.append(mapper_intr)
        if route_name:
            view_intr.relate('routes', route_name)  # see add_route
        if renderer is not None and renderer.name and '.' in renderer.name:
            # the renderer is a template
            tmpl_intr = self.introspectable(
                'templates', discriminator, renderer.name, 'template'
            )
            tmpl_intr.relate('views', discriminator)
            tmpl_intr['name'] = renderer.name
            tmpl_intr['type'] = renderer.type
            tmpl_intr['renderer'] = renderer
            introspectables.append(tmpl_intr)
        if permission is not None:
            # if a permission exists, register a permission introspectable
            perm_intr = self.introspectable(
                'permissions', permission, permission, 'permission'
            )
            perm_intr['value'] = permission
            perm_intr.relate('views', discriminator)
            introspectables.append(perm_intr)
        self.action(discriminator, register, introspectables=introspectables)

    def _check_view_options(self, **kw):
        # we only need to validate deriver options because the predicates
        # were checked by the predlist
        derivers = self.registry.getUtility(IViewDerivers)
        for deriver in derivers.values():
            for opt in getattr(deriver, 'options', []):
                kw.pop(opt, None)
        if kw:
            raise ConfigurationError(f'Unknown view options: {kw}')

    def _apply_view_derivers(self, info):
        # These derivers are not really derivers and so have fixed order
        outer_derivers = [
            ('attr_wrapped_view', attr_wrapped_view),
            ('predicated_view', predicated_view),
        ]

        view = info.original_view
        derivers = self.registry.getUtility(IViewDerivers)
        for name, deriver in reversed(outer_derivers + derivers.sorted()):
            view = wraps_view(deriver)(view, info)
        return view

    @action_method
    def add_view_predicate(
        self, name, factory, weighs_more_than=None, weighs_less_than=None
    ):
        """
        .. versionadded:: 1.4

        Adds a view predicate factory.  The associated view predicate can
        later be named as a keyword argument to
        :meth:`pyramid.config.Configurator.add_view` in the
        ``predicates`` anonyous keyword argument dictionary.

        ``name`` should be the name of the predicate.  It must be a valid
        Python identifier (it will be used as a keyword argument to
        ``add_view`` by others).

        ``factory`` should be a :term:`predicate factory` or :term:`dotted
        Python name` which refers to a predicate factory.

        See :ref:`view_and_route_predicates` for more information.
        """
        self._add_predicate(
            'view',
            name,
            factory,
            weighs_more_than=weighs_more_than,
            weighs_less_than=weighs_less_than,
        )

    def add_default_view_predicates(self):
        p = pyramid.predicates
        for (name, factory) in (
            ('xhr', p.XHRPredicate),
            ('request_method', p.RequestMethodPredicate),
            ('path_info', p.PathInfoPredicate),
            ('request_param', p.RequestParamPredicate),
            ('header', p.HeaderPredicate),
            ('accept', p.AcceptPredicate),
            ('containment', p.ContainmentPredicate),
            ('request_type', p.RequestTypePredicate),
            ('match_param', p.MatchParamPredicate),
            ('physical_path', p.PhysicalPathPredicate),
            ('is_authenticated', p.IsAuthenticatedPredicate),
            ('effective_principals', p.EffectivePrincipalsPredicate),
            ('custom', p.CustomPredicate),
        ):
            self.add_view_predicate(name, factory)

    def add_default_accept_view_order(self):
        for accept in (
            'text/html',
            'application/xhtml+xml',
            'application/xml',
            'text/xml',
            'text/plain',
            'application/json',
        ):
            self.add_accept_view_order(accept)

    @action_method
    def add_accept_view_order(
        self, value, weighs_more_than=None, weighs_less_than=None
    ):
        """
        Specify an ordering preference for the ``accept`` view option used
        during :term:`view lookup`.

        By default, if two views have different ``accept`` options and a
        request specifies ``Accept: */*`` or omits the header entirely then
        it is random which view will be selected. This method provides a way
        to specify a server-side, relative ordering between accept media types.

        ``value`` should be a :term:`media type` as specified by
        :rfc:`7231#section-5.3.2`. For example, ``text/plain;charset=utf8``,
        ``application/json`` or ``text/html``.

        ``weighs_more_than`` and ``weighs_less_than`` control the ordering
        of media types. Each value may be a string or a list of strings. If
        all options for ``weighs_more_than`` (or ``weighs_less_than``) cannot
        be found, it is an error.

        Earlier calls to ``add_accept_view_order`` are given higher priority
        over later calls, assuming similar constraints but standard conflict
        resolution mechanisms can be used to override constraints.

        See :ref:`accept_content_negotiation` for more information.

        .. versionadded:: 1.10

        """

        def check_type(than):
            than_type, than_subtype, than_params = Accept.parse_offer(than)
            # text/plain vs text/html;charset=utf8
            if bool(offer_params) ^ bool(than_params):
                raise ConfigurationError(
                    'cannot compare a media type with params to one without '
                    'params'
                )
            # text/plain;charset=utf8 vs text/html;charset=utf8
            if offer_params and (
                offer_subtype != than_subtype or offer_type != than_type
            ):
                raise ConfigurationError(
                    'cannot compare params across different media types'
                )

        def normalize_types(thans):
            thans = [normalize_accept_offer(than) for than in thans]
            for than in thans:
                check_type(than)
            return thans

        value = normalize_accept_offer(value)
        offer_type, offer_subtype, offer_params = Accept.parse_offer(value)

        if weighs_more_than:
            if not is_nonstr_iter(weighs_more_than):
                weighs_more_than = [weighs_more_than]
            weighs_more_than = normalize_types(weighs_more_than)

        if weighs_less_than:
            if not is_nonstr_iter(weighs_less_than):
                weighs_less_than = [weighs_less_than]
            weighs_less_than = normalize_types(weighs_less_than)

        discriminator = ('accept view order', value)
        intr = self.introspectable(
            'accept view order', value, value, 'accept view order'
        )
        intr['value'] = value
        intr['weighs_more_than'] = weighs_more_than
        intr['weighs_less_than'] = weighs_less_than

        def register():
            sorter = self.registry.queryUtility(IAcceptOrder)
            if sorter is None:
                sorter = TopologicalSorter()
                self.registry.registerUtility(sorter, IAcceptOrder)
            sorter.add(
                value, value, before=weighs_more_than, after=weighs_less_than
            )

        self.action(
            discriminator,
            register,
            introspectables=(intr,),
            order=PHASE1_CONFIG,
        )  # must be registered before add_view

    @action_method
    def add_view_deriver(self, deriver, name=None, under=None, over=None):
        """
        .. versionadded:: 1.7

        Add a :term:`view deriver` to the view pipeline. View derivers are
        a feature used by extension authors to wrap views in custom code
        controllable by view-specific options.

        ``deriver`` should be a callable conforming to the
        :class:`pyramid.interfaces.IViewDeriver` interface.

        ``name`` should be the name of the view deriver.  There are no
        restrictions on the name of a view deriver. If left unspecified, the
        name will be constructed from the name of the ``deriver``.

        The ``under`` and ``over`` options can be used to control the ordering
        of view derivers by providing hints about where in the view pipeline
        the deriver is used. Each option may be a string or a list of strings.
        At least one view deriver in each, the over and under directions, must
        exist to fully satisfy the constraints.

        ``under`` means closer to the user-defined :term:`view callable`,
        and ``over`` means closer to view pipeline ingress.

        The default value for ``over`` is ``rendered_view`` and ``under`` is
        ``decorated_view``. This places the deriver somewhere between the two
        in the view pipeline. If the deriver should be placed elsewhere in the
        pipeline, such as above ``decorated_view``, then you MUST also specify
        ``under`` to something earlier in the order, or a
        ``CyclicDependencyError`` will be raised when trying to sort the
        derivers.

        See :ref:`view_derivers` for more information.

        """
        deriver = self.maybe_dotted(deriver)

        if name is None:
            name = deriver.__name__

        if name in (INGRESS, VIEW):
            raise ConfigurationError(
                '%s is a reserved view deriver name' % name
            )

        if under is None:
            under = 'decorated_view'

        if over is None:
            over = 'rendered_view'

        over = as_sorted_tuple(over)
        under = as_sorted_tuple(under)

        if INGRESS in over:
            raise ConfigurationError('%s cannot be over INGRESS' % name)

        # ensure everything is always over mapped_view
        if VIEW in over and name != 'mapped_view':
            over = as_sorted_tuple(over + ('mapped_view',))

        if VIEW in under:
            raise ConfigurationError('%s cannot be under VIEW' % name)
        if 'mapped_view' in under:
            raise ConfigurationError('%s cannot be under "mapped_view"' % name)

        discriminator = ('view deriver', name)
        intr = self.introspectable('view derivers', name, name, 'view deriver')
        intr['name'] = name
        intr['deriver'] = deriver
        intr['under'] = under
        intr['over'] = over

        def register():
            derivers = self.registry.queryUtility(IViewDerivers)
            if derivers is None:
                derivers = TopologicalSorter(
                    default_before=None,
                    default_after=INGRESS,
                    first=INGRESS,
                    last=VIEW,
                )
                self.registry.registerUtility(derivers, IViewDerivers)
            derivers.add(name, deriver, before=over, after=under)

        self.action(
            discriminator,
            register,
            introspectables=(intr,),
            order=PHASE1_CONFIG,
        )  # must be registered before add_view

    def add_default_view_derivers(self):
        d = pyramid.viewderivers
        derivers = [
            ('secured_view', d.secured_view),
            ('owrapped_view', d.owrapped_view),
            ('http_cached_view', d.http_cached_view),
            ('decorated_view', d.decorated_view),
            ('rendered_view', d.rendered_view),
            ('mapped_view', d.mapped_view),
        ]
        last = INGRESS
        for name, deriver in derivers:
            self.add_view_deriver(deriver, name=name, under=last, over=VIEW)
            last = name

        # leave the csrf_view loosely coupled to the rest of the pipeline
        # by ensuring nothing in the default pipeline depends on the order
        # of the csrf_view
        self.add_view_deriver(
            d.csrf_view,
            'csrf_view',
            under='secured_view',
            over='owrapped_view',
        )

    def derive_view(self, view, attr=None, renderer=None):
        """
        Create a :term:`view callable` using the function, instance,
        or class (or :term:`dotted Python name` referring to the same)
        provided as ``view`` object.

        .. warning::

           This method is typically only used by :app:`Pyramid` framework
           extension authors, not by :app:`Pyramid` application developers.

        This is API is useful to framework extenders who create
        pluggable systems which need to register 'proxy' view
        callables for functions, instances, or classes which meet the
        requirements of being a :app:`Pyramid` view callable.  For
        example, a ``some_other_framework`` function in another
        framework may want to allow a user to supply a view callable,
        but he may want to wrap the view callable in his own before
        registering the wrapper as a :app:`Pyramid` view callable.
        Because a :app:`Pyramid` view callable can be any of a
        number of valid objects, the framework extender will not know
        how to call the user-supplied object.  Running it through
        ``derive_view`` normalizes it to a callable which accepts two
        arguments: ``context`` and ``request``.

        For example:

        .. code-block:: python

           def some_other_framework(user_supplied_view):
               config = Configurator(reg)
               proxy_view = config.derive_view(user_supplied_view)
               def my_wrapper(context, request):
                   do_something_that_mutates(request)
                   return proxy_view(context, request)
               config.add_view(my_wrapper)

        The ``view`` object provided should be one of the following:

        - A function or another non-class callable object that accepts
          a :term:`request` as a single positional argument and which
          returns a :term:`response` object.

        - A function or other non-class callable object that accepts
          two positional arguments, ``context, request`` and which
          returns a :term:`response` object.

        - A class which accepts a single positional argument in its
          constructor named ``request``, and which has a ``__call__``
          method that accepts no arguments that returns a
          :term:`response` object.

        - A class which accepts two positional arguments named
          ``context, request``, and which has a ``__call__`` method
          that accepts no arguments that returns a :term:`response`
          object.

        - A :term:`dotted Python name` which refers to any of the
          kinds of objects above.

        This API returns a callable which accepts the arguments
        ``context, request`` and which returns the result of calling
        the provided ``view`` object.

        The ``attr`` keyword argument is most useful when the view
        object is a class.  It names the method that should be used as
        the callable.  If ``attr`` is not provided, the attribute
        effectively defaults to ``__call__``.  See
        :ref:`class_as_view` for more information.

        The ``renderer`` keyword argument should be a renderer
        name. If supplied, it will cause the returned callable to use
        a :term:`renderer` to convert the user-supplied view result to
        a :term:`response` object.  If a ``renderer`` argument is not
        supplied, the user-supplied view must itself return a
        :term:`response` object."""
        return self._derive_view(view, attr=attr, renderer=renderer)

    # b/w compat
    def _derive_view(
        self,
        view,
        permission=None,
        predicates=(),
        attr=None,
        renderer=None,
        wrapper_viewname=None,
        viewname=None,
        accept=None,
        order=MAX_ORDER,
        phash=DEFAULT_PHASH,
        decorator=None,
        route_name=None,
        mapper=None,
        http_cache=None,
        context=None,
        require_csrf=None,
        exception_only=False,
        extra_options=None,
    ):
        view = self.maybe_dotted(view)
        mapper = self.maybe_dotted(mapper)
        if isinstance(renderer, str):
            renderer = renderers.RendererHelper(
                name=renderer, package=self.package, registry=self.registry
            )
        if renderer is None:
            # use default renderer if one exists
            if self.registry.queryUtility(IRendererFactory) is not None:
                renderer = renderers.RendererHelper(
                    name=None, package=self.package, registry=self.registry
                )

        options = dict(
            view=view,
            context=context,
            permission=permission,
            attr=attr,
            renderer=renderer,
            wrapper=wrapper_viewname,
            name=viewname,
            accept=accept,
            mapper=mapper,
            decorator=decorator,
            http_cache=http_cache,
            require_csrf=require_csrf,
            route_name=route_name,
        )
        if extra_options:
            options.update(extra_options)

        info = ViewDeriverInfo(
            view=view,
            registry=self.registry,
            package=self.package,
            predicates=predicates,
            exception_only=exception_only,
            options=options,
        )

        # order and phash are only necessary for the predicated view and
        # are not really view deriver options
        info.order = order
        info.phash = phash

        return self._apply_view_derivers(info)

    @viewdefaults
    @action_method
    def add_forbidden_view(
        self,
        view=None,
        attr=None,
        renderer=None,
        wrapper=None,
        route_name=None,
        request_type=None,
        request_method=None,
        request_param=None,
        containment=None,
        xhr=None,
        accept=None,
        header=None,
        path_info=None,
        custom_predicates=(),
        decorator=None,
        mapper=None,
        match_param=None,
        **view_options,
    ):
        """Add a forbidden view to the current configuration state.  The
        view will be called when Pyramid or application code raises a
        :exc:`pyramid.httpexceptions.HTTPForbidden` exception and the set of
        circumstances implied by the predicates provided are matched.  The
        simplest example is:

          .. code-block:: python

            def forbidden(request):
                return Response('Forbidden', status='403 Forbidden')

            config.add_forbidden_view(forbidden)

        If ``view`` argument is not provided, the view callable defaults to
        :func:`~pyramid.httpexceptions.default_exceptionresponse_view`.

        All arguments have the same meaning as
        :meth:`pyramid.config.Configurator.add_view` and each predicate
        argument restricts the set of circumstances under which this forbidden
        view will be invoked.  Unlike
        :meth:`pyramid.config.Configurator.add_view`, this method will raise
        an exception if passed ``name``, ``permission``, ``require_csrf``,
        ``context``, ``for_``, or ``exception_only`` keyword arguments. These
        argument values make no sense in the context of a forbidden
        :term:`exception view`.

        .. versionadded:: 1.3

        .. versionchanged:: 1.8

           The view is created using ``exception_only=True``.
        """
        for arg in (
            'name',
            'permission',
            'context',
            'for_',
            'require_csrf',
            'exception_only',
        ):
            if arg in view_options:
                raise ConfigurationError(
                    '%s may not be used as an argument to add_forbidden_view'
                    % (arg,)
                )

        if view is None:
            view = default_exceptionresponse_view

        settings = dict(
            view=view,
            context=HTTPForbidden,
            exception_only=True,
            wrapper=wrapper,
            request_type=request_type,
            request_method=request_method,
            request_param=request_param,
            containment=containment,
            xhr=xhr,
            accept=accept,
            header=header,
            path_info=path_info,
            custom_predicates=custom_predicates,
            decorator=decorator,
            mapper=mapper,
            match_param=match_param,
            route_name=route_name,
            permission=NO_PERMISSION_REQUIRED,
            require_csrf=False,
            attr=attr,
            renderer=renderer,
        )
        settings.update(view_options)
        return self.add_view(**settings)

    set_forbidden_view = add_forbidden_view  # deprecated sorta-bw-compat alias

    @viewdefaults
    @action_method
    def add_notfound_view(
        self,
        view=None,
        attr=None,
        renderer=None,
        wrapper=None,
        route_name=None,
        request_type=None,
        request_method=None,
        request_param=None,
        containment=None,
        xhr=None,
        accept=None,
        header=None,
        path_info=None,
        custom_predicates=(),
        decorator=None,
        mapper=None,
        match_param=None,
        append_slash=False,
        **view_options,
    ):
        """Add a default :term:`Not Found View` to the current configuration
        state. The view will be called when Pyramid or application code raises
        an :exc:`pyramid.httpexceptions.HTTPNotFound` exception (e.g., when a
        view cannot be found for the request).  The simplest example is:

          .. code-block:: python

            def notfound(request):
                return Response('Not Found', status='404 Not Found')

            config.add_notfound_view(notfound)

        If ``view`` argument is not provided, the view callable defaults to
        :func:`~pyramid.httpexceptions.default_exceptionresponse_view`.

        All arguments except ``append_slash`` have the same meaning as
        :meth:`pyramid.config.Configurator.add_view` and each predicate
        argument restricts the set of circumstances under which this notfound
        view will be invoked.  Unlike
        :meth:`pyramid.config.Configurator.add_view`, this method will raise
        an exception if passed ``name``, ``permission``, ``require_csrf``,
        ``context``, ``for_``, or ``exception_only`` keyword arguments. These
        argument values make no sense in the context of a Not Found View.

        If ``append_slash`` is ``True``, when this Not Found View is invoked,
        and the current path info does not end in a slash, the notfound logic
        will attempt to find a :term:`route` that matches the request's path
        info suffixed with a slash.  If such a route exists, Pyramid will
        issue a redirect to the URL implied by the route; if it does not,
        Pyramid will return the result of the view callable provided as
        ``view``, as normal.

        If the argument provided as ``append_slash`` is not a boolean but
        instead implements :class:`~pyramid.interfaces.IResponse`, the
        append_slash logic will behave as if ``append_slash=True`` was passed,
        but the provided class will be used as the response class instead of
        the default :class:`~pyramid.httpexceptions.HTTPTemporaryRedirect`
        response class when a redirect is performed.  For example:

          .. code-block:: python

            from pyramid.httpexceptions import HTTPMovedPermanently
            config.add_notfound_view(append_slash=HTTPMovedPermanently)

        The above means that a redirect to a slash-appended route will be
        attempted, but instead of
        :class:`~pyramid.httpexceptions.HTTPTemporaryRedirect`
        being used, :class:`~pyramid.httpexceptions.HTTPMovedPermanently will
        be used` for the redirect response if a slash-appended route is found.

        :class:`~pyramid.httpexceptions.HTTPTemporaryRedirect` class is used
        as default response, which is equivalent to
        :class:`~pyramid.httpexceptions.HTTPFound` with addition of redirecting
        with the same HTTP method (useful when doing POST requests).

        .. versionadded:: 1.3

        .. versionchanged:: 1.6

           The ``append_slash`` argument was modified to allow any object that
           implements the ``IResponse`` interface to specify the response class
           used when a redirect is performed.

        .. versionchanged:: 1.8

           The view is created using ``exception_only=True``.

        .. versionchanged: 1.10

           Default response was changed from
           :class:`~pyramid.httpexceptions.HTTPFound`
           to :class:`~pyramid.httpexceptions.HTTPTemporaryRedirect`.

        """
        for arg in (
            'name',
            'permission',
            'context',
            'for_',
            'require_csrf',
            'exception_only',
        ):
            if arg in view_options:
                raise ConfigurationError(
                    '%s may not be used as an argument to add_notfound_view'
                    % (arg,)
                )

        if view is None:
            view = default_exceptionresponse_view

        settings = dict(
            view=view,
            context=HTTPNotFound,
            exception_only=True,
            wrapper=wrapper,
            request_type=request_type,
            request_method=request_method,
            request_param=request_param,
            containment=containment,
            xhr=xhr,
            accept=accept,
            header=header,
            path_info=path_info,
            custom_predicates=custom_predicates,
            decorator=decorator,
            mapper=mapper,
            match_param=match_param,
            route_name=route_name,
            permission=NO_PERMISSION_REQUIRED,
            require_csrf=False,
        )
        settings.update(view_options)
        if append_slash:
            view = self._derive_view(view, attr=attr, renderer=renderer)
            if IResponse.implementedBy(append_slash):
                view = AppendSlashNotFoundViewFactory(
                    view, redirect_class=append_slash
                )
            else:
                view = AppendSlashNotFoundViewFactory(view)
            settings['view'] = view
        else:
            settings['attr'] = attr
            settings['renderer'] = renderer
        return self.add_view(**settings)

    set_notfound_view = add_notfound_view  # deprecated sorta-bw-compat alias

    @viewdefaults
    @action_method
    def add_exception_view(
        self,
        view=None,
        context=None,
        # force all other arguments to be specified as key=value
        **view_options,
    ):
        """Add an :term:`exception view` for the specified ``exception`` to
        the current configuration state. The view will be called when Pyramid
        or application code raises the given exception.

        This method accepts almost all of the same arguments as
        :meth:`pyramid.config.Configurator.add_view` except for ``name``,
        ``permission``, ``for_``, ``require_csrf``, and ``exception_only``.

        By default, this method will set ``context=Exception``, thus
        registering for most default Python exceptions. Any subclass of
        ``Exception`` may be specified.

        .. versionadded:: 1.8
        """
        for arg in (
            'name',
            'for_',
            'exception_only',
            'require_csrf',
            'permission',
        ):
            if arg in view_options:
                raise ConfigurationError(
                    '%s may not be used as an argument to add_exception_view'
                    % (arg,)
                )
        if context is None:
            context = Exception
        view_options.update(
            dict(
                view=view,
                context=context,
                exception_only=True,
                permission=NO_PERMISSION_REQUIRED,
                require_csrf=False,
            )
        )
        return self.add_view(**view_options)

    @action_method
    def set_view_mapper(self, mapper):
        """
        Setting a :term:`view mapper` makes it possible to make use of
        :term:`view callable` objects which implement different call
        signatures than the ones supported by :app:`Pyramid` as described in
        its narrative documentation.

        The ``mapper`` argument should be an object implementing
        :class:`pyramid.interfaces.IViewMapperFactory` or a :term:`dotted
        Python name` to such an object.  The provided ``mapper`` will become
        the default view mapper to be used by all subsequent :term:`view
        configuration` registrations.

        .. seealso::

            See also :ref:`using_a_view_mapper`.

        .. note::

           Using the ``default_view_mapper`` argument to the
           :class:`pyramid.config.Configurator` constructor
           can be used to achieve the same purpose.
        """
        mapper = self.maybe_dotted(mapper)

        def register():
            self.registry.registerUtility(mapper, IViewMapperFactory)

        # IViewMapperFactory is looked up as the result of view config
        # in phase 3
        intr = self.introspectable(
            'view mappers',
            IViewMapperFactory,
            self.object_description(mapper),
            'default view mapper',
        )
        intr['mapper'] = mapper
        self.action(
            IViewMapperFactory,
            register,
            order=PHASE1_CONFIG,
            introspectables=(intr,),
        )

    @action_method
    def add_static_view(self, name, path, **kw):
        """Add a view used to render static assets such as images
        and CSS files.

        The ``name`` argument is a string representing an
        application-relative local URL prefix.  It may alternately be a full
        URL.

        The ``path`` argument is the path on disk where the static files
        reside.  This can be an absolute path, a package-relative path, or a
        :term:`asset specification`.

        The ``cache_max_age`` keyword argument is input to set the
        ``Expires`` and ``Cache-Control`` headers for static assets served.
        Note that this argument has no effect when the ``name`` is a *url
        prefix*.  By default, this argument is ``None``, meaning that no
        particular Expires or Cache-Control headers are set in the response.

        The ``content_encodings`` keyword argument is a list of alternative
        file encodings supported in the ``Accept-Encoding`` HTTP Header.
        Alternative files are found using file extensions defined in
        :attr:`mimetypes.encodings_map`. An encoded asset will be returned
        with the ``Content-Encoding`` header set to the selected encoding.
        If the asset contains alternative encodings then the
        ``Accept-Encoding`` value will be added to the response's ``Vary``
        header. By default, the list is empty and no alternatives will be
        supported.

        The ``permission`` keyword argument is used to specify the
        :term:`permission` required by a user to execute the static view.  By
        default, it is the string
        :data:`pyramid.security.NO_PERMISSION_REQUIRED`, a special sentinel
        which indicates that, even if a :term:`default permission` exists for
        the current application, the static view should be renderered to
        completely anonymous users.  This default value is permissive
        because, in most web apps, static assets seldom need protection from
        viewing.  If ``permission`` is specified, the security checking will
        be performed against the default root factory ACL.

        Any other keyword arguments sent to ``add_static_view`` are passed on
        to :meth:`pyramid.config.Configurator.add_route` (e.g. ``factory``,
        perhaps to define a custom factory with a custom ACL for this static
        view).

        *Usage*

        The ``add_static_view`` function is typically used in conjunction
        with the :meth:`pyramid.request.Request.static_url` method.
        ``add_static_view`` adds a view which renders a static asset when
        some URL is visited; :meth:`pyramid.request.Request.static_url`
        generates a URL to that asset.

        The ``name`` argument to ``add_static_view`` is usually a simple URL
        prefix (e.g. ``'images'``).  When this is the case, the
        :meth:`pyramid.request.Request.static_url` API will generate a URL
        which points to a Pyramid view, which will serve up a set of assets
        that live in the package itself. For example:

        .. code-block:: python

           add_static_view('images', 'mypackage:images/')

        Code that registers such a view can generate URLs to the view via
        :meth:`pyramid.request.Request.static_url`:

        .. code-block:: python

           request.static_url('mypackage:images/logo.png')

        When ``add_static_view`` is called with a ``name`` argument that
        represents a URL prefix, as it is above, subsequent calls to
        :meth:`pyramid.request.Request.static_url` with paths that start with
        the ``path`` argument passed to ``add_static_view`` will generate a
        URL something like ``http://<Pyramid app URL>/images/logo.png``,
        which will cause the ``logo.png`` file in the ``images`` subdirectory
        of the ``mypackage`` package to be served.

        ``add_static_view`` can alternately be used with a ``name`` argument
        which is a *URL*, causing static assets to be served from an external
        webserver.  This happens when the ``name`` argument is a fully
        qualified URL (e.g. starts with ``http://`` or similar).  In this
        mode, the ``name`` is used as the prefix of the full URL when
        generating a URL using :meth:`pyramid.request.Request.static_url`.
        Furthermore, if a protocol-relative URL (e.g. ``//example.com/images``)
        is used as the ``name`` argument, the generated URL will use the
        protocol of the request (http or https, respectively).

        For example, if ``add_static_view`` is called like so:

        .. code-block:: python

           add_static_view('http://example.com/images', 'mypackage:images/')

        Subsequently, the URLs generated by
        :meth:`pyramid.request.Request.static_url` for that static view will
        be prefixed with ``http://example.com/images`` (the external webserver
        listening on ``example.com`` must be itself configured to respond
        properly to such a request.):

        .. code-block:: python

           static_url('mypackage:images/logo.png', request)

        See :ref:`static_assets_section` for more information.

        .. versionchanged:: 2.0

           Added the ``content_encodings`` argument.

        """
        spec = self._make_spec(path)
        info = self._get_static_info()
        info.add(self, name, spec, **kw)

    def add_cache_buster(self, path, cachebust, explicit=False):
        """
        Add a cache buster to a set of files on disk.

        The ``path`` should be the path on disk where the static files
        reside.  This can be an absolute path, a package-relative path, or a
        :term:`asset specification`.

        The ``cachebust`` argument may be set to cause
        :meth:`~pyramid.request.Request.static_url` to use cache busting when
        generating URLs. See :ref:`cache_busting` for general information
        about cache busting. The value of the ``cachebust`` argument must
        be an object which implements
        :class:`~pyramid.interfaces.ICacheBuster`.

        If ``explicit`` is set to ``True`` then the ``path`` for the cache
        buster will be matched based on the ``rawspec`` instead of the
        ``pathspec`` as defined in the
        :class:`~pyramid.interfaces.ICacheBuster` interface.
        Default: ``False``.

        .. versionadded:: 1.6

        """
        spec = self._make_spec(path)
        info = self._get_static_info()
        info.add_cache_buster(self, spec, cachebust, explicit=explicit)

    def _get_static_info(self):
        info = self.registry.queryUtility(IStaticURLInfo)
        if info is None:
            info = StaticURLInfo()
            self.registry.registerUtility(info, IStaticURLInfo)
        return info


def isexception(o):
    if IInterface.providedBy(o):
        if IException.isEqualOrExtendedBy(o):
            return True
    return isinstance(o, Exception) or (
        inspect.isclass(o) and (issubclass(o, Exception))
    )


def runtime_exc_view(view, excview):
    # create a view callable which can pretend to be both a normal view
    # and an exception view, dispatching to the appropriate one based
    # on the state of request.exception
    def wrapper_view(context, request):
        if getattr(request, 'exception', None):
            return excview(context, request)
        return view(context, request)

    # these constants are the same between the two views
    wrapper_view.__wraps__ = wrapper_view
    wrapper_view.__original_view__ = getattr(view, '__original_view__', view)
    wrapper_view.__module__ = view.__module__
    wrapper_view.__doc__ = view.__doc__
    wrapper_view.__name__ = view.__name__

    wrapper_view.__accept__ = getattr(view, '__accept__', None)
    wrapper_view.__order__ = getattr(view, '__order__', MAX_ORDER)
    wrapper_view.__phash__ = getattr(view, '__phash__', DEFAULT_PHASH)
    wrapper_view.__view_attr__ = getattr(view, '__view_attr__', None)
    wrapper_view.__permission__ = getattr(view, '__permission__', None)

    def wrap_fn(attr):
        def wrapper(context, request):
            if getattr(request, 'exception', None):
                selected_view = excview
            else:
                selected_view = view
            fn = getattr(selected_view, attr, None)
            if fn is not None:
                return fn(context, request)

        return wrapper

    # these methods are dynamic per-request and should dispatch to their
    # respective views based on whether it's an exception or not
    wrapper_view.__call_permissive__ = wrap_fn('__call_permissive__')
    wrapper_view.__permitted__ = wrap_fn('__permitted__')
    wrapper_view.__predicated__ = wrap_fn('__predicated__')
    wrapper_view.__predicates__ = wrap_fn('__predicates__')
    return wrapper_view


@implementer(IViewDeriverInfo)
class ViewDeriverInfo:
    def __init__(
        self, view, registry, package, predicates, exception_only, options
    ):
        self.original_view = view
        self.registry = registry
        self.package = package
        self.predicates = predicates or []
        self.options = options or {}
        self.exception_only = exception_only

    @reify
    def settings(self):
        return self.registry.settings


@implementer(IStaticURLInfo)
class StaticURLInfo:
    def __init__(self):
        self.registrations = []
        self.cache_busters = []

    def generate(self, path, request, **kw):
        for (url, spec, route_name) in self.registrations:
            if path.startswith(spec):
                subpath = path[len(spec) :]
                if WIN:  # pragma: no cover
                    subpath = subpath.replace('\\', '/')  # windows
                if self.cache_busters:
                    subpath, kw = self._bust_asset_path(
                        request, spec, subpath, kw
                    )
                if url is None:
                    kw['subpath'] = subpath
                    return request.route_url(route_name, **kw)
                else:
                    app_url, qs, anchor = parse_url_overrides(request, kw)
                    parsed = urlparse(url)
                    if not parsed.scheme:
                        url = urlunparse(
                            parsed._replace(scheme=request.scheme)
                        )
                    subpath = quote(subpath)
                    result = urljoin(url, subpath)
                    return result + qs + anchor

        raise ValueError('No static URL definition matching %s' % path)

    def add(self, config, name, spec, **extra):
        # This feature only allows for the serving of a directory and
        # the files contained within, not of a single asset;
        # appending a slash here if the spec doesn't have one is
        # required for proper prefix matching done in ``generate``
        # (``subpath = path[len(spec):]``).
        if os.path.isabs(spec):  # FBO windows
            sep = os.sep
        else:
            sep = '/'
        if not spec.endswith(sep) and not spec.endswith(':'):
            spec = spec + sep

        # we also make sure the name ends with a slash, purely as a
        # convenience: a name that is a url is required to end in a
        # slash, so that ``urljoin(name, subpath))`` will work above
        # when the name is a URL, and it doesn't hurt things for it to
        # have a name that ends in a slash if it's used as a route
        # name instead of a URL.
        if not name.endswith('/'):
            # make sure it ends with a slash
            name = name + '/'

        if urlparse(name).netloc:
            # it's a URL
            # url, spec, route_name
            url = name
            route_name = None
        else:
            # it's a view name
            url = None
            cache_max_age = extra.pop('cache_max_age', None)
            content_encodings = extra.pop('content_encodings', [])

            # create a view
            view = static_view(
                spec,
                cache_max_age=cache_max_age,
                use_subpath=True,
                reload=config.registry.settings['pyramid.reload_assets'],
                content_encodings=content_encodings,
            )

            # Mutate extra to allow factory, etc to be passed through here.
            # Treat permission specially because we'd like to default to
            # permissiveness (see docs of config.add_static_view).
            permission = extra.pop('permission', None)
            if permission is None:
                permission = NO_PERMISSION_REQUIRED

            context = extra.pop('context', None)
            if context is None:
                context = extra.pop('for_', None)

            renderer = extra.pop('renderer', None)

            # register a route using the computed view, permission, and
            # pattern, plus any extras passed to us via add_static_view
            pattern = "%s*subpath" % name  # name already ends with slash
            if config.route_prefix:
                route_name = f'__{config.route_prefix}/{name}'
            else:
                route_name = '__%s' % name
            config.add_route(route_name, pattern, **extra)
            config.add_view(
                route_name=route_name,
                view=view,
                permission=permission,
                context=context,
                renderer=renderer,
            )

        def register():
            registrations = self.registrations

            names = [t[0] for t in registrations]

            if name in names:
                idx = names.index(name)
                registrations.pop(idx)

            # url, spec, route_name
            registrations.append((url, spec, route_name))

        intr = config.introspectable(
            'static views', name, 'static view for %r' % name, 'static view'
        )
        intr['name'] = name
        intr['spec'] = spec

        config.action(None, callable=register, introspectables=(intr,))

    def add_cache_buster(self, config, spec, cachebust, explicit=False):
        # ensure the spec always has a trailing slash as we only support
        # adding cache busters to folders, not files
        if os.path.isabs(spec):  # FBO windows
            sep = os.sep
        else:
            sep = '/'
        if not spec.endswith(sep) and not spec.endswith(':'):
            spec = spec + sep

        def register():
            if config.registry.settings.get('pyramid.prevent_cachebust'):
                return

            cache_busters = self.cache_busters

            # find duplicate cache buster (old_idx)
            # and insertion location (new_idx)
            new_idx, old_idx = len(cache_busters), None
            for idx, (spec_, cb_, explicit_) in enumerate(cache_busters):
                # if we find an identical (spec, explicit) then use it
                if spec == spec_ and explicit == explicit_:
                    old_idx = new_idx = idx
                    break

                # past all explicit==False specs then add to the end
                elif not explicit and explicit_:
                    new_idx = idx
                    break

                # explicit matches and spec is shorter
                elif explicit == explicit_ and len(spec) < len(spec_):
                    new_idx = idx
                    break

            if old_idx is not None:
                cache_busters.pop(old_idx)

            cache_busters.insert(new_idx, (spec, cachebust, explicit))

        intr = config.introspectable(
            'cache busters', spec, 'cache buster for %r' % spec, 'cache buster'
        )
        intr['cachebust'] = cachebust
        intr['path'] = spec
        intr['explicit'] = explicit

        config.action(None, callable=register, introspectables=(intr,))

    def _bust_asset_path(self, request, spec, subpath, kw):
        registry = request.registry
        pkg_name, pkg_subpath = resolve_asset_spec(spec)
        rawspec = None

        if pkg_name is not None:
            pathspec = f'{pkg_name}:{pkg_subpath}{subpath}'
            overrides = registry.queryUtility(IPackageOverrides, name=pkg_name)
            if overrides is not None:
                resource_name = posixpath.join(pkg_subpath, subpath)
                sources = overrides.filtered_sources(resource_name)
                for source, filtered_path in sources:
                    rawspec = source.get_path(filtered_path)
                    if hasattr(source, 'pkg_name'):
                        rawspec = f'{source.pkg_name}:{rawspec}'
                    break

        else:
            pathspec = pkg_subpath + subpath

        if rawspec is None:
            rawspec = pathspec

        kw['pathspec'] = pathspec
        kw['rawspec'] = rawspec
        for spec_, cachebust, explicit in reversed(self.cache_busters):
            if (explicit and rawspec.startswith(spec_)) or (
                not explicit and pathspec.startswith(spec_)
            ):
                subpath, kw = cachebust(request, subpath, kw)
                break
        return subpath, kw
