"""Two roles, implemented as auth groups: viewers can see the plan, planners can also change settings."""
from functools import wraps

from django.core.exceptions import PermissionDenied

VIEWER = 'viewer'
PLANNER = 'planner'
ROLE_GROUPS = (VIEWER, PLANNER)


def _groups(user):
    if not hasattr(user, '_mrp_groups'):
        user._mrp_groups = set(user.groups.values_list('name', flat=True))
    return user._mrp_groups


def is_planner(user):
    return user.is_authenticated and (user.is_superuser or PLANNER in _groups(user))


def can_view(user):
    return user.is_authenticated and (user.is_superuser or bool(_groups(user) & set(ROLE_GROUPS)))


def viewer_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_view(request.user):
            raise PermissionDenied('Ask an administrator to add you to the viewer or planner group.')
        return view(request, *args, **kwargs)
    return wrapped


def planner_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_planner(request.user):
            raise PermissionDenied('Only planners can change planning settings.')
        return view(request, *args, **kwargs)
    return wrapped
