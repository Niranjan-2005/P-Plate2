from __future__ import annotations

from rest_framework.permissions import BasePermission

from .models import UserProfile


def get_user_role(user) -> str | None:
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, "profile", None)
    if profile is None:
        return None
    return profile.role


def is_administrator(user) -> bool:
    return get_user_role(user) == UserProfile.Role.ADMINISTRATOR


def is_engineer(user) -> bool:
    return get_user_role(user) == UserProfile.Role.ENGINEER


def is_supervisor(user) -> bool:
    return get_user_role(user) == UserProfile.Role.SUPERVISOR


class IsAdministrator(BasePermission):
    message = "Only administrators may manage products."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_administrator(request.user))


class IsEngineer(BasePermission):
    message = "Only engineers may submit inspection images."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_engineer(request.user))


# Historical alias kept so the upload endpoint reads intentionally.
CanSubmitInspections = IsEngineer


class IsSupervisor(BasePermission):
    message = "Only supervisors may access this resource."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_supervisor(request.user))
